"""Claude through the Anthropic SDK's tool runner, on the direct API or a cloud platform.

Refusal handling: on the direct API the server-side `fallbacks: "default"` retries a
safety-classifier decline on the model Anthropic recommends. Bedrock, Vertex and Foundry
have no server-side fallback, so the SDK's client-side middleware retries on the models in
`fallback_models` (set it to [] to disable). A refusal that survives the chain surfaces as
AgentResult.refused and the caller falls back to the rules-based triage.
"""
from __future__ import annotations

import os
from typing import Any

import anthropic
from anthropic.lib.tools import BetaFunctionTool

from dbtriage.llm import AgentResult, Budget, ToolSpec, token_budget, wrap_up_text

DEFAULT_FALLBACKS = {  # platform-specific ids for the same fallback model
    "bedrock": [{"model": "anthropic.claude-opus-4-8"}],
    "vertex": [{"model": "claude-opus-4-8"}],
    "foundry": [{"model": "claude-opus-4-8"}],
}


def _middleware(cfg: dict) -> list:
    from anthropic.lib.middleware import BetaRefusalFallbackMiddleware

    chain = cfg.get("fallback_models", DEFAULT_FALLBACKS.get(cfg["provider"], []))
    chain = [{"model": m} if isinstance(m, str) else m for m in chain]
    return [BetaRefusalFallbackMiddleware(chain)] if chain else []


def make_client(cfg: dict) -> anthropic.Anthropic:
    p = cfg["provider"]
    if p == "anthropic":
        return anthropic.Anthropic()  # ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile
    if p == "bedrock":
        from anthropic import AnthropicBedrockMantle

        return AnthropicBedrockMantle(aws_region=cfg.get("region") or os.environ.get("AWS_REGION"),
                                      aws_profile=cfg.get("aws_profile"), middleware=_middleware(cfg))
    if p == "vertex":
        from anthropic import AnthropicVertex

        return AnthropicVertex(project_id=cfg.get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT"),
                               region=cfg.get("region") or "global", middleware=_middleware(cfg))
    if p == "foundry":
        from anthropic import AnthropicFoundry

        key = os.environ.get(cfg.get("api_key_env") or "ANTHROPIC_FOUNDRY_API_KEY")
        return AnthropicFoundry(resource=cfg.get("resource"), api_key=key, middleware=_middleware(cfg))
    raise SystemExit(f"unsupported anthropic provider {p!r}")


class AnthropicBackend:
    def __init__(self, cfg: dict[str, Any], client: Any | None = None):
        self.cfg = cfg
        self.name = cfg["provider"]
        self.model = cfg["model"]
        self.client = client or make_client(cfg)

    def run(self, system: str, task: str, tools: list[ToolSpec], max_iterations: int = 25,
            require_tool: str | None = None, stop_when=None) -> AgentResult:  # Claude follows the system prompt; no nudge needed
        runner_tools = [_runner_tool(t) for t in tools]
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=int(self.cfg.get("max_tokens") or 16000),
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=runner_tools,
            messages=[{"role": "user", "content": task}],
        )
        if self.cfg.get("effort", "high"):  # effort = "" omits it (models before Claude 4.6 reject it)
            kwargs["output_config"] = {"effort": self.cfg.get("effort", "high")}
        if self.name == "anthropic":
            kwargs.update(fallbacks="default", betas=["server-side-fallback-2026-07-01"])
        budget = Budget(token_budget(self.cfg))
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        history: list = list(kwargs["messages"])  # what was sent, for a wrap-up call after the runner stops
        calls, last, stopped = 0, None, ""
        runner = self.client.beta.messages.tool_runner(**kwargs, max_iterations=max_iterations)
        try:
            for message in runner:
                last = message
                _count(message, usage, budget)
                uses = [b for b in message.content if b.type == "tool_use"]
                calls += len(uses)
                if not uses:
                    continue
                # run this turn's tools now (the runner reuses these results rather than running them again), so the
                # loop can stop here: once the review is in, or before a call that would pass the token budget
                results = runner.generate_tool_call_response()
                if results is None:
                    continue
                history += [message.to_param(), results]
                if stop_when and stop_when():
                    stopped = "submitted"
                    break
                if budget.exhausted():
                    stopped = "token_budget"
                    break
        finally:
            runner.close()
        if stopped == "token_budget":  # one last call, told to finish, with the same system and tools (cache hits)
            budget.reached = True
            history[-1] = {"role": "user", "content": [*history[-1]["content"], {"type": "text", "text": wrap_up_text(require_tool)}]}
            params = {k: v for k, v in kwargs.items() if k not in ("tools", "messages")}
            last = self.client.beta.messages.create(**params, tools=[t.to_dict() for t in runner_tools], messages=history)
            _count(last, usage, budget)
            by_name = {t.name: t for t in tools}
            for b in last.content:
                if b.type == "tool_use":
                    calls += 1
                    if b.name == require_tool and b.name in by_name:  # the review; nothing else runs after the budget
                        try:
                            by_name[b.name].fn(dict(b.input or {}))
                        except Exception:  # noqa: BLE001 - a bad final submission means no review: the caller falls back
                            pass
        if last is None:
            return AgentResult(text="", stop_reason="no_response", model=self.model, tool_calls=0)
        text = "".join(b.text for b in last.content if b.type == "text")
        reason = stopped or (budget.stop_reason(last.stop_reason or "") if last.stop_reason != "refusal" else "refusal")
        return AgentResult(text=text, stop_reason=reason, model=getattr(last, "model", self.model),
                           usage=budget.report(usage), tool_calls=calls, refused=last.stop_reason == "refusal")


def _count(message, usage: dict, budget: Budget) -> None:
    """Add one model call's tokens: input counts the cached part too (it is resent, and billed, every call)."""
    u = message.usage
    cached = (getattr(u, "cache_read_input_tokens", None) or 0) + (getattr(u, "cache_creation_input_tokens", None) or 0)
    inp = (u.input_tokens or 0) + cached
    usage["input_tokens"] += inp
    usage["output_tokens"] += u.output_tokens or 0
    usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", None) or 0
    budget.add(inp, u.output_tokens)


def _runner_tool(spec: ToolSpec) -> BetaFunctionTool:
    def call(**kwargs):
        return spec.fn(kwargs)

    return BetaFunctionTool(call, name=spec.name, description=spec.description, input_schema=spec.input_schema)
