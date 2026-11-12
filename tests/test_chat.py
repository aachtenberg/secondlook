import json

from dbtriage import chat, llm
from dbtriage.storage import LocalStorage

SID = "postgres/demo/2026-11-05/20261105T100000Z"


def store(tmp_path):
    st = LocalStorage(tmp_path / "data")
    st.put_json(f"{SID}/manifest.json", {"id": SID, "target": "demo", "engine": "postgres", "captured_at": "2026-11-05T10:00:00+00:00",
                                         "window": {"mode": "adhoc"}, "reports": [], "baseline": {"snapshot": None}})
    st.put_json(f"{SID}/summary.json", {"captured_at": "2026-11-05T10:00:00+00:00", "top_sql": [{"sql_id": "q1", "calls": 3, "mean_time_ms": 7.5}]})
    st.put_json(f"{SID}/triage.json", {"severity": "high", "title": "q1 slow", "source": "agent",
                                       "findings": [{"severity": "high", "where": "sql_id q1", "summary": "q1 slow", "impact": "i", "evidence": ["e"], "fix": "f"},
                                                    {"severity": "low", "where": "table t", "summary": "bloat", "impact": "i", "evidence": [], "fix": "f"}]})
    st.put_text(f"{SID}/triage.md", "**2 findings**\n\n1. q1 slow\n   - **Fix:** f\n\n2. bloat\n")
    return st


class Fake:
    """Calls one tool, then answers with what it was asked and what it saw."""
    name = "fake"

    def __init__(self):
        self.tasks, self.systems = [], []

    def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
        self.systems.append(system); self.tasks.append(task)
        names = [t.name for t in tools]
        assert "submit_review" not in names and "get_sql_history" in names
        hist = json.loads(next(t for t in tools if t.name == "get_sql_history").fn({"sql_id": "q1"}))
        return llm.AgentResult(text=f"mean {hist[0]['mean_time_ms']} ms; you asked: {task.splitlines()[-1]}", stop_reason="end_turn",
                               model="m", usage={"input_tokens": 10, "output_tokens": 5}, tool_calls=1)


def test_the_tools_honour_the_targets_band_weeks(tmp_path, monkeypatch):
    """Chat builds the reviewer's tools without run(): compare_snapshots takes the band_weeks lookup the web view
    passes, so a target's 0 or 12 holds in a discussion too; without one, the default."""
    from dbtriage import band
    from tests.conftest import seed_store

    demo, base = "postgres/demo/2026-11-05/20261105T100000Z", "postgres/demo/2026-11-05/20261105T090000Z"
    st = seed_store(tmp_path / "data")
    asked = []

    def usual_band(store, engine, target, sid, weeks=6, hours_tolerance=1, baseline=None):
        asked.append((target, weeks))
        return None

    monkeypatch.setattr(band, "usual_band", usual_band)

    class Compares:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            next(t for t in tools if t.name == "compare_snapshots").fn({"snapshot_id": demo, "baseline_id": base})
            return llm.AgentResult(text="ok", stop_reason="end_turn", model="m", usage={}, tool_calls=1)

    chat.Chat(st, Compares, band_weeks_for=lambda name: {"demo": 12}[name]).ask(demo, None, "is this hour usual?")
    chat.Chat(st, Compares, band_weeks_for=lambda name: 0).ask(demo, None, "and now?")
    chat.Chat(st, Compares).ask(demo, None, "and by default?")
    assert asked == [("demo", 12), ("demo", 6)]


def test_the_ask_command_honours_the_targets_band_weeks(tmp_path, monkeypatch):
    """`dbtriage ask` built its Chat without the lookup, so a target's 0 still got band sentences and 12 read as 6:
    the command passes the target's band_weeks as the web view and the MCP server do."""
    from dbtriage import band, cli
    from tests.conftest import seed_store

    demo, base = "postgres/demo/2026-11-05/20261105T100000Z", "postgres/demo/2026-11-05/20261105T090000Z"
    seed_store(tmp_path / "data")
    asked = []

    def usual_band(store, engine, target, sid, weeks=6, hours_tolerance=1, baseline=None):
        asked.append((target, weeks))
        return None

    class Compares:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            next(t for t in tools if t.name == "compare_snapshots").fn({"snapshot_id": demo, "baseline_id": base})
            return llm.AgentResult(text="ok", stop_reason="end_turn", model="m", usage={}, tool_calls=1)

    monkeypatch.setattr(band, "usual_band", usual_band)
    monkeypatch.setattr(llm, "make_backend", lambda cfg: Compares())
    toml = tmp_path / "targets.toml"
    for weeks in (12, 0):
        toml.write_text(f'[storage]\nurl = "file://{tmp_path / "data"}"\n[targets.demo]\nengine = "postgres"\ndsn = "d"\nband_weeks = {weeks}\n')
        cli.main(["--config", str(toml), "ask", demo, "is this hour usual?"])
    assert asked == [("demo", 12)]  # 0 switches the band off: never asked


def test_thread_keys_and_context(tmp_path):
    st = store(tmp_path)
    c = chat.Chat(st, Fake)
    assert chat.thread_key(None) == "review" and chat.thread_key(2) == "finding-2"
    ctx = c.context(SID, 2)
    assert "The review as delivered:" in ctx and "bloat" in ctx and "about finding 2" in ctx and '"where": "table t"' in ctx
    assert "about finding" not in c.context(SID, None)
    try:
        c.context(SID, 3)
        assert False
    except ValueError as exc:
        assert "1..2" in str(exc)


def test_ask_persists_and_replays(tmp_path):
    st = store(tmp_path)
    fake = Fake()
    c = chat.Chat(st, lambda: fake)
    a = c.ask(SID, 1, "why is q1 slow?")
    assert a["role"] == "assistant" and a["text"].startswith("mean 7.5 ms") and "cite: why is q1 slow?" in a["text"]
    assert a["model"] == "m" and a["tool_calls"] == 1 and "10 in / 5 out tokens" in chat.footer(a) and "1 tool calls" in chat.footer(a)
    assert st.exists(f"{SID}/chat/finding-1.json")
    b = c.ask(SID, 1, "and the fix?")
    assert "Earlier in this discussion:" in fake.tasks[1] and "Engineer: why is q1 slow?" in fake.tasks[1] and "You: mean 7.5" in fake.tasks[1]
    assert "Earlier in this discussion" not in fake.tasks[0]
    t = chat.load_thread(st, SID, "finding-1")
    assert [m["role"] for m in t["messages"]] == ["user", "assistant", "user", "assistant"] and t["messages"][3]["text"] == b["text"]
    assert chat.list_threads(st, SID) == {"finding-1": 4}
    md = chat.thread_markdown(t)
    assert "**Engineer**" in md and "and the fix?" in md
    assert "no access to the live database" in fake.systems[0]


def test_ask_validation(tmp_path):
    st = store(tmp_path)
    c = chat.Chat(st, Fake)
    for bad in ("", "   "):
        try:
            c.ask(SID, None, bad); assert False
        except ValueError:
            pass
    try:
        c.ask(SID, None, "x" * (chat.MAX_QUESTION + 1)); assert False
    except ValueError as exc:
        assert "longer" in str(exc)
    try:
        chat.load_thread(st, SID, "../etc"); assert False
    except ValueError:
        pass
    try:
        c.ask("postgres/demo/2026-11-05/nope", None, "hi"); assert False
    except FileNotFoundError:
        pass
    # a refusal is stored as a visible non-answer, not an exception
    class Refuses(Fake):
        def run(self, *a, **k):
            return llm.AgentResult(text="", stop_reason="refusal", model="m", refused=True)
    a = chat.Chat(st, Refuses).ask(SID, None, "hi")
    assert "declined" in a["text"]



def test_target_thread_tells_the_model_about_the_whole_database(tmp_path):
    from dbtriage import ledger

    st = store(tmp_path)
    c = chat.Chat(st, Fake)
    ledger.save(st, "postgres", "demo", {"entries": [{"kind": "blocking", "subject": "", "title": "3 blocked", "severity": "high", "state": "worse",
                                                      "first_seen": SID, "first_seen_at": "2026-11-05T09:00:00+00:00", "tickets": {"jira": {"key": "DBA-1"}}}],
                                         "tombstones": [{"kind": "bloat", "subject": "t", "gone_at": "2026-11-04T09:00:00+00:00"}], "updated": SID, "prior": None, "generation": 0})
    ctx = c.context(SID, "target")
    assert "about the database as a whole" in ctx and "- Health:" in ctx and "- Open: 1 finding, worst high" in ctx
    assert "- blocking : high, worse, since 2026-11-05T09:00, tickets DBA-1 · 3 blocked" in ctx and "Cleared lately: bloat t (2026-11-04)" in ctx
    assert chat.KEY_RE.match("target") and chat.thread_path(SID, "target").endswith("/chat/target.json")


def test_proposed_reads_and_a_pasted_result(tmp_path):
    """An answer's proposed-read blocks are parsed and numbered through the thread; a result a person pastes back is
    stored masked and cut, labelled as supplied by a person, and replayed to the model as such."""

    class Proposes(Fake):
        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            self.systems.append(system); self.tasks.append(task)
            return llm.AgentResult(text="Look for a sequential scan on orders:\n\n```proposed-read\nEXPLAIN SELECT count(*) FROM orders WHERE status = 'open';\n```\n\n"
                                        "Then the waits:\n```proposed-read\nSELECT wait_event, count(*) FROM pg_stat_activity GROUP BY 1;\n```",
                                   stop_reason="end_turn", model="m", usage={}, tool_calls=0)

    st = store(tmp_path)
    fake = Proposes()
    c = chat.Chat(st, lambda: fake)
    a = c.ask(SID, 1, "what should I run?")
    assert a["proposals"] == [{"n": 1, "statement": "EXPLAIN SELECT count(*) FROM orders WHERE status = 'open';", "note": "Look for a sequential scan on orders:"},
                              {"n": 2, "statement": "SELECT wait_event, count(*) FROM pg_stat_activity GROUP BY 1;", "note": "Then the waits:"}]
    assert "```proposed-read" in fake.systems[0] and "never a statement that changes the database" in fake.systems[0]
    b = c.ask(SID, 1, "and more?")
    assert [p["n"] for p in b["proposals"]] == [3, 4]  # numbered through the thread, so a pasted result names one read
    r = c.answer(SID, "finding-1", 1, "Seq Scan on orders  (cost=0.00..1.00 rows=5)\n  Filter: (status = 'open')\nSELECT 1 FROM t WHERE id = 42\n")
    assert r == {"role": "operator", "text": "Seq Scan on orders  (cost=0.00..1.00 rows=5)\n  Filter: (status = '?')\nSELECT ? FROM t WHERE id = ?",
                 "proposal": 1, "at": r["at"]}  # quoted values masked, the echoed statement masked as SQL, the plan's numbers kept
    t = chat.load_thread(st, SID, "finding-1")
    assert [m["role"] for m in t["messages"]] == ["user", "assistant", "user", "assistant", "operator"]
    c.ask(SID, 1, "so?")
    assert "Result supplied by a person for proposed read 1 (not measured by Second Look):\nSeq Scan on orders" in fake.tasks[2]
    big = c.answer(SID, "finding-1", 2, "x" * (chat.MAX_RESULT + 5))
    assert big["truncated"] is True and big["text"] == "x" * chat.MAX_RESULT + "\n... (truncated)"
    for key, n, text in (("finding-1", 9, "r"), ("finding-1", 1, "  "), ("finding-2", 1, "r"), ("../x", 1, "r")):
        try:
            c.answer(SID, key, n, text); assert False
        except ValueError:
            pass
    try:
        c.answer("postgres/demo/2026-11-05/nope", "review", 1, "r"); assert False
    except FileNotFoundError:
        pass
    assert "**Person** (result for proposed read 1, supplied by a person)" in chat.thread_markdown(chat.load_thread(st, SID, "finding-1"))
    answers = chat.operator_answers(st, SID)
    assert [x["proposal"] for x in answers] == [1, 2] and answers[0]["statement"].startswith("EXPLAIN") and answers[0]["thread"] == "finding-1"
    assert answers[0]["look_for"] == "Look for a sequential scan on orders:" and answers[0]["supplied_by"] == chat.SUPPLIED_BY and answers[1]["truncated"] is True
    assert chat.operator_answers(st, "postgres/demo/2026-11-05/nope") == []
