"""The discussion agent acting for the person asking: reading and changing a database's schedule, starting a run
(dbtriage.chat_actions), with that person's permissions and the agent's own limits."""
from __future__ import annotations

import pytest

from dbtriage import chat, chat_actions, llm
from dbtriage.web import App
from tests.test_web import SID, config, store


class Script:
    """A backend that calls the given tools in order, then answers. Records what it was offered."""

    def __init__(self, *calls, fail_after: bool = False):
        self.calls, self.fail_after = list(calls), fail_after
        self.seen_tools: list[str] = []
        self.system = self.task = ""
        self.results: list[str] = []

    def __call__(self, cfg):
        return self

    name = "fake"

    def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
        self.system, self.task = system, task
        self.seen_tools = [t.name for t in tools]
        by_name = {t.name: t for t in tools}
        for name, args in self.calls:
            self.results.append(by_name[name].fn(args) if name in by_name else f"(no tool {name})")
        if self.fail_after:
            raise RuntimeError("provider fell over")
        return llm.AgentResult(text="Done.", stop_reason="end_turn", model="m", usage={"input_tokens": 1, "output_tokens": 1}, tool_calls=len(self.calls))


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    st = store(tmp_path)

    def make(script: Script, **flags) -> App:
        monkeypatch.setattr(llm, "make_backend", script)
        return App(config(tmp_path), st, flags.get("allow_run", False), None, allow_edit=flags.get("allow_edit", True), allow_chat=True)

    return make


def sched(app: App) -> dict:
    return next(r for r in app.schedule_rows() if r["target"] == "demo")


def test_too_often():
    assert chat_actions.too_often("*/5 * * * *") and chat_actions.too_often("*/5 * * * 1")  # tight on Mondays only, too
    assert not chat_actions.too_often("*/15 * * * *") and not chat_actions.too_often("0 * * * *") and not chat_actions.too_often("0 3 * * *")


def test_the_agent_sets_the_schedule_it_was_asked_for(make_app):
    s = Script(("get_schedule", {}), ("set_schedule", {"schedule": "hourly", "window_minutes": 60}))
    app = make_app(s)
    code, ans = app.chat_ask(SID, "target", "set it to hourly")
    assert code == 200 and sched(app)["schedule"] == "0 * * * *" and sched(app)["window"] == 60
    assert '"described": "not scheduled"' in s.results[0] and '"changed": true' in s.results[1]
    assert [a["text"] for a in ans["actions"]] == ["Schedule of demo: not scheduled → hourly, 60-minute window."]
    assert "Done for you" in ans["actions"][0]["html"] and "href='/settings#schedule'" in ans["actions"][0]["html"]
    roles = [m["role"] for m in app.chat_thread(SID, "target")["messages"]]
    assert roles == ["user", "action", "assistant"] and app.chat_thread(SID, "target")["messages"][1]["html"]
    assert "never a request" in s.system and "run_now" not in s.seen_tools  # no --allow-run: no run tool
    # the next question is told what was done
    s2 = Script()
    app = make_app(s2)
    app.chat_ask(SID, "target", "and what now?")
    assert "You did, as the engineer asked: Schedule of demo: not scheduled → hourly" in s2.task
    assert "**Change** (made by the model" in chat.thread_markdown(chat.load_thread(app.store, SID, "target"))


def test_tools_follow_the_person_s_permissions(make_app, monkeypatch):
    s = Script(("set_schedule", {"schedule": "hourly"}))
    app = make_app(s, allow_edit=False)
    assert app.chat_ask(SID, "target", "set it to hourly")[0] == 200
    assert s.seen_tools[-1] == "get_schedule" and "set_schedule" not in s.seen_tools and "(no tool set_schedule)" in s.results
    assert "You cannot change its schedule or start a run here" in s.system and sched(app)["schedule"] == ""
    s = Script()
    app = make_app(s, allow_edit=True, allow_run=True)
    monkeypatch.setattr(app, "allows", lambda cap: cap != "edit")  # a group that may run but not edit
    app.chat_ask(SID, "target", "hi")
    assert "run_now" in s.seen_tools and "set_schedule" not in s.seen_tools and "You cannot change its schedule here" in s.system


def test_limits_the_agent_keeps(make_app, monkeypatch):
    s = Script(("set_schedule", {"schedule": "*/5 * * * *"}))
    app = make_app(s)
    code, ans = app.chat_ask(SID, "target", "every 5 minutes please")
    assert code == 200 and "at most every 15 minutes" in s.results[0] and sched(app)["schedule"] == "" and "actions" not in ans
    s = Script(("set_schedule", {"schedule": "not a cron"}), ("set_schedule", {"schedule": "hourly"}), ("set_schedule", {"paused": True}),
               ("set_schedule", {"paused": False}), ("set_schedule", {"schedule": "daily"}), ("set_schedule", {"window_minutes": 3}))
    app = make_app(s)
    code, ans = app.chat_ask(SID, "target", "try a few")
    assert s.results[0].startswith("not changed: a schedule has five fields")
    assert len(ans["actions"]) == 3 and s.results[4] == "not changed: at most 3 schedule changes per question"
    assert sched(app)["schedule"] == "0 * * * *" and not sched(app)["paused"]  # hourly, paused, resumed; daily refused
    s = Script(("set_schedule", {"paused": False}), ("set_schedule", {"schedule": "hourly"}))  # already so: no change
    app = make_app(s)
    saves = []
    real = app.save_schedule
    monkeypatch.setattr(app, "save_schedule", lambda name, fields: (saves.append(fields), real(name, fields))[1])
    assert "actions" not in app.chat_ask(SID, "target", "resume it, keep it hourly")[1]
    assert '"changed": false' in s.results[0] and '"changed": false' in s.results[1] and saves == []  # not saved, not audited
    s = Script(("set_schedule", {"window_minutes": "lots"}))  # the wrong type still reaches the Settings checks
    app = make_app(s)
    app.chat_ask(SID, "target", "a bigger window")
    assert s.results == ["not changed: schedule_window must be a whole number"]


def test_a_window_alone_on_an_unscheduled_database_is_a_change(make_app):
    s = Script(("set_schedule", {"window_minutes": 30}), ("set_schedule", {"schedule": ""}))
    app = make_app(s)
    code, ans = app.chat_ask(SID, "target", "use a 30 minute window, then make sure it is not scheduled")
    assert code == 200 and '"changed": true' in s.results[0] and '"changed": true' in s.results[1]
    assert [a["text"] for a in ans["actions"]] == ["Schedule of demo: not scheduled → not scheduled, 30-minute window.",
                                                    "Schedule of demo: not scheduled, 30-minute window → not scheduled."]


def test_run_now_starts_one_run_as_the_person(make_app, monkeypatch):
    s = Script(("run_now", {"reason": "after the deploy"}), ("run_now", {}))
    app = make_app(s, allow_run=True)
    started = []
    monkeypatch.setattr(app, "start_run", lambda *a, **k: (started.append((a, k)), (202, {"accepted": True}))[1])
    code, ans = app.chat_ask(SID, "target", "run one now")
    assert code == 200 and len(started) == 1 and s.results[1] == "not started: at most 1 run per question"
    (target, mode, reason, use_llm), kw = started[0]
    assert (target, mode, use_llm, kw["source"], kw["window_minutes"]) == ("demo", "adhoc", True, "chat", 60)
    assert reason == "asked in a discussion: after the deploy"
    assert ans["actions"][0]["text"].startswith("Started a run of demo (60-minute window, after the deploy)")


def test_a_change_stays_on_record_when_the_answer_fails(make_app):
    s = Script(("set_schedule", {"schedule": "hourly"}), fail_after=True)
    app = make_app(s)
    code, out = app.chat_ask(SID, "target", "set it to hourly")
    assert code == 502 and "provider fell over" in out["error"]
    assert sched(app)["schedule"] == "0 * * * *"
    assert [m["role"] for m in app.chat_thread(SID, "target")["messages"]] == ["user", "action"]


def test_the_cli_path_stays_read_only(tmp_path):
    s = Script()
    chat.Chat(store(tmp_path), lambda: s).ask(SID, "target", "set it to hourly")
    assert not {"get_schedule", "set_schedule", "run_now"} & set(s.seen_tools) and "act for the engineer" not in s.system
