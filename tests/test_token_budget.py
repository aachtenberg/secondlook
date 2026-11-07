"""The per-review token budget: counted over every model call, a wrap-up call when the next one would pass it, no
call after the review is in, the rules review when the model runs out without submitting, and the Settings field."""
import json

from dbtriage import llm
from dbtriage.llm import Budget, token_budget
from dbtriage.llm.openai_backend import OpenAIBackend
from tests.test_backends import FakeConverse, FakeOpenAI, cv, oa_response, tools

DEMO = "postgres/demo/2026-11-05/20261105T100000Z"
DEMO_BASE = "postgres/demo/2026-11-05/20261105T090000Z"


def test_token_budget_setting():
    assert token_budget({}) == llm.DEFAULT_TOKEN_BUDGET and token_budget({"token_budget": ""}) == llm.DEFAULT_TOKEN_BUDGET
    assert token_budget({"token_budget": 0}) == 0 and token_budget({"token_budget": "50000"}) == 50000
    b = Budget(100)
    b.add(40, 10)
    assert not b.exhausted()  # 50 spent, the next call resends at least 40: 90
    b.add(45, 10)
    assert b.exhausted()  # 105 spent already
    assert not Budget(0).exhausted()


def test_openai_backend_wraps_up_and_makes_no_call_after_the_budget():
    log = []
    # each call: 10 in, 5 out. Budget 30: after two calls 30 are spent and the next resends >= 10, so call 3 is the last
    client = FakeOpenAI([oa_response(tool_calls=[("add", '{"a": 1, "b": 1}')]), oa_response(tool_calls=[("add", '{"a": 2, "b": 2}')]),
                         oa_response(tool_calls=[("add", '{"a": 3, "b": 3}')]), oa_response(content="never")])
    out = OpenAIBackend({"provider": "ollama", "model": "m", "token_budget": 30}, client=client).run("s", "t", tools(log), 10, require_tool="submit")
    assert len(client.requests) == 3 and out.stop_reason == "token_budget" and out.usage["budget_reached"]
    msgs = client.requests[2]["messages"]  # the fake keeps the live list: find the wrap-up by content
    wraps = [i for i, m in enumerate(msgs) if m["role"] == "user" and "call submit now" in (m.get("content") or "")]
    assert len(wraps) == 1 and msgs[wraps[0] - 1]["role"] == "tool"  # after the second call's tool results
    assert client.requests[2]["tools"] == client.requests[0]["tools"]  # same tools: the provider's prompt cache still hits


def test_openai_backend_stops_as_soon_as_the_review_is_in():
    log = []
    client = FakeOpenAI([oa_response(tool_calls=[("submit", '{"text": "x"}')]), oa_response(content="acknowledged")])
    done = lambda: bool(log)  # noqa: E731
    out = OpenAIBackend({"provider": "ollama", "model": "m"}, client=client).run("s", "t", tools(log), 5, stop_when=done)
    assert len(client.requests) == 1 and out.stop_reason == "submitted" and log == [("submit", {"text": "x"})]


def test_converse_backend_wraps_up_inside_the_tool_result_turn():
    from dbtriage.llm.bedrock_converse import BedrockConverseBackend

    log = []
    client = FakeConverse([cv(uses=[("add", {"a": 1, "b": 2})]), cv(uses=[("submit", {"text": "x"})]), cv(text="never")])
    cfg = {"provider": "bedrock-converse", "model": "us.amazon.nova-lite-v1:0", "token_budget": 20}  # 14 per call
    out = BedrockConverseBackend(cfg, client=client).run("s", "t", tools(log), 8, require_tool="submit",
                                                          stop_when=lambda: any(n == "submit" for n, _ in log))
    assert len(client.requests) == 2 and out.usage["budget_reached"] and out.stop_reason == "submitted"
    turns = [m for m in client.requests[1]["messages"] if m["role"] == "user" and any("call submit now" in (b.get("text") or "") for b in m["content"])]
    assert len(turns) == 1 and "toolResult" in turns[0]["content"][0]  # one user turn: the tool results, then the note


def test_review_over_http_within_and_at_the_budget(tmp_path, fake_llm):
    """The real loop against the fake model: a normal review makes no acknowledgement call; a tight budget still
    gets a review, marked in its footer."""
    from dbtriage.cli import triage
    from dbtriage.config import Config, Target
    from dbtriage.report import render_markdown
    from tests.conftest import seed_store

    st = seed_store(tmp_path / "data")
    cfg = Config(storage_url=f"file://{tmp_path}/data", targets={"demo": Target("demo", "postgres", "x")},
                 agent={"provider": "ollama", "model": "fake-model", "base_url": fake_llm.url}, base_dir=str(tmp_path))
    n0 = len(fake_llm.requests)
    result, comparison = triage(cfg, st, DEMO, DEMO_BASE, "incident", True)
    assert result["source"] == "agent" and len(fake_llm.requests) - n0 == 2  # compare, submit: no third call
    assert result["usage"]["token_budget"] == llm.DEFAULT_TOKEN_BUDGET and not result["usage"]["budget_reached"]

    cfg.agent["token_budget"] = 150  # 120 per call: the second call is the last, told to submit
    n0 = len(fake_llm.requests)
    result, comparison = triage(cfg, st, DEMO, DEMO_BASE, "incident", True)
    assert result["source"] == "agent" and result["usage"]["budget_reached"] and len(fake_llm.requests) - n0 == 2
    assert "token budget for this review is used up" in json.dumps(fake_llm.requests[-1]["messages"][-1])
    assert "token budget 150 reached" in render_markdown(result, comparison)


def test_out_of_budget_without_a_review_falls_back_to_the_rules(tmp_path, monkeypatch):
    from dbtriage.cli import triage
    from dbtriage.config import Config, Target
    from tests.conftest import seed_store

    class Spent:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            return llm.AgentResult(text="", stop_reason="token_budget", model="m", tool_calls=4,
                                   usage={"input_tokens": 9000, "output_tokens": 500, "token_budget": 9000, "budget_reached": True})

    monkeypatch.setattr(llm, "make_backend", lambda cfg: Spent())
    st = seed_store(tmp_path / "data")
    cfg = Config(storage_url=f"file://{tmp_path}/data", targets={"demo": Target("demo", "postgres", "x")},
                 agent={"provider": "ollama", "model": "m"}, base_dir=str(tmp_path))
    result, _ = triage(cfg, st, DEMO, DEMO_BASE, "incident", True)
    assert result["source"] == "rules" and "token budget of 9,000 reached" in result["agent_error"]
    assert "token_budget" in result["agent_error"]  # says which setting to change


def test_settings_token_budget_field(tmp_path):
    from dbtriage.settings import Settings

    s = Settings(tmp_path / "dbtriage-settings.json")
    assert s.put_agent({"token_budget": str(llm.DEFAULT_TOKEN_BUDGET)}, base={}) == [] and "token_budget" not in s.data["agent"]
    assert s.put_agent({"token_budget": "0"}, base={}) == [] and s.data["agent"]["token_budget"] == 0  # no limit: stored
    assert s.put_agent({"token_budget": ""}, base={}) == [] and "token_budget" not in s.data["agent"]  # blank: the file's
    for bad in ("lots", -5, [1], True):
        assert s.put_agent({"token_budget": bad}, base={}) == ["token_budget: a whole number, at least 0"]
    assert s.put_agent({"max_iterations": 0}, base={}) == ["max_iterations: a whole number, at least 1"]


def test_agent_card_shows_the_budget(web_server):
    import urllib.request

    page = urllib.request.urlopen(f"{web_server}/settings").read().decode()
    assert f"name=\"token_budget\" type=\"number\" min=\"0\" step=\"10000\" value=\"{llm.DEFAULT_TOKEN_BUDGET}\"" in page



def test_a_discussion_answer_that_runs_out_says_so(tmp_path):
    from dbtriage.chat import Chat
    from tests.test_chat import SID, store

    class Spent:
        name = "fake"

        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            return llm.AgentResult(text="", stop_reason="token_budget", model="m",
                                   usage={"input_tokens": 9000, "output_tokens": 10, "token_budget": 9000, "budget_reached": True})

    answer = Chat(store(tmp_path), Spent).ask(SID, None, "why is it slow?")
    assert "token budget (9,000 tokens) ran out" in answer["text"]


def test_a_budget_crossed_by_the_last_text_turn_is_reported():
    """A model that ends on a text-only turn after spending past the budget must be reported as a budget stop, in
    every backend (agent review on PR #3, c14bd0b)."""
    from dbtriage.llm.anthropic_backend import AnthropicBackend
    from tests.test_backends import am, anthropic_client

    log = []
    client, reqs = anthropic_client([am([("add", {"a": 1, "b": 2})], inp=100, out=10), am(["I am done."], inp=300, out=50)])
    out = AnthropicBackend({"provider": "anthropic", "model": "m", "token_budget": 400}, client=client).run(
        "s", "t", tools(log), require_tool="submit", stop_when=lambda: False)
    assert len(reqs) == 2 and out.stop_reason == "token_budget" and out.usage["budget_reached"]
    # the OpenAI-style loop, once the nudge is spent: the same report
    client = FakeOpenAI([oa_response(content="prose"), oa_response(content="more prose")])
    out = OpenAIBackend({"provider": "ollama", "model": "m", "token_budget": 25}, client=client).run("s", "t", tools([]), 6, require_tool="submit")
    assert out.usage["budget_reached"] and out.stop_reason == "token_budget"


def test_prose_at_the_budget_gets_the_wrap_up_not_a_second_ask():
    """A model that answers in prose once the budget is spent gets one call, the wrap-up, which is also the ask."""
    log = []
    client = FakeOpenAI([oa_response(tool_calls=[("add", '{"a": 1, "b": 1}')]), oa_response(content="prose"),
                         oa_response(tool_calls=[("submit", '{"text": "x"}')]), oa_response(content="never")])
    out = OpenAIBackend({"provider": "ollama", "model": "m", "token_budget": 30}, client=client).run(
        "s", "t", tools(log), 8, require_tool="submit", stop_when=lambda: bool([n for n, _ in log if n == "submit"]))
    assert len(client.requests) == 3 and [n for n, _ in log] == ["add", "submit"] and out.usage["budget_reached"]
    asks = [m["content"] for m in client.requests[2]["messages"] if m["role"] == "user"][1:]
    assert len(asks) == 1 and "call submit now" in asks[0]  # the wrap-up only, no nudge on top of it
