"""Judges: other models check a finished review against the same data; a finding short of the quorum is kept,
marked disputed, and does not alert (dbtriage.judge)."""
import json

import pytest

from dbtriage import deliver, judge, ledger, llm
from dbtriage.report import render_markdown
from tests.test_grounding import CHUNK, RIGHT, WRONG, _store

SLOW_SQL = {"severity": "medium", "kind": "sql_regressed", "subject": "q1", "where": "sql q1", "summary": "q1 doubled per call",
            "impact": "Callers wait longer.", "evidence": ["5 ms per call, from 2.5 ms."], "fix": "Compare the plans."}


def _review(*findings):
    fs = [dict(f) for f in findings]
    return {"severity": fs[0]["severity"] if fs else "info", "title": fs[0]["summary"] if fs else "no significant change",
            "summary": "s", "checked": "c", "confidence": "high", "findings": fs, "source": "agent", "provider": "ollama", "model": "gemma"}


def _verdict(name, *votes, missed=()):
    return {"judge": name, "model": name, "verdicts": [{"finding": i, "verdict": v, "severity": s, "reason": f"{name} says {v}"}
                                                     for i, (v, s) in enumerate(votes, 1)], "missed": list(missed), "note": "n"}


MISSED = {"severity": "high", "kind": "blocking", "subject": "pid 42", "where": "session 42", "summary": "Session 42 blocks 9 others",
          "impact": "Callers stall.", "evidence": ["9 sessions wait on pid 42."], "fix": "End its transaction."}


def test_a_finding_short_of_the_quorum_is_kept_disputed_and_the_severity_is_what_stands():
    r = _review(WRONG, SLOW_SQL)
    judge.combine(r, [_verdict("a", ("dispute", "low"), ("agree", "medium")), _verdict("b", ("agree", "low"), ("agree", "low"))], quorum=2)
    high, slow = r["findings"]
    assert high["disputed"] is True and [v["verdict"] for v in high["judges"]] == ["dispute", "agree"]
    assert "disputed" not in slow and slow["judges"][1] == {"judge": "b", "verdict": "agree", "severity": "low", "reason": "b says agree"}
    assert r["severity"] == "medium" and r["title"] == SLOW_SQL["summary"]  # the disputed high no longer sets the review's level
    assert r["judges"]["judged"] is True and r["judges"]["disputed"] == 1 and r["judges"]["quorum"] == 2
    # one of two judges is enough with quorum 1
    r1 = _review(WRONG)
    judge.combine(r1, [_verdict("a", ("dispute", "low")), _verdict("b", ("agree", "low"))], quorum=1)
    assert "disputed" not in r1["findings"][0] and r1["severity"] == "high"


def test_when_every_finding_is_disputed_nothing_pages():
    r = judge.combine(_review(WRONG), [_verdict("a", ("dispute", "low"))], quorum=1)
    assert r["severity"] == "info" and r["title"] == "1 finding disputed by the judges" and r["findings"][0]["disputed"]


def test_a_miss_named_by_the_quorum_is_added_and_one_short_of_it_is_a_note():
    other = dict(MISSED, severity="medium", kind="wait_shift", subject="Lock", summary="Lock waits rose")
    r = _review()  # a clean review
    judge.combine(r, [_verdict("a", missed=[MISSED, other]), _verdict("b", missed=[dict(MISSED, severity="medium", subject="PID 42 ")])], quorum=2)
    (added,) = r["findings"]
    assert added["kind"] == "blocking" and added["added_by"] == ["a", "b"] and added["severity"] == "medium"  # the more cautious one
    assert r["severity"] == "medium" and r["judges"]["added"] == 1
    assert r["judges"]["noted"] == [{"judges": ["a"], "severity": "medium", "kind": "wait_shift", "subject": "Lock", "summary": "Lock waits rose"}]
    # a "miss" the review already has is not added twice
    r2 = _review(SLOW_SQL)
    judge.combine(r2, [_verdict("a", ("agree", "medium"), missed=[dict(SLOW_SQL, severity="high")])], quorum=1)
    assert len(r2["findings"]) == 1 and r2["judges"]["noted"][0]["subject"] == "q1"


def test_too_few_judges_answering_leaves_the_review_as_written():
    r = _review(WRONG)
    judge.combine(r, [_verdict("a", ("dispute", "low")), {"judge": "b", "error": "RuntimeError: down"}], quorum=2)
    assert r["severity"] == "high" and "disputed" not in r["findings"][0] and "judges" not in r["findings"][0]
    assert r["judges"]["judged"] is False and r["judges"]["note"] == "1 of 2 judges answered; the quorum is 2, so the review stands as written"
    md = render_markdown(dict(r, target="ts", engine="postgres", snapshot="s"))
    assert "_Not judged: 1 of 2 judges answered; the quorum is 2, so the review stands as written. Judges: a, b (failed)._" in md


def test_judges_and_quorum_from_the_config():
    cfg = {"provider": "ollama", "model": "gemma", "base_url": "http://x", "token_budget": 90000, "api_key_cmd": "echo k",
           "judges": [{"provider": "openrouter", "model": "anthropic/claude-sonnet-5.5", "api_key_cmd": "rm -rf /"},
                      {"provider": "openrouter"}, "junk", {"provider": "groq", "model": "m", "token_budget": 50000}]}
    js = judge.judges_of(cfg)
    assert js == [{"token_budget": 90000, "provider": "openrouter", "model": "anthropic/claude-sonnet-5.5"},
                  {"token_budget": 50000, "provider": "groq", "model": "m"}]  # nothing of the main provider, never api_key_cmd
    assert [judge.quorum_of({}, n) for n in (1, 2, 3, 4)] == [1, 2, 2, 3]
    assert judge.quorum_of({"quorum": 5}, 2) == 2 and judge.quorum_of({"quorum": 1}, 3) == 1 and judge.quorum_of({"quorum": "x"}, 3) == 2
    assert judge.judge_review(_review(WRONG), None, "postgres", "ts", "s", None, "adhoc", None, {"judges": []}, 6)["severity"] == "high"


def test_a_judge_ignores_the_reviewers_model_override(monkeypatch):
    monkeypatch.setenv("DBTRIAGE_PROVIDER", "anthropic")
    monkeypatch.setenv("DBTRIAGE_MODEL", "the-reviewers-model")
    b = llm.make_backend({"provider": "ollama", "model": "judge-model", "base_url": "http://127.0.0.1:9/v1"}, env=False)
    assert (b.name, b.model) == ("ollama", "judge-model")


class Judging:
    """A judge as a model would act: read the comparison, get the verdict wrong once, then submit it."""
    name = "fake-judge"

    def __init__(self, ids, missed=()):
        self.ids, self.missed, self.tasks, self.replies = ids, list(missed), [], []

    def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
        assert require_tool == "submit_verdict" and "submit_review" not in [t.name for t in tools]
        self.tasks.append(task)
        by = {t.name: t.fn for t in tools}
        by["compare_snapshots"]({"snapshot_id": self.ids[-1], "baseline_id": self.ids[-2]})
        self.replies.append(by["submit_verdict"]({"verdicts": [], "missed": [], "note": "n"}))
        assert not stop_when()
        self.replies.append(by["submit_verdict"]({"verdicts": [{"finding": 1, "verdict": "dispute", "severity": "low",
                                                                 "reason": "idx_scan was 44 in the window; 2,127 is a running total."}],
                                                   "missed": self.missed, "note": "One finding misreads a running total."}))
        assert stop_when()
        return llm.AgentResult(text="", stop_reason="submitted", model="judge-1", tool_calls=3, usage={"input_tokens": 10, "output_tokens": 2})


def test_a_judge_reads_the_measured_lines_and_its_own_miss_is_checked_too(tmp_path):
    st, ids = _store(tmp_path)
    miss = dict(MISSED, kind="sql_volume", subject="q1", evidence=["q1 ran 40 calls in the window.", "It ran 9,876 calls an hour ago."])
    fake = Judging(ids, missed=[miss])
    r = judge.run_judge({"provider": "ollama", "model": "judge"}, st, _review(WRONG), "postgres", "ts", ids[-1], ids[-2], "scheduled", None, 6, backend=fake)
    task = fake.tasks[0]
    assert "Finding 1 · high · other _hyper_24_1242_chunk: Massive spike in index scans" in task
    assert "  evidence: idx_scan increased from 0 to 2127." in task and f"  measured by Second Look: {CHUNK} in this window" in task
    assert "written by the model ollama:gemma" in task and "How busy the database was: In this 1.0 h window" in task
    assert fake.replies[0] == "give exactly one verdict for each finding 1 to 1 (got none)" and fake.replies[1] == "recorded"
    assert r["judge"] == "ollama:judge" and r["model"] == "judge-1" and r["verdicts"][0]["verdict"] == "dispute"
    (m,) = r["missed"]
    assert m["measured"][0].startswith("SQL q1 in this window: 40 calls") and m["not_in_data"] == ["9,876"]  # its own numbers are checked too
    assert [c["tool"] for c in r["calls"]] == ["compare_snapshots", "submit_verdict", "submit_verdict"]


def test_a_judge_that_fails_is_recorded_and_never_raises(tmp_path):
    st, ids = _store(tmp_path)

    class Down:
        name = "down"

        def run(self, *a, **k):
            raise ConnectionError("no route")

    r = judge.run_judge({"provider": "openrouter", "model": "z"}, st, _review(), "postgres", "ts", ids[-1], ids[-2], "adhoc", None, 6, backend=Down())
    assert r["judge"] == "openrouter:z" and r["error"] == "ConnectionError: no route"


def test_judge_review_runs_them_together_and_stores_their_calls(tmp_path):
    st, ids = _store(tmp_path)
    cfg = {"judges": [{"provider": "ollama", "model": "a"}, {"provider": "ollama", "model": "b"}], "quorum": 1}
    r = judge.judge_review(_review(WRONG), st, "postgres", "ts", ids[-1], ids[-2], "scheduled", None, cfg, 6,
                           backends=[Judging(ids), Judging(ids)])
    assert r["findings"][0]["disputed"] is True and r["severity"] == "info"
    assert [j["judge"] for j in r["judges"]["judges"]] == ["ollama:a", "ollama:b"] and not any("calls" in j for j in r["judges"]["judges"])
    stored = st.get_json(f"{ids[-1]}/judges.json")
    assert stored["quorum"] == 1 and [c["tool"] for c in stored["judges"][0]["calls"]][0] == "compare_snapshots"


def test_a_disputed_finding_is_not_tracked_and_does_not_fire():
    led = {"entries": [], "tombstones": []}
    findings = [dict(WRONG, disputed=True), SLOW_SQL]
    out = ledger.apply(led, "postgres/ts/2026-11-28/20261128T140000Z", "2026-11-28T14:00:00+00:00",
                       [{"kind": "other", "subject": "_hyper_24_1242_chunk", "severity": "high", "title": "rules saw it too"}], findings)
    assert [l["state"] for l in out["life"]] == ["disputed", "new"]
    assert out["still_open"] == []  # the rules' copy of it does not page through the still-open list either
    triage = {"severity": "medium", "findings": findings, "life": out["life"], "still_open": out["still_open"]}
    assert deliver._fires(triage, 1) == [1]
    md = render_markdown(dict(_review(WRONG, SLOW_SQL), findings=[dict(WRONG, disputed=True, judges=[{"judge": "a", "verdict": "dispute",
                         "severity": "low", "reason": "44 in the window"}]), dict(SLOW_SQL, judges=[{"judge": "a", "verdict": "agree", "severity": "low",
                         "reason": "ok"}])], judges={"quorum": 1, "judged": True, "disputed": 1, "added": 0, "judges": [{"judge": "a"}]},
                         target="ts", engine="postgres", snapshot="s"))
    assert "_Judged by a: quorum 1, 1 disputed._" in md and "**2 findings**: 1 high, 1 medium (1 disputed)" in md
    assert "1. 🔴 **High** · disputed · `_hyper_24_1242_chunk`" in md and "   - **Disputed** (no alert):\n     - a: 44 in the window\n" in md
    assert "   - **Judges' severity:** a would rate it low" in md


def test_the_settings_card_takes_one_judge_per_line(tmp_path):
    from dbtriage.settings import Settings, judges_text, judges_value
    from tests.test_settings_charts import seed

    rows, bad = judges_value("openrouter anthropic/claude-sonnet-5.5\n# a comment\n\nollama gpt-oss:20b http://llm.lan:11434/v1 LLM_KEY\n")
    assert bad == "" and rows == [{"provider": "openrouter", "model": "anthropic/claude-sonnet-5.5"},
                                  {"provider": "ollama", "model": "gpt-oss:20b", "base_url": "http://llm.lan:11434/v1", "api_key_env": "LLM_KEY"}]
    assert judges_text(rows) == "openrouter anthropic/claude-sonnet-5.5\nollama gpt-oss:20b http://llm.lan:11434/v1 LLM_KEY"
    assert judges_value("nope m")[1].startswith("judges, line 1: the provider is one of")
    assert judges_value("groq")[1] == "judges, line 1: give the model after the provider"
    assert judges_value("groq m llm.lan:11434")[1].startswith("judges, line 1: 'llm.lan:11434' is not an environment variable name")
    assert judges_value([{"provider": "groq", "model": "m", "api_key_cmd": "x"}]) == ([{"provider": "groq", "model": "m"}], "")
    st = Settings.for_config(seed(tmp_path))
    assert st.put_agent({"judges": "openrouter z-ai/glm-5.3", "quorum": "1"}) == []
    assert st.data["agent"]["judges"] == [{"provider": "openrouter", "model": "z-ai/glm-5.3"}] and st.data["agent"]["quorum"] == 1
    assert st.put_agent({"judges": "", "quorum": ""}) == [] and "judges" not in st.data["agent"] and "quorum" not in st.data["agent"]


def test_the_page_shows_disputed_and_added_findings(tmp_path):
    from tests.test_overlay_semantics import app_for
    from tests.conftest import seed_store

    seed_store(tmp_path / "data")
    p = tmp_path / "targets.toml"
    p.write_text(f'[storage]\nurl = "file://{tmp_path / "data"}"\n[agent]\njudges = [{{ provider = "openrouter", model = "z-ai/glm-5.3" }}]\n'
                 '[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    app = app_for(p)
    assert "openrouter z-ai/glm-5.3</textarea>" in app.settings_page().decode()
    sid = "postgres/demo/2026-11-05/20261105T100000Z"
    tr = dict(_review(dict(WRONG, disputed=True, judges=[{"judge": "openrouter:z-ai/glm-5.3", "verdict": "dispute", "severity": "low",
                                                          "reason": "44 in the window, not 2127"}]),
                      dict(MISSED, added_by=["openrouter:z-ai/glm-5.3"])),
              judges={"quorum": 1, "judged": True, "disputed": 1, "added": 1, "judges": [{"judge": "openrouter:z-ai/glm-5.3"}],
                      "noted": [{"judges": ["openrouter:z-ai/glm-5.3"], "severity": "low", "kind": "bloat", "subject": "t", "summary": "dead rows"}]},
              snapshot=sid, target="demo", engine="postgres")
    app.store.put_json(f"{sid}/triage.json", tr)
    page = app.snapshot(sid).decode()
    assert "<li class=disputed>" in page and "class='mark disputed'" in page and "added by the judges</span>" in page
    assert "<b>openrouter:z-ai/glm-5.3</b> disputes (low): 44 in the window, not 2127" in page
    assert "Judged by openrouter:z-ai/glm-5.3: quorum 1, 1 disputed, 1 added." in page and "Judges also noted" in page


@pytest.mark.parametrize("judged", [True, False])
def test_cli_triage_judges_every_review_the_rules_one_included(tmp_path, monkeypatch, judged):
    """The model is down, so the rules write the review; a judge still checks it, and its dispute keeps the finding
    from paging. With no judges set nothing changes."""
    from dbtriage import cli
    from dbtriage.config import Config, Target
    from tests.conftest import seed_store

    st = seed_store(tmp_path / "data")
    agent = {"provider": "anthropic", "model": "m", **({"judges": [{"provider": "openrouter", "model": "j"}]} if judged else {})}
    cfg = Config(storage_url="x", targets={"demo": Target("demo", "postgres", "d")}, base_dir=str(tmp_path), agent=agent)
    monkeypatch.setattr(llm, "make_backend", lambda a, env=True: (_ for _ in ()).throw(RuntimeError("no model in tests")))
    asked = []

    def fake_judge(j, storage, review, *a, **k):
        asked.append((j["model"], review["source"], len(review["findings"])))
        return _verdict("openrouter:j", *[("dispute" if i == 0 else "agree", "low") for i in range(len(review["findings"]))])

    monkeypatch.setattr(judge, "run_judge", fake_judge)
    sid, base = "postgres/demo/2026-11-05/20261105T100000Z", "postgres/demo/2026-11-05/20261105T090000Z"
    result, _ = cli.triage(cfg, st, sid, base, "scheduled", True, source="schedule")
    stored = json.loads(st.get(f"{sid}/triage.json"))
    if not judged:
        assert asked == [] and "judges" not in stored and not any(f.get("disputed") for f in stored["findings"])
        return
    assert asked == [("j", "rules", len(stored["findings"]))] and stored["findings"][0]["disputed"] is True
    assert stored["life"][0]["state"] == "disputed" and 0 not in deliver._fires(stored, 0)
    assert "disputed" in st.get(f"{sid}/triage.md").decode()


def test_a_review_and_its_judge_over_http(tmp_path, fake_llm):
    """The reviewer and a judge on the OpenAI-style backend against the fake model: the judge reads the comparison,
    disputes the review's finding, and the stored review keeps it, disputed, with the reason, and does not page."""
    from dbtriage import cli
    from dbtriage.config import Config, Target
    from tests.conftest import seed_store

    st = seed_store(tmp_path / "data")
    fake = {"provider": "ollama", "model": "fake-model", "base_url": fake_llm.url}
    cfg = Config(storage_url="x", targets={"demo": Target("demo", "postgres", "d")}, base_dir=str(tmp_path),
                 agent={**fake, "judges": [dict(fake, model="fake-large")]})
    sid, base = "postgres/demo/2026-11-05/20261105T100000Z", "postgres/demo/2026-11-05/20261105T090000Z"
    n0 = len(fake_llm.requests)
    result, _ = cli.triage(cfg, st, sid, base, "adhoc", True)
    judged = [r for r in fake_llm.requests[n0:] if "submit_verdict" in [t["function"]["name"] for t in r.get("tools") or []]]
    assert len(judged) == 2 and {r["model"] for r in judged} == {"fake-large"}  # compare, then the verdict
    (f,) = result["findings"]
    assert result["source"] == "agent" and f["disputed"] is True and f["judges"][0]["reason"] == "Fake judge: the evidence does not hold."
    assert result["severity"] == "info" and result["judges"]["judges"][0]["judge"] == "ollama:fake-large"
    assert result["life"][0]["state"] == "disputed" and deliver._fires(result, 0) == []
