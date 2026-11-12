"""Exercise the agent's tools against stored snapshots without calling the model."""
import json

from dbtriage.agent import TriageAgent, rule_based_triage
from dbtriage.storage import LocalStorage
from tests.test_compare import snap


def _store(tmp_path):
    st = LocalStorage(tmp_path)
    for m, sql in ((0, {"q1": (0, 0)}), (10, {"q1": (1000, 10000)}), (20, {"q1": (2000, 60000)})):
        s = snap(m, sql)
        sid = f"postgres/demo/2026-11-05/2026110510{m:02d}00Z"
        st.put_json(f"{sid}/summary.json", s)
        st.put_json(f"{sid}/manifest.json", {"id": sid, "engine": "postgres", "target": "demo", "captured_at": s["captured_at"], "reports": ["awr.txt"],
                                             "changes_file": str(tmp_path / "changes.json")})
        st.put_text(f"{sid}/awr.txt", "AWR REPORT " * 100)
    (tmp_path / "changes.json").write_text(json.dumps([
        {"time": "2026-11-05T09:30:00+00:00", "kind": "deploy", "description": "v2"},
        {"time": "2026-10-01T09:30:00+00:00", "kind": "deploy", "description": "old"}]))
    return st


def test_tools_read_compare_history_changes_submit(tmp_path):
    st = _store(tmp_path)
    agent = TriageAgent(st, backend=None)
    tools = {t.name: t for t in agent._tools("postgres", "demo")}
    ids = st.list_snapshots("postgres", "demo")

    lp = json.loads(tools["get_report"].call({"snapshot_id": ids[-1], "section": "load_profile"}))
    assert lp["xact_commit"] == 20000
    assert "unknown section" in tools["get_report"].call({"snapshot_id": ids[-1], "section": "nope"})

    raw = tools["get_raw_report"].call({"snapshot_id": ids[-1], "name": "awr.txt", "offset": 0, "length": 20})
    assert raw.startswith("[chars 0-20 of 1100]")
    assert "available" in tools["get_raw_report"].call({"snapshot_id": ids[-1], "name": "missing.txt"})

    cmp = json.loads(tools["compare_snapshots"].call({"snapshot_id": ids[-1], "baseline_id": ids[-2]}))
    assert cmp["reference"]["snapshot"] == ids[-2] and any(f["kind"] == "sql_regressed" for f in cmp["findings"])

    hist = json.loads(tools["get_sql_history"].call({"sql_id": "q1", "limit": 5}))
    assert [h["snapshot"] for h in hist] == list(reversed(ids))

    ch = json.loads(tools["get_recent_changes"].call({"snapshot_id": ids[-1], "hours": 48}))
    assert [c["description"] for c in ch] == ["v2"]

    bad = {"severity": "critical", "kind": "sql_regressed", "subject": "q1", "where": "q1", "summary": "s", "impact": "i", "evidence": [], "fix": "f"}
    assert "severity must be" in tools["submit_review"].call({"findings": [bad], "summary": "s", "checked": "c", "confidence": "low"})
    odd = dict(bad, severity="low", kind="slow_query")
    assert "kind must be one of" in tools["submit_review"].call({"findings": [odd], "summary": "s", "checked": "c", "confidence": "low"})
    low = dict(bad, severity="low"); high = dict(bad, severity="high", summary="High one")
    assert tools["submit_review"].call({"findings": [low, high], "summary": "s", "checked": "c", "confidence": "high"}) == "recorded"
    assert agent.result["severity"] == "high" and agent.result["title"] == "High one"
    assert [f["severity"] for f in agent.result["findings"]] == ["high", "low"]  # sorted most severe first

    rules = rule_based_triage(cmp)
    assert rules["severity"] == cmp["severity"] and rules["source"] == "rules"
    assert rules["findings"][0]["fix"] and rules["findings"][0]["where"].startswith("sql_id ")
    assert rules["findings"][0]["kind"] == "sql_regressed" and rules["findings"][0]["where"] == f"sql_id {rules['findings'][0]['subject']}"


def test_run_stores_the_tool_transcript(tmp_path):
    """tools.json next to triage.json: every call in order, with its arguments and a truncated result."""
    from dbtriage import llm

    st = _store(tmp_path)
    ids = st.list_snapshots("postgres", "demo")
    st.put_text(f"{ids[-1]}/awr.txt", "AWR REPORT " * 500)  # 5500 chars: longer than one stored result may be

    class Scripted:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            by = {t.name: t.fn for t in tools}
            by["compare_snapshots"]({"snapshot_id": ids[-1], "baseline_id": ids[-2]})
            by["get_raw_report"]({"snapshot_id": ids[-1], "name": "awr.txt", "offset": 0, "length": 5000})
            by["get_raw_report"]({"snapshot_id": ids[-1], "name": "missing.txt"})
            finding = {"severity": "high", "kind": "sql_regressed", "subject": "q1", "where": "sql_id q1", "summary": "Slow", "impact": "i", "evidence": [], "fix": "f"}
            by["submit_review"]({"findings": [finding], "summary": "s", "checked": "c", "confidence": "high"})
            return llm.AgentResult(text="", stop_reason="tool_use", model="m", tool_calls=4)

    result = TriageAgent(st, Scripted()).run("postgres", "demo", ids[-1], ids[-2], "adhoc")
    assert result["tool_calls"] == 4 and result["tools_file"] == "tools.json"
    log = st.get_json(f"{ids[-1]}/tools.json")
    assert log["snapshot"] == ids[-1] and log["provider"] == "fake" and log["model"] == "m"
    calls = log["calls"]
    assert [c["n"] for c in calls] == [1, 2, 3, 4]
    assert [c["tool"] for c in calls] == ["compare_snapshots", "get_raw_report", "get_raw_report", "submit_review"]
    assert calls[0]["args"] == {"snapshot_id": ids[-1], "baseline_id": ids[-2]} and all(isinstance(c["ms"], int) for c in calls)
    long = calls[1]["result"]
    assert long.startswith("[chars 0-5000 of 5500]") and long.endswith("... (5023 chars)") and len(long) < 2100
    assert calls[2]["result"].startswith("no report 'missing.txt'") and calls[3]["result"] == "recorded"  # short results are kept whole


def test_triage_falls_back_to_rules_when_provider_fails(tmp_path, monkeypatch, capsys):
    from dbtriage import cli
    from dbtriage.config import Config, Target

    st = _store(tmp_path)
    cfg = Config(storage_url="x", targets={"demo": Target("demo", "postgres", "dsn")}, agent={"provider": "groq", "model": "m"})
    monkeypatch.setenv("GROQ_API_KEY", "bad")

    class Boom:
        name = "groq"

        def run(self, *a, **k):
            raise RuntimeError("401 invalid api key")

    monkeypatch.setattr("dbtriage.llm.make_backend", lambda c: Boom())
    ids = st.list_snapshots("postgres", "demo")
    result, comparison = cli.triage(cfg, st, ids[-1], ids[-2], "adhoc", use_llm=True)
    assert result["source"] == "rules" and "401" in result["agent_error"] and comparison["findings"]
    assert result["agreement"] is None and st.get_json(f"{ids[-1]}/triage.json")["agreement"] is None  # rules only: nothing to agree with
    assert "using rules-based triage" in capsys.readouterr().err
    assert "agent unavailable" in st.get(f"{ids[-1]}/triage.md").decode()


def test_render_review_markdown():
    from dbtriage.report import render_markdown

    t = {"target": "demo", "engine": "postgres", "mode": "incident", "severity": "high", "summary": "One hot query.",
         "checked": "compare, top SQL", "confidence": "high", "source": "agent", "provider": "ollama", "model": "m",
         "tool_calls": 3, "usage": {"input_tokens": 1200, "output_tokens": 50}, "duration_s": 4.2, "snapshot": "s2", "baseline": "s1",
         "findings": [{"severity": "high", "where": "sql_id q1", "summary": "Full scan", "impact": "slow", "evidence": ["99%"], "fix": "index"},
                      {"severity": "low", "where": "table t", "summary": "Bloat", "impact": "", "evidence": [], "fix": "vacuum"}]}
    md = render_markdown(t, {"findings": [1, 2]})
    assert md.startswith("🔴 **DB review · demo**")
    assert "**2 findings**: 1 high, 1 low" in md
    assert "1. 🔴 **High** · `sql_id q1`: Full scan" in md and "   - **Fix:** index" in md
    assert "2. 🟡 **Low** · `table t`: Bloat" in md and "**Checked:** compare, top SQL" in md
    assert "ollama:m · 3 tool calls · 1,200 in / 50 out tokens · 4.2s · confidence high · 2 heuristic findings · snapshot `s2` vs `s1`" in md
    none = render_markdown(dict(t, severity="info", findings=[], source="rules"), None)
    assert "✅ **No significant change.**" in none and "rules-based (no model)" in none


def test_render_markdown_agreement_section():
    from dbtriage.report import render_markdown

    heur = [{"severity": "high", "kind": "deadlocks", "title": "4 deadlocks in the window", "evidence": {"deadlocks": 4}},
            {"severity": "medium", "kind": "sql_regressed", "title": "q1 regressed", "evidence": {"sql_id": "q1"}},
            {"severity": "low", "kind": "bloat", "title": "t is bloated", "evidence": {"table": "t"}}]
    t = {"target": "demo", "engine": "postgres", "severity": "medium", "source": "agent", "snapshot": "s2", "confidence": "high",
         "findings": [{"severity": "medium", "kind": "sql_regressed", "subject": "q1", "where": "q1", "summary": "slow"},
                      {"severity": "low", "kind": "other", "subject": "", "where": "x", "summary": "added"}],
         "heuristics": heur,
         "agreement": [{"mark": "both", "kind": "sql_regressed", "subject": "q1", "model": 0, "rules": 1, "severity": "medium"},
                       {"mark": "model_only", "kind": "other", "subject": "", "model": 1, "rules": None, "severity": "low"},
                       {"mark": "rules_only", "kind": "deadlocks", "subject": "", "model": None, "rules": 0, "severity": "high"},
                       {"mark": "rules_only", "kind": "bloat", "subject": "t", "model": None, "rules": 2, "severity": "low"}]}
    md = render_markdown(t, {"findings": heur})
    assert "**Agreement:** 1 both, 1 model only, 2 rules only" in md
    clean = render_markdown(dict(t, findings=[], heuristics=[], agreement=[], severity="info"), {"findings": []})
    assert "Agreement" not in clean  # nothing to agree about: no line of zeros
    assert "- ⚠️ rules found, not in the review: 🔴 **High** · 4 deadlocks in the window" in md
    assert "t is bloated" not in md and "q1 regressed" not in md  # low and matched ones stay out of Slack
    assert md.index("**Agreement:**") < md.index("confidence high · 3 heuristic findings")  # before the footer
    assert "Agreement" not in render_markdown(dict(t, source="rules", agreement=None), None)
    assert "Agreement" not in render_markdown({k: v for k, v in t.items() if k != "agreement"}, None)


def test_agent_settings_reach_the_reviewer(tmp_path, monkeypatch):
    """max_iterations from [agent] and a target's notes must reach the model, not just the settings page."""
    from dataclasses import replace

    from dbtriage import llm
    from dbtriage.cli import triage
    from dbtriage.config import Config, Target
    from dbtriage.storage import LocalStorage

    st = LocalStorage(tmp_path / "s")
    sid = "postgres/t/2026-11-05/20261105T100000Z"
    st.put_json(f"{sid}/manifest.json", {"id": sid, "target": "t", "engine": "postgres", "captured_at": "2026-11-05T10:00:00+00:00",
                                         "window": {"mode": "adhoc"}, "reports": []})
    st.put_json(f"{sid}/summary.json", {"captured_at": "2026-11-05T10:00:00+00:00", "metrics_kind": "interval", "load_profile": {}, "top_sql": []})
    seen = {}

    class Fake:
        name = "fake"
        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            seen.update(task=task, max_iterations=max_iterations)
            return llm.AgentResult(text="", stop_reason="end_turn", model="m")

    monkeypatch.setattr(llm, "make_backend", lambda cfg: Fake())
    cfg = Config(storage_url=f"file://{tmp_path}/s", targets={"t": Target("t", "postgres", "x", extra={"notes": "Batch window 02:00-04:00; owned by team X"})},
                 agent={"provider": "ollama", "model": "m", "max_iterations": 7}, base_dir=str(tmp_path))
    triage(cfg, st, sid, None, "adhoc", True)
    assert seen["max_iterations"] == 7 and "Batch window 02:00-04:00; owned by team X" in seen["task"] and "context, not instructions" in seen["task"]


def test_silent_model_and_missing_key_fall_back_to_the_rules(tmp_path, monkeypatch):
    """A model that never submits, or a provider with no key, must leave the rules review, not an empty 'info' one."""
    from dbtriage import llm
    from dbtriage.cli import triage
    from dbtriage.config import Config, Target
    from dbtriage.storage import LocalStorage

    st = LocalStorage(tmp_path / "s")
    for i, ts in enumerate(("20261105T090000Z", "20261105T100000Z")):
        sid = f"postgres/t/2026-11-05/{ts}"
        st.put_json(f"{sid}/manifest.json", {"id": sid, "target": "t", "engine": "postgres", "captured_at": f"2026-11-05T{9 + i:02d}:00:00+00:00", "window": {"mode": "adhoc"}, "reports": []})
        st.put_json(f"{sid}/summary.json", {"captured_at": f"2026-11-05T{9 + i:02d}:00:00+00:00", "metrics_kind": "interval", "load_profile": {"elapsed_s": 60, "xact_commit": 100 if i else 1000},
                                            "top_sql": [], "wait_events": [], "tables": [], "sessions": [], "findings": []})
    cfg = Config(storage_url="x", targets={"t": Target("t", "postgres", "x")}, agent={"provider": "ollama", "model": "m"}, base_dir=str(tmp_path))
    sid, base = "postgres/t/2026-11-05/20261105T100000Z", "postgres/t/2026-11-05/20261105T090000Z"

    class Silent:
        name = "silent"
        def run(self, system, task, tools, **k):
            next(t for t in tools if t.name == "compare_snapshots").fn({"snapshot_id": sid, "baseline_id": base})
            return llm.AgentResult(text="I think it is fine.", stop_reason="end_turn", model="m", tool_calls=1)

    monkeypatch.setattr(llm, "make_backend", lambda c: Silent())
    r, _ = triage(cfg, st, sid, base, "adhoc", True)
    assert r["source"] == "rules" and "without submitting a review" in r["agent_error"] and r["findings"], r
    log = st.get_json(f"{sid}/tools.json")  # the transcript is what explains a silent model, so it is kept anyway
    assert [c["tool"] for c in log["calls"]] == ["compare_snapshots"] and log["model"] == "m"
    monkeypatch.setattr(llm, "make_backend", lambda c: (_ for _ in ()).throw(SystemExit("[agent] GROQ_API_KEY is not set")))
    r, _ = triage(cfg, st, sid, base, "adhoc", True)
    assert r["source"] == "rules" and "GROQ_API_KEY is not set" in r["agent_error"]


def test_changes_come_from_the_store_and_legacy_snapshots_from_the_file(tmp_path):
    """The feed stored with the snapshot wins; a snapshot from before the feed was stored still reads its changes_file."""
    st = _store(tmp_path)
    ids = st.list_snapshots("postgres", "demo")
    st.put_json(f"{ids[-1]}/changes.json", [
        {"time": "2026-11-05T10:04:00+00:00", "kind": "deploy", "description": "billing@1.4.2 to production", "link": None, "source": "github"},
        {"time": "2026-11-05T10:19:00+00:00", "kind": "other", "description": "github: HTTP Error 401: Unauthorized", "link": None, "source": "github", "error": True},
        {"time": "2026-11-02T10:00:00+00:00", "kind": "deploy", "description": "three days ago", "link": None, "source": "github"}])
    m = st.get_json(f"{ids[-1]}/manifest.json")
    st.put_json(f"{ids[-1]}/manifest.json", {**m, "changes_sources": ["github"], "correlate_minutes": 15})
    agent = TriageAgent(st, backend=None)
    tools = {t.name: t for t in agent._tools("postgres", "demo")}

    ch = json.loads(tools["get_recent_changes"].call({"snapshot_id": ids[-1], "hours": 48}))
    assert [c["description"] for c in ch] == ["billing@1.4.2 to production", "github: HTTP Error 401: Unauthorized"]  # not the file's "v2"
    assert ch[1]["error"] is True
    assert [c["description"] for c in json.loads(tools["get_recent_changes"].call({"snapshot_id": ids[-1], "hours": 100}))][0] == "three days ago"
    legacy = json.loads(tools["get_recent_changes"].call({"snapshot_id": ids[-2], "hours": 48}))  # manifest has changes_file, no changes.json
    assert [c["description"] for c in legacy] == ["v2"] and legacy[0]["source"] == "file"
    st.put_json(f"{ids[0]}/manifest.json", {k: v for k, v in st.get_json(f"{ids[0]}/manifest.json").items() if k != "changes_file"})
    assert json.loads(tools["get_recent_changes"].call({"snapshot_id": ids[0], "hours": 48})) == []

    cmp = json.loads(tools["compare_snapshots"].call({"snapshot_id": ids[-1], "baseline_id": ids[-2]}))  # window 10:10-10:20
    reg = next(f for f in cmp["findings"] if f["kind"] == "sql_regressed")
    assert reg["evidence"]["changes"] == [{"time": "2026-11-05T10:04:00+00:00", "kind": "deploy", "description": "billing@1.4.2 to production"}]
    assert cmp["changes"][0]["source"] == "github" and len(cmp["changes"]) == 1  # the feed error is not a change
    st.put_json(f"{ids[-1]}/manifest.json", {**m, "correlate_minutes": 5})  # the margin is the one recorded at capture
    cmp5 = json.loads(tools["compare_snapshots"].call({"snapshot_id": ids[-1], "baseline_id": ids[-2]}))
    assert cmp5["changes"] == [] and "changes" not in next(f for f in cmp5["findings"] if f["kind"] == "sql_regressed")["evidence"]
    st.put_json(f"{ids[-1]}/manifest.json", {**m, "correlate_minutes": 0})  # an explicit 0 is "inside the window only", not the 30-minute default
    cmp0 = json.loads(tools["compare_snapshots"].call({"snapshot_id": ids[-1], "baseline_id": ids[-2]}))
    assert cmp0["changes"] == []

    rules = rule_based_triage(cmp)
    reg = next(f for f in rules["findings"] if f["kind"] == "sql_regressed")
    assert reg["evidence"][-1] == "deploy billing@1.4.2 to production landed 6 minutes before the window"
    assert not any(e.startswith("changes:") for e in reg["evidence"])


def test_change_lines_wording():
    from dbtriage.agent import _change_lines, _evidence_lines

    start = "2026-11-05T10:10:00+00:00"
    found = [{"time": "2026-11-05T10:09:00+00:00", "kind": "deploy", "description": "a@1"},
             {"time": "2026-11-05T10:10:00+00:00", "kind": "job", "description": "stats"},
             {"time": "2026-11-05T10:12:00+00:00", "kind": "failover", "description": "to replica 2"},
             {"time": "2026-11-05T10:13:00+00:00", "kind": "deploy", "description": "fourth"}]
    assert _change_lines(found, start) == ["deploy a@1 landed 1 minute before the window", "job stats landed at the start of the window",
                                           "failover to replica 2 landed 2 minutes into the window"]
    assert _change_lines([{"kind": "deploy", "description": "undated"}], start) == ["deploy undated landed"]
    assert _change_lines(found[:1], None) == ["deploy a@1 landed at 2026-11-05T10:09:00+00:00"]
    ev = {"sql_id": "q1", "a": 1, "b": 2, "c": 3, "d": 4, "settings_changed": ["jit"], "changes": found[:1]}
    lines = _evidence_lines(ev, window_start=start)  # three plain lines, then the settings change, then the deploy: both always make the cut
    assert lines[:2] == ["a: 1", "b: 2"] and lines[2:] == ['settings_changed: ["jit"]', "deploy a@1 landed 1 minute before the window"]


def test_compare_tool_passes_the_usual_band(tmp_path, monkeypatch):
    """compare_snapshots computes the band for the target's band_weeks and hands it to compare; 0 leaves it out."""
    from dbtriage import band as usual

    st = _store(tmp_path)
    ids = st.list_snapshots("postgres", "demo")
    asked = []
    fake = {"label": "Thursday 10:20 UTC", "weeks": 6, "weeks_used": 5, "high_is": "p90", "window_s": 600, "skipped": [{"week": 2, "reason": "x"}],
            "metrics": {"sql_time_ms": {"median": 20.0, "high": 30.0, "low": 15.0, "max": 40.0, "min": 10.0, "n": 5}}}

    def usual_band(store, engine, target, sid, weeks=6, hours_tolerance=1, baseline=None):
        asked.append((engine, target, sid, weeks, baseline))
        return fake

    monkeypatch.setattr(usual, "usual_band", usual_band)
    agent = TriageAgent(st, backend=None)
    agent.band_weeks = 8
    tools = {t.name: t for t in agent._tools("postgres", "demo")}
    cmp = json.loads(tools["compare_snapshots"].call({"snapshot_id": ids[-1], "baseline_id": ids[-2]}))
    assert asked == [("postgres", "demo", ids[-1], 8, ids[-2])]  # the band's hour is the baseline -> current interval
    assert cmp["band"]["applies"] is True and cmp["band"]["weeks_used"] == 5 and cmp["band"]["skipped"] == [{"week": 2, "reason": "x"}]
    reg = next(f for f in cmp["findings"] if f["kind"] == "sql_regressed")
    assert reg["evidence"]["band"].startswith("total SQL time ") and "in 5 of 6 weeks" in reg["evidence"]["band"]
    rules = rule_based_triage(cmp)
    assert reg["evidence"]["band"] in rules["findings"][0]["evidence"]  # the line survives the three-line cut
    assert rules["summary"].endswith(f"The interval is heavier than usual: {cmp['band']['sentence']}.")
    from dbtriage.report import render_markdown

    md = render_markdown(dict(rules, snapshot=ids[-1], baseline=ids[-2], target="demo", engine="postgres"), cmp)
    assert f"_Usual band: {cmp['band']['sentence']}_" in md
    agent.band_weeks = 0
    assert json.loads(tools["compare_snapshots"].call({"snapshot_id": ids[-1], "baseline_id": ids[-2]}))["band"] is None
    assert asked == [("postgres", "demo", ids[-1], 8, ids[-2])]  # not asked again
    # a front end that builds the tools without run() passes the lookup by target name
    by_name = TriageAgent(st, backend=None, band_weeks_for=lambda name: {"demo": 12}[name])
    tools = {t.name: t for t in by_name._tools("postgres", "demo")}
    tools["compare_snapshots"].call({"snapshot_id": ids[-1], "baseline_id": ids[-2]})
    assert asked[-1][3] == 12


def test_evidence_lines_keep_the_band_sentence():
    from dbtriage.agent import _evidence_lines

    line = "commits 1/s is inside the usual Monday 02:00 UTC band (6 weeks): median 1/s, high 2/s"
    ev = {"sql_id": "q1", "a": 1, "b": 2, "c": 3, "d": 4, "band": line}
    assert _evidence_lines(ev) == ["a: 1", "b: 2", line]  # the sentence as it is, never cut, in place of the third plain line
    ev["settings_changed"] = ["jit"]
    assert _evidence_lines(ev) == ["a: 1", line, 'settings_changed: ["jit"]']  # both kept, one plain line left


def test_operator_answers_tool_reads_this_and_the_previous_snapshot(tmp_path):
    """A result a person pasted for a proposed read yesterday is still evidence today: the tool returns the answers
    stored with the snapshot and with the previous one of the same target, each marked as supplied by a person."""
    from dbtriage import chat
    from dbtriage.agent import SYSTEM

    st = _store(tmp_path)
    ids = st.list_snapshots("postgres", "demo")
    tools = {t.name: t for t in TriageAgent(st, backend=None)._tools("postgres", "demo")}
    assert tools["get_operator_answers"].call({"snapshot_id": ids[-1]}) == f"no results supplied by a person for {ids[-1]} or the previous snapshot {ids[-2]}"
    assert tools["get_operator_answers"].call({"snapshot_id": ids[0]}) == f"no results supplied by a person for {ids[0]}"
    for sid in (ids[-2], ids[-1]):
        st.put_json(chat.thread_path(sid, "review"), {"snapshot": sid, "key": "review", "messages": [
            {"role": "user", "text": "what to run?", "at": "t"},
            {"role": "assistant", "text": "Look for a seq scan:\n```proposed-read\nEXPLAIN SELECT 1;\n```", "at": "t",
             "proposals": [{"n": 1, "statement": "EXPLAIN SELECT 1;", "note": "Look for a seq scan:"}]}]})
        chat.Chat(st, None).answer(sid, "review", 1, f"Seq Scan {sid[-3:]} Filter: (x = 'y')")
    out = json.loads(tools["get_operator_answers"].call({"snapshot_id": ids[-1]}))
    assert "supplied by a person" in out["note"] and [a["snapshot"] for a in out["answers"]] == [ids[-1], ids[-2]]
    a = out["answers"][0]
    assert a["statement"] == "EXPLAIN SELECT 1;" and a["look_for"] == "Look for a seq scan:" and a["result"] == f"Seq Scan {ids[-1][-3:]} Filter: (x = '?')"
    assert a["supplied_by"] == chat.SUPPLIED_BY and a["thread"] == "review" and a["proposal"] == 1
    assert [a["snapshot"] for a in json.loads(tools["get_operator_answers"].call({"snapshot_id": ids[-2]}))["answers"]] == [ids[-2]]  # ids[0] has none
    assert "get_operator_answers" in SYSTEM and "supplied by a person, never as something Second Look measured" in SYSTEM


# ---------------------------------------------------------------- the fix as a change proposal
def test_a_proposal_needs_a_rollback_and_must_cite_the_current_snapshot(tmp_path):
    st = _store(tmp_path)
    agent = TriageAgent(st, backend=None)
    ids = st.list_snapshots("postgres", "demo")
    agent.snapshot_id = ids[-1]  # what run() sets
    tools = {t.name: t for t in agent._tools("postgres", "demo")}
    base = {"severity": "high", "kind": "seq_scans", "subject": "public.t", "where": "table public.t", "summary": "s", "impact": "i", "evidence": ["e"], "fix": "f"}
    prop = {"statement": "CREATE INDEX CONCURRENTLY t_a_idx ON public.t (a);", "rollback": "", "runner": "DBA in a change window",
            "window": "tonight", "justified_by": [ids[-1], ids[-2]]}

    def submit(f):
        return tools["submit_review"].call({"findings": [f], "summary": "s", "checked": "c", "confidence": "high"})

    out = submit(dict(base, proposal=prop))
    assert out.startswith("finding 1: the proposal changes the database but has no rollback") and "CREATE INDEX" in out and agent.result is None
    out = submit(dict(base, proposal=dict(prop, rollback="DROP INDEX CONCURRENTLY t_a_idx;", justified_by=[ids[-2]])))
    assert f"justified_by must include the current snapshot {ids[-1]}" in out and agent.result is None
    assert "has no statement" in submit(dict(base, proposal=dict(prop, statement="  ", rollback="x")))
    ok = dict(prop, rollback="DROP INDEX CONCURRENTLY t_a_idx;")
    assert submit(dict(base, proposal=ok)) == "recorded" and agent.result["findings"][0]["proposal"] == ok
    assert submit(base) == "recorded" and "proposal" not in agent.result["findings"][0]  # no proposal: the shape as it was
    agent.snapshot_id = None  # no run (chat, MCP): the current id is unknown, so only the rollback is required
    assert submit(dict(base, proposal=dict(ok, justified_by=["other"]))) == "recorded"
    spec = next(s for s in agent.tool_specs("postgres", "demo") if s.name == "submit_review")
    schema = json.dumps(spec.input_schema)
    assert "$ref" not in schema and '"proposal"' in schema and '"justified_by"' in schema  # inlined for every backend


def test_rules_proposals_per_kind_and_engine():
    from dbtriage.agent import COLUMNS_PLACEHOLDER

    def comp(engine, *findings, reference=True):
        cur, ref = f"{engine}/t/2026-11-05/20261105T100000Z", f"{engine}/t/2026-11-05/20261105T090000Z"
        return {"severity": "low", "current": {"snapshot": cur}, "reference": {"snapshot": ref} if reference else None,
                "findings": [dict(f, severity="low", detail="") for f in findings]}

    def rule(kind, **ev):
        return {"kind": kind, "title": f"{kind} x", "evidence": ev}

    stale, bloat = rule("stale_stats", table="APP.ORDERS"), rule("bloat", table="public.t", dead_rows=5, live_rows=10)
    scans, setting = rule("seq_scans", table="public.t", seq_scan=5), rule("setting_changed", parameter="work_mem", before="4MB", after="64MB")
    pg = rule_based_triage(comp("postgres", stale, bloat, scans, setting, rule("sql_regressed", sql_id="q1")))["findings"]
    ps = [f.get("proposal") for f in pg]
    assert ps[0]["statement"] == "ANALYZE APP.ORDERS;" and "no statistics history" in ps[0]["rollback"]
    assert ps[1]["statement"] == "VACUUM (ANALYZE, VERBOSE) public.t;" and ps[1]["rollback"].startswith("Nothing to undo")
    assert ps[2]["statement"] == f"CREATE INDEX CONCURRENTLY t_<columns>_idx ON public.t ({COLUMNS_PLACEHOLDER});"
    assert ps[2]["rollback"] == "DROP INDEX CONCURRENTLY t_<columns>_idx;" and ps[2]["runner"] == "DBA in a change window"
    assert ps[3]["statement"] == "ALTER SYSTEM SET work_mem = '4MB'; SELECT pg_reload_conf();"
    assert ps[3]["rollback"] == "ALTER SYSTEM SET work_mem = '64MB'; SELECT pg_reload_conf();"
    assert ps[4] is None and "proposal" not in pg[4]  # a regression says what to look at, not what to run
    assert all(p["justified_by"] == ["postgres/t/2026-11-05/20261105T100000Z", "postgres/t/2026-11-05/20261105T090000Z"] for p in ps[:4])
    assert all(p["runner"] and p["window"] for p in ps[:4])
    named = rule_based_triage(comp("postgres", rule("seq_scans", table="public.t", columns=["a", "b"])))["findings"][0]["proposal"]
    assert named["statement"] == "CREATE INDEX CONCURRENTLY t_a_b_idx ON public.t (a, b);"  # columns in the evidence name the index
    # a first snapshot cites one id; the "N more settings changed" finding has no before and after: no proposal
    only = rule_based_triage(comp("postgres", setting, rule("setting_changed", parameters=["a", "b"]), reference=False))["findings"]
    assert only[0]["proposal"]["justified_by"] == ["postgres/t/2026-11-05/20261105T100000Z"] and "proposal" not in only[1]
    ora = rule_based_triage(comp("oracle", stale, bloat, scans, setting))["findings"]
    assert [f["proposal"]["statement"] for f in ora] == [
        "EXEC DBMS_STATS.GATHER_TABLE_STATS(ownname => 'APP', tabname => 'ORDERS', cascade => TRUE);",
        "ALTER TABLE public.t SHRINK SPACE CASCADE;",
        f"CREATE INDEX t_<columns>_idx ON public.t ({COLUMNS_PLACEHOLDER}) ONLINE;",
        "ALTER SYSTEM SET work_mem = '4MB' SCOPE=BOTH;"]
    assert "RESTORE_TABLE_STATS(ownname => 'APP', tabname => 'ORDERS'" in ora[0]["proposal"]["rollback"] and ora[2]["proposal"]["rollback"] == "DROP INDEX t_<columns>_idx;"
    my = rule_based_triage(comp("mysql", stale, bloat, scans, setting))["findings"]
    assert [f["proposal"]["statement"] for f in my] == [
        "ANALYZE TABLE APP.ORDERS;", "OPTIMIZE TABLE public.t;",
        f"ALTER TABLE public.t ADD INDEX t_<columns>_idx ({COLUMNS_PLACEHOLDER}), ALGORITHM=INPLACE, LOCK=NONE;", "SET PERSIST work_mem = '4MB';"]
    assert my[2]["proposal"]["rollback"] == "ALTER TABLE public.t DROP INDEX t_<columns>_idx;"
    ms = rule_based_triage(comp("sqlserver", stale, bloat, scans, rule("setting_changed", parameter="max degree of parallelism", before=0, after=4)))["findings"]
    assert [f["proposal"]["statement"] for f in ms] == [
        "UPDATE STATISTICS APP.ORDERS WITH FULLSCAN;", "ALTER INDEX ALL ON public.t REORGANIZE;",
        f"CREATE INDEX t_<columns>_idx ON public.t ({COLUMNS_PLACEHOLDER}) WITH (ONLINE = ON);",
        "EXEC sp_configure 'max degree of parallelism', 0; RECONFIGURE;"]
    assert ms[2]["proposal"]["rollback"] == "DROP INDEX t_<columns>_idx ON public.t;" and ms[3]["proposal"]["rollback"].endswith("', 4; RECONFIGURE;")
    # the engine argument wins over the id's first segment; an engine without its own form reads the PostgreSQL one
    assert rule_based_triage(comp("postgres", stale), engine="oracle")["findings"][0]["proposal"]["statement"].startswith("EXEC DBMS_STATS")
    assert rule_based_triage(comp("mariadb", stale))["findings"][0]["proposal"]["statement"] == "ANALYZE APP.ORDERS;"


def test_render_markdown_change_proposal():
    from dbtriage.report import proposal_lines, render_markdown

    p = {"statement": "CREATE INDEX CONCURRENTLY t_a_idx ON public.t (a);\n-- then ANALYZE", "rollback": "DROP INDEX CONCURRENTLY t_a_idx;",
         "runner": "DBA in a change window", "window": "tonight 02:00", "justified_by": ["s2", "s1"]}
    t = {"target": "demo", "engine": "postgres", "severity": "high", "source": "rules", "confidence": "low", "snapshot": "s2", "baseline": "s1",
         "findings": [{"severity": "high", "where": "table public.t", "summary": "Full scans", "impact": "slow", "evidence": ["seq_scan 500 vs 20"], "fix": "add an index", "proposal": p},
                      {"severity": "low", "where": "sql_id q1", "summary": "Slower", "impact": "", "evidence": [], "fix": "look"}]}
    md = render_markdown(t, None)
    block = md.split("   - **Fix:** add an index\n")[1].split("\n\n")[0]
    assert block.splitlines()[:5] == ["   - **Change proposal:**", "     ```", "     CREATE INDEX CONCURRENTLY t_a_idx ON public.t (a);", "     -- then ANALYZE", "     ```"]
    assert "     - **Undo:** DROP INDEX CONCURRENTLY t_a_idx;" in block and "     - **Run by:** DBA in a change window · tonight 02:00" in block
    assert "     - **Justified by:** `s2`, `s1`" in block and "_Second Look never applies changes" in block
    assert md.count("Change proposal") == 1 and "   - **Fix:** look\n\n" in md  # the finding without one renders as before
    assert proposal_lines(None) == [] and proposal_lines({"statement": ""}) == []
    assert proposal_lines({"statement": "ANALYZE t;", "rollback": "none needed"})[:4] == ["- **Change proposal:**", "  ```", "  ANALYZE t;", "  ```"]


def test_operator_answers_tool_is_bounded_newest_first(tmp_path, monkeypatch):
    """However busy the discussions, the tool returns at most OPERATOR_ANSWERS_MAX results and OPERATOR_ANSWERS_CHARS
    of text, newest first, and says how many it left out."""
    from dbtriage import agent as agent_mod, chat

    st = _store(tmp_path)
    sid = st.list_snapshots("postgres", "demo")[-1]
    msgs = [{"role": "assistant", "text": "x", "at": "t", "proposals": [{"n": 1, "statement": "EXPLAIN SELECT 1;", "note": ""}]}]
    msgs += [{"role": "operator", "text": f"result {i:02d}", "proposal": 1, "at": f"2026-11-12T10:{i:02d}:00+00:00"} for i in range(20)]
    st.put_json(chat.thread_path(sid, "review"), {"snapshot": sid, "key": "review", "messages": msgs})
    tool = {t.name: t for t in TriageAgent(st, backend=None)._tools("postgres", "demo")}["get_operator_answers"]
    out = json.loads(tool.call({"snapshot_id": sid}))
    assert len(out["answers"]) == agent_mod.OPERATOR_ANSWERS_MAX and out["answers"][0]["result"] == "result 19"  # newest first
    assert out["omitted"].startswith(f"{20 - agent_mod.OPERATOR_ANSWERS_MAX} older result(s) left out")
    monkeypatch.setattr(agent_mod, "OPERATOR_ANSWERS_CHARS", 20)  # two 9-character results fit, a third does not
    out = json.loads(tool.call({"snapshot_id": sid}))
    assert [a["result"] for a in out["answers"]] == ["result 19", "result 18"] and out["omitted"].startswith("18 older")
