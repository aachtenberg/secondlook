"""Both backends against fake clients: the loop, tool dispatch, bad JSON, refusals."""
import json
from types import SimpleNamespace as NS

from dbtriage.llm import ToolSpec, make_backend
from dbtriage.llm.anthropic_backend import AnthropicBackend
from dbtriage.llm.openai_backend import OpenAIBackend


def tools(log):
    def add(args):
        log.append(("add", args)); return str(args["a"] + args["b"])

    def submit(args):
        log.append(("submit", args)); return "recorded"

    schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}
    return [ToolSpec("add", "add two ints", schema, add),
            ToolSpec("submit", "finish", {"type": "object", "properties": {"text": {"type": "string"}}}, submit)]


# ------------------------------------------------------------------ openai-compatible
def oa_response(content=None, tool_calls=None, finish="stop"):
    msg = NS(content=content, tool_calls=[NS(id=f"c{i}", function=NS(name=n, arguments=a)) for i, (n, a) in enumerate(tool_calls)] if tool_calls else None)
    return NS(choices=[NS(message=msg, finish_reason="tool_calls" if tool_calls else finish)], usage=NS(prompt_tokens=10, completion_tokens=5), model="fake-1")


class FakeOpenAI:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        return self.script.pop(0)


def test_openai_backend_loop_and_errors(monkeypatch):
    log = []
    client = FakeOpenAI([
        oa_response(tool_calls=[("add", '{"a": 2, "b": 3}'), ("nope", "{}"), ("add", "{not json")]),
        oa_response(tool_calls=[("submit", '{"text": "done"}')]),
        oa_response(content="final words"),
    ])
    b = OpenAIBackend({"provider": "ollama", "model": "qwen3:8b"}, client=client)
    out = b.run("sys", "task", tools(log), max_iterations=5)
    assert out.text == "final words" and out.tool_calls == 4 and out.model == "fake-1"
    assert out.usage == {"input_tokens": 30, "output_tokens": 15, "token_budget": 300_000, "budget_reached": False}
    assert [n for n, _ in log] == ["add", "submit"]
    # the model saw every tool result, including the errors, as role=tool messages
    second = client.requests[1]["messages"]
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "5"
    assert "unknown tool" in tool_msgs[1]["content"] and "not valid JSON" in tool_msgs[2]["content"]
    assert client.requests[0]["tools"][0]["function"]["name"] == "add"


def test_openai_backend_nudges_once_for_required_tool():
    log = []
    client = FakeOpenAI([oa_response(content="here is my prose answer"),          # stops without submitting
                         oa_response(tool_calls=[("submit", '{"text": "ok"}')]),   # after the nudge
                         oa_response(content="done")])
    out = OpenAIBackend({"provider": "ollama", "model": "m"}, client=client).run("s", "t", tools(log), 6, require_tool="submit")
    assert [n for n, _ in log] == ["submit"] and out.text == "done"
    nudge = client.requests[1]["messages"][3]  # system, task, prose answer, nudge
    assert nudge["role"] == "user" and "submit" in nudge["content"]
    # a model that ignores the nudge is not nudged forever
    client = FakeOpenAI([oa_response(content="prose"), oa_response(content="more prose")])
    out = OpenAIBackend({"provider": "ollama", "model": "m"}, client=client).run("s", "t", tools([]), 6, require_tool="submit")
    assert out.text == "more prose" and len(client.requests) == 2


def test_openai_backend_hits_iteration_cap():
    client = FakeOpenAI([oa_response(tool_calls=[("add", '{"a": 1, "b": 1}')])] * 3)
    out = OpenAIBackend({"provider": "groq", "model": "x"}, client=client).run("s", "t", tools([]), max_iterations=3)
    assert out.stop_reason == "max_iterations" and out.tool_calls == 3


def test_make_backend_presets(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    b = make_backend({"provider": "groq", "model": "llama-3.3-70b-versatile"})
    assert isinstance(b, OpenAIBackend) and str(b.client.base_url).startswith("https://api.groq.com/openai/v1")
    monkeypatch.setenv("DBTRIAGE_PROVIDER", "ollama")
    monkeypatch.setenv("DBTRIAGE_MODEL", "qwen3:8b")
    assert make_backend({"provider": "anthropic"}).model == "qwen3:8b"


# ------------------------------------------------------------------ anthropic
def anthropic_client(script):
    """A real Anthropic SDK client whose HTTP calls get scripted Messages API replies, so the real tool runner runs."""
    import anthropic
    import httpx2 as httpx

    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=script.pop(0))

    client = anthropic.Anthropic(api_key="test", base_url="http://anthropic.test", max_retries=0,
                                 http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    return client, requests


def am(content, stop="end_turn", inp=100, out=10, cache_read=0, model="claude-x"):
    blocks = [{"type": "text", "text": c} if isinstance(c, str) else
              {"type": "tool_use", "id": f"tu_{i}", "name": c[0], "input": c[1]} for i, c in enumerate(content)]
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": blocks,
            "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else stop, "stop_sequence": None,
            "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": 0}}


def test_anthropic_backend_direct_and_refusal():
    log = []
    client, reqs = anthropic_client([am([("add", {"a": 1, "b": 2}), ("submit", {"text": "x"})]), am(["ok"], model="claude-y")])
    out = AnthropicBackend({"provider": "anthropic", "model": "claude-opus-5-5"}, client=client).run("s", "t", tools(log))
    assert out.text == "ok" and out.tool_calls == 2 and out.model == "claude-y" and not out.refused
    assert [n for n, _ in log] == ["add", "submit"]  # each tool ran once
    assert reqs[0]["fallbacks"] == "default" and reqs[0]["output_config"] == {"effort": "high"}
    assert reqs[0]["system"][0]["cache_control"]
    tool_results = reqs[1]["messages"][-1]["content"]
    assert [r["content"] for r in tool_results] == ["3", "recorded"]

    client, reqs = anthropic_client([am(["no"], stop="refusal")])
    out = AnthropicBackend({"provider": "anthropic", "model": "claude-opus-5-5"}, client=client).run("s", "t", tools([]))
    assert out.refused


def test_anthropic_backend_counts_every_call_and_stops_once_submitted():
    """Usage is the sum of every model call, cached input included; once the review is in, no further call is made."""
    log = []
    client, reqs = anthropic_client([am([("add", {"a": 1, "b": 2})], inp=50, cache_read=1000, out=10),
                                     am([("submit", {"text": "x"})], inp=60, cache_read=1000, out=20),
                                     am(["acknowledged"])])  # never requested
    done = lambda: any(n == "submit" for n, _ in log)  # noqa: E731
    out = AnthropicBackend({"provider": "anthropic", "model": "m"}, client=client).run("s", "t", tools(log), stop_when=done)
    assert len(reqs) == 2 and out.stop_reason == "submitted" and out.tool_calls == 2
    assert out.usage["input_tokens"] == 2110 and out.usage["output_tokens"] == 30 and out.usage["cache_read_input_tokens"] == 2000
    assert [n for n, _ in log] == ["add", "submit"]


def test_anthropic_backend_wraps_up_at_the_token_budget():
    """When the next call would pass the budget, the model gets one last call, told to submit; nothing runs after it."""
    log = []
    client, reqs = anthropic_client([am([("add", {"a": 1, "b": 2})], inp=400, out=50),
                                     am([("submit", {"text": "early"}), ("add", {"a": 5, "b": 5})], inp=500, out=40),
                                     am(["never"])])
    cfg = {"provider": "anthropic", "model": "m", "token_budget": 800}
    done = lambda: any(n == "submit" for n, _ in log)  # noqa: E731
    out = AnthropicBackend(cfg, client=client).run("s", "t", tools(log), require_tool="submit", stop_when=done)
    assert len(reqs) == 2 and out.stop_reason == "token_budget" and out.usage["budget_reached"] and out.usage["token_budget"] == 800
    last = reqs[1]["messages"][-1]["content"]
    assert last[0]["type"] == "tool_result" and last[-1]["type"] == "text" and "call submit now" in last[-1]["text"]
    assert reqs[1]["tools"] == reqs[0]["tools"] and reqs[1]["system"] == reqs[0]["system"]  # same prefix: the cache still hits
    assert [n for n, _ in log] == ["add", "submit"]  # the final submit ran, the extra add did not
    assert out.usage["input_tokens"] == 900 and out.tool_calls == 3


# ------------------------------------------------------------------ bedrock converse
class FakeConverse:
    def __init__(self, script):
        self.script, self.requests = list(script), []

    def converse(self, **kw):
        self.requests.append(kw)
        return self.script.pop(0)


def cv(text=None, uses=None, stop="end_turn"):
    content = ([{"text": text}] if text else []) + [{"toolUse": {"toolUseId": f"u{i}", "name": n, "input": a}} for i, (n, a) in enumerate(uses or [])]
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use" if uses else stop,
            "usage": {"inputTokens": 10, "outputTokens": 4}}


def test_bedrock_converse_loop_nudge_and_errors():
    from dbtriage.llm.bedrock_converse import BedrockConverseBackend

    log = []
    client = FakeConverse([cv(uses=[("add", {"a": 2, "b": 3}), ("nope", {})]), cv(text="prose only"),
                           cv(uses=[("submit", {"text": "x"})]), cv(text="done")])
    b = BedrockConverseBackend({"provider": "bedrock-converse", "model": "us.amazon.nova-lite-v1:0"}, client=client)
    out = b.run("sys", "task", tools(log), 8, require_tool="submit")
    assert out.text == "done" and out.tool_calls == 3
    assert out.usage == {"input_tokens": 40, "output_tokens": 16, "token_budget": 300_000, "budget_reached": False}
    assert [n for n, _ in log] == ["add", "submit"]
    first_results = client.requests[1]["messages"][2]["content"]
    assert first_results[0]["toolResult"]["content"][0]["text"] == "5" and first_results[0]["toolResult"]["status"] == "success"
    assert "unknown tool" in first_results[1]["toolResult"]["content"][0]["text"]
    assert client.requests[0]["toolConfig"]["tools"][0]["toolSpec"]["name"] == "add"
    assert client.requests[0]["system"] == [{"text": "sys"}]


def test_vertex_gemini_preset_builds_url_and_token(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    b = make_backend({"provider": "vertex-gemini", "model": "gemini-3.8-flash", "project_id": "p1", "api_key_cmd": "echo tok-123"})
    assert b.model == "google/gemini-3.8-flash"
    assert str(b.client.base_url).startswith("https://aiplatform.googleapis.com/v1beta1/projects/p1/locations/global/endpoints/openapi")
    assert b.client.api_key == "tok-123"


def test_cleared_agent_fields_mean_the_default(monkeypatch):
    """The Agent card stores a cleared field as "" (so it beats targets.toml); the backends must read that as their
    default, not literally (agent review on PR #1, 071f22b)."""
    import anthropic

    from dbtriage.llm import anthropic_backend

    seen = {}
    monkeypatch.setattr(anthropic, "AnthropicVertex", lambda **kw: seen.update(vertex=kw) or "client")
    monkeypatch.setattr(anthropic, "AnthropicFoundry", lambda **kw: seen.update(foundry=kw) or "client")
    anthropic_backend.make_client({"provider": "vertex", "region": "", "project_id": "p", "fallback_models": []})
    assert seen["vertex"]["region"] == "global"
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "k-default")
    anthropic_backend.make_client({"provider": "foundry", "api_key_env": "", "resource": "r", "fallback_models": []})
    assert seen["foundry"]["api_key"] == "k-default"
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    b = make_backend({"provider": "vertex-gemini", "model": "gemini-3.8-flash", "project_id": "p1", "region": "", "api_key_cmd": "echo tok"})
    assert "/locations/global/" in str(b.client.base_url)
