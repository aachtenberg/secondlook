"""A review is checked against what Second Look measured before it is recorded (dbtriage.grounding)."""
from dbtriage import grounding, llm
from dbtriage.agent import TriageAgent
from dbtriage.compare import compare
from dbtriage.report import render_markdown
from dbtriage.storage import LocalStorage
from tests.test_compare import snap

CHUNK = "_timescaledb_internal._hyper_24_1242_chunk"


def _chunk(idx_scan):
    return [{"name": CHUNK, "seq_scan": 5, "seq_tup_read": 151200, "idx_scan": idx_scan, "live_rows": 30240, "dead_rows": 0},
            {"name": "_timescaledb_internal._hyper_24_1219_chunk", "seq_scan": 0, "seq_tup_read": 0, "idx_scan": 3, "live_rows": 30240, "dead_rows": 0}]


def _pair():
    """The ts-sensors hour: a quiet database where one chunk's running total is high and its window count is small."""
    before = snap(0, {"q1": (1000, 4000)}, tables=_chunk(2000))
    base = snap(60, {"q1": (1040, 4100)}, tables=_chunk(2083))
    cur = snap(120, {"q1": (1080, 4300)}, tables=_chunk(2127))
    return before, base, cur


def test_a_table_finding_gets_the_window_count_and_both_running_totals():
    """The model wrote "idx_scan increased from 0 to 2127": the measured lines say 44 in the window and 2,083 to
    2,127 as running totals, and an unqualified chunk name still finds its table."""
    before, base, cur = _pair()
    c = compare(cur, base, before)
    lines = grounding.measured({"subject": "_hyper_24_1242_chunk"}, c, cur, base)
    assert lines[0].startswith(f"{CHUNK} in this window:") and "idx_scan 44 (0.0122/s)" in lines[0] and "seq_scan 0," in lines[0]
    assert "idx_scan 2,083 at the baseline, 2,127 now" in lines[1] and lines[1].startswith("Running totals")
    assert lines[2] == "Size: 30,240 live rows, 0 dead."
    assert grounding.measured({"subject": ""}, c, cur, base) == []  # names nothing: nothing to measure
    assert grounding.measured({"subject": "no_such_table"}, c, cur, base) == [grounding.NOT_FOUND.format("no_such_table")]
    typo = grounding.measured({"subject": "_timescaledb_internal._hyper_24_1342_chunk"}, c, cur, base)[0]
    assert typo.endswith(f"The nearest is {CHUNK}.")  # a mistyped name is shown beside the real one


def test_sql_metric_wait_and_parameter_subjects():
    before, base, cur = _pair()
    base["settings"], cur["settings"] = {"work_mem": "4MB"}, {"work_mem": "64MB"}
    c = compare(cur, base, before)
    sql = grounding.measured({"subject": "q1"}, c, cur, base)
    assert sql[0] == "SQL q1 in this window: 40 calls, 200 ms in all (0.0556 ms of SQL per second, 100.0% of the top SQL's time), 5 ms per call."
    assert sql[1].startswith("Reference window: 40 calls, 2.5 ms per call") and "per call +100%" in sql[1] and "+0.0278 ms of SQL per second" in sql[1]
    assert grounding.measured({"subject": "xact_commit"}, c, cur, base) == ["xact_commit: 60,000 in this window (16.7/s), 60,000 in the reference window (16.7/s)."]
    assert grounding.measured({"subject": "lock"}, c, cur, base)[0].startswith("Wait class Lock: ")
    assert grounding.measured({"subject": "work_mem"}, c, cur, base) == ["Parameter work_mem changed in this window: 4MB to 64MB."]
    # a cumulative first snapshot has no window: the running totals, said to be that
    first = grounding.measured({"subject": "q1"}, None, cur, None)
    assert first[0].startswith("SQL q1, running totals since the statistics were last reset (no baseline")


def test_compare_says_how_busy_the_database_was():
    """A query doubling per call on an idle database: the load and the time the change added are in the comparison."""
    before, base, cur = _pair()
    c = compare(cur, base, before)
    assert c["load"]["sql_ms_per_s"] == 0.055556 and c["load"]["commits_per_s"] == 16.667
    assert c["load"]["sentence"] == ("In this 1.0 h window the top SQL ran 200 ms in all: 0.0556 ms of SQL per second, "
                                     "under 0.01% of one session's time; 16.7 commits/s.")
    ch = c["sql"]["changed"][0]
    assert ch["mean_pct"] == 100.0 and ch["ms_per_s"] == 0.055556 and ch["reference"]["ms_per_s"] == 0.027778
    assert ch["added_ms_per_s"] == 0.027778 and "not a running total" in c["counts"]


def test_the_rules_call_a_regression_that_costs_nothing_an_efficiency_issue():
    """Twice as slow per call at 100% of SQL time reads as a regression, but on this database it is 0.06 ms of SQL a
    second and 5 ms a call: low, and the evidence and impact say why. The same doubling on a busy database stays."""
    from dbtriage.agent import EFFICIENCY_IMPACT, rule_based_triage

    before, base, cur = _pair()
    f = next(x for x in compare(cur, base, before)["findings"] if x["kind"] == "sql_regressed")
    assert f["severity"] == "low" and f["evidence"]["efficiency"].startswith("efficiency, not impact: it costs 0.0556 ms")
    review = rule_based_triage(compare(cur, base, before), "postgres")
    assert review["findings"][0]["impact"] == EFFICIENCY_IMPACT and any(e.startswith("efficiency, not impact") for e in review["findings"][0]["evidence"])
    busy = [snap(m, {"q1": (n, t)}) for m, n, t in ((0, 100000, 400000), (60, 140000, 2000000), (120, 180000, 5200000))]
    f = next(x for x in compare(busy[2], busy[1], busy[0])["findings"] if x["kind"] == "sql_regressed")
    assert f["severity"] != "low" and "efficiency" not in f["evidence"]  # 889 ms of SQL a second: callers feel it


def test_numbers_no_tool_showed():
    seen = grounding.pool(['{"idx_scan": 44, "share_of_time": 0.628, "total_time_ms": 4851.2, "current_per_s": 1.067}'])
    assert grounding.unseen(["idx_scan increased from 0 to 2127"], seen) == ["2127"]  # small counts like 0 are left out
    assert grounding.unseen(["44 scans, 62.8% of SQL time, 4.85 s in all, 1.067/s"], seen) == []  # ratio as %, ms as s
    assert grounding.unseen(["2,127 and 2,127 and 9999.5"], seen) == ["2,127", "9999.5"]


def _store(tmp_path):
    st = LocalStorage(tmp_path)
    ids = []
    for minutes, s in zip((0, 60, 120), _pair()):
        sid = f"postgres/ts/2026-11-28/202611281{minutes // 60}0000Z"
        st.put_json(f"{sid}/summary.json", s)
        st.put_json(f"{sid}/manifest.json", {"id": sid, "engine": "postgres", "target": "ts", "captured_at": s["captured_at"], "reports": []})
        ids.append(sid)
    return st, ids


WRONG = {"severity": "high", "kind": "other", "subject": "_hyper_24_1242_chunk", "where": "_hyper_24_1242_chunk",
         "summary": "Massive spike in index scans", "impact": "I/O pressure", "evidence": ["idx_scan increased from 0 to 2127."], "fix": "look"}
RIGHT = dict(WRONG, severity="low", summary="One chunk takes the hypertable's index scans",
             impact="Efficiency only: 44 index scans in the hour on an idle database.", evidence=["idx_scan 44 in the window (0.0122/s)."])


def test_the_first_submit_is_held_and_the_second_records(tmp_path):
    """The model sees its evidence beside the measured lines and the load, corrects the finding, and submits again;
    the stored finding carries the measured lines, which the model cannot write itself."""
    st, ids = _store(tmp_path)
    sheets = []

    class Corrects:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            by = {t.name: t.fn for t in tools}
            by["compare_snapshots"]({"snapshot_id": ids[-1], "baseline_id": ids[-2]})
            sheets.append(by["submit_review"]({"findings": [WRONG], "summary": "s", "checked": "c", "confidence": "high"}))
            assert not stop_when()  # held: the run goes on
            assert by["submit_review"]({"findings": [dict(RIGHT, measured=["forged"])], "summary": "s", "checked": "c", "confidence": "medium"}) == "recorded"
            assert stop_when()
            return llm.AgentResult(text="", stop_reason="submitted", model="m", tool_calls=3)

    r = TriageAgent(st, Corrects()).run("postgres", "ts", ids[-1], ids[-2], "scheduled")
    sheet = sheets[0]
    assert sheet.startswith("Held, not final yet") and "How busy the database was: In this 1.0 h window" in sheet
    assert "your evidence: idx_scan increased from 0 to 2127." in sheet and "idx_scan 44 (0.0122/s)" in sheet
    assert "idx_scan 2,083 at the baseline, 2,127 now" in sheet and "a running total is not a change" in sheet
    assert r["rechecked"] is True and r["severity"] == "low" and r["findings"][0]["evidence"] == RIGHT["evidence"]
    assert r["findings"][0]["measured"][0].startswith(f"{CHUNK} in this window") and "forged" not in str(r["findings"][0]["measured"])
    md = render_markdown(dict(r, target="ts", engine="postgres", snapshot=ids[-1]))
    assert f"   - **Measured:** {CHUNK} in this window" in md and "Not in the data" not in md  # 44 and 0.0122 were measured


def test_a_number_the_final_submission_made_up_is_shown_to_the_reader(tmp_path):
    """The check sheet repeats the model's own evidence, so it is not a source: a made-up number written before the
    check and kept after it is still flagged on the stored finding. (2127 would pass: it is the measured running
    total, and saying it was the change is what the measured lines show.)"""
    made_up = dict(WRONG, evidence=["idx_scan rose to 9,876 in the hour, from 2127."])
    st, ids = _store(tmp_path)

    class Keeps:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            by = {t.name: t.fn for t in tools}
            by["compare_snapshots"]({"snapshot_id": ids[-1], "baseline_id": ids[-2]})
            for _ in range(2):
                by["submit_review"]({"findings": [made_up], "summary": "s", "checked": "c", "confidence": "high"})
            return llm.AgentResult(text="", stop_reason="submitted", model="m", tool_calls=3)

    r = TriageAgent(st, Keeps()).run("postgres", "ts", ids[-1], ids[-2], "scheduled")
    assert r["rechecked"] is True and r["findings"][0]["not_in_data"] == ["9,876"]
    assert "   - **Not in the data:** 9,876" in render_markdown(dict(r, target="ts", engine="postgres", snapshot=ids[-1]))


def test_a_run_that_ends_after_the_first_submit_keeps_the_held_review(tmp_path):
    st, ids = _store(tmp_path)

    class Stops:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            next(t for t in tools if t.name == "submit_review").fn({"findings": [WRONG], "summary": "s", "checked": "c", "confidence": "high"})
            return llm.AgentResult(text="looks right", stop_reason="stop", model="m", tool_calls=1)

    r = TriageAgent(st, Stops()).run("postgres", "ts", ids[-1], ids[-2], "scheduled")
    assert r["source"] == "agent" and r["rechecked"] is False and r["severity"] == "high"
    assert "idx_scan 44" in r["findings"][0]["measured"][0]  # the reader still sees what was measured


def test_no_findings_records_at_once(tmp_path):
    st, ids = _store(tmp_path)
    agent = TriageAgent(st, None)
    agent.snapshot_id, agent._engine, agent._target, agent._baseline_id = ids[-1], "postgres", "ts", ids[-2]
    submit = next(t for t in agent._tools("postgres", "ts") if t.name == "submit_review")
    assert submit.call({"findings": [], "summary": "quiet", "checked": "c", "confidence": "high"}) == "recorded"
    assert agent.final and "rechecked" not in agent.result
