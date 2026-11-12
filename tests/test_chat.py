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
