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
