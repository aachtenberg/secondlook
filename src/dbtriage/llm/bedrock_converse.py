"""Any Bedrock model through the Converse API with IAM auth: Amazon Nova (no third-party
agreement needed), Llama, Mistral, and Claude once its agreement is accepted. Uses boto3,
which is already a dependency for S3 storage.

    [agent]
    provider = "bedrock-converse"
    model = "us.amazon.nova-lite-v1:0"     # or us.amazon.nova-pro-v1:0, us.anthropic.claude-haiku-5-5 ...
    region = "us-east-1"
"""
from __future__ import annotations

import os
from typing import Any

from dbtriage.llm import AgentResult, Budget, ToolSpec, token_budget, wrap_up_text


class BedrockConverseBackend:
    name = "bedrock-converse"

    def __init__(self, cfg: dict[str, Any], client: Any | None = None):
        self.cfg = cfg
        self.model = cfg.get("model") or _die("[agent] model is required for provider 'bedrock-converse'")
        if client is None:
            import boto3

            client = boto3.client("bedrock-runtime", region_name=cfg.get("region") or os.environ.get("AWS_REGION"))
        self.client = client

    def run(self, system: str, task: str, tools: list[ToolSpec], max_iterations: int = 25,
            require_tool: str | None = None, stop_when=None) -> AgentResult:
        tool_config = {"tools": [{"toolSpec": {"name": t.name, "description": t.description, "inputSchema": {"json": t.input_schema}}}
                                 for t in tools]}
        by_name = {t.name: t for t in tools}
        messages: list[dict] = [{"role": "user", "content": [{"text": task}]}]
        calls, usage = 0, {"input_tokens": 0, "output_tokens": 0}
        called, nudged = set(), False
        budget = Budget(token_budget(self.cfg))
        done = lambda reason, text="": AgentResult(text=text, stop_reason=reason, model=self.model,  # noqa: E731
                                                   usage=budget.report(usage), tool_calls=calls)
        for _ in range(max_iterations):
            if not budget.reached and budget.exhausted():  # one more call, told to finish
                budget.reached = True
                note = {"text": wrap_up_text(require_tool)}
                if messages[-1]["role"] == "user":  # tool results: Converse wants one user turn, so add to it
                    messages[-1] = {"role": "user", "content": [*messages[-1]["content"], note]}
                else:
                    messages.append({"role": "user", "content": [note]})
            tool_kw = {"toolConfig": tool_config} if tools else {}  # Converse rejects an empty tool list
            resp = self.client.converse(modelId=self.model, system=[{"text": system}], messages=messages, **tool_kw,
                                        inferenceConfig={"maxTokens": int(self.cfg.get("max_tokens") or 4096)})
            usage["input_tokens"] += resp.get("usage", {}).get("inputTokens", 0)
            usage["output_tokens"] += resp.get("usage", {}).get("outputTokens", 0)
            budget.add(resp.get("usage", {}).get("inputTokens", 0), resp.get("usage", {}).get("outputTokens", 0))
            out = resp["output"]["message"]
            messages.append(out)
            uses = [b["toolUse"] for b in out.get("content", []) if "toolUse" in b]
            text = "".join(b.get("text", "") for b in out.get("content", []) if "text" in b)
            if not uses:
                if budget.reached:
                    return done("token_budget", text)
                if require_tool and require_tool not in called and not nudged:
                    nudged = True
                    if budget.exhausted():  # the budget's wrap-up, next, is the ask (and the last call)
                        continue
                    messages.append({"role": "user", "content": [{"text": f"Now record your conclusions by calling the {require_tool} tool. Do not answer in text."}]})
                    continue
                return done(budget.stop_reason(resp.get("stopReason", "end_turn")), text)
            results = []
            for u in uses:
                calls += 1
                called.add(u["name"])
                spec = by_name.get(u["name"])
                try:
                    result = spec.fn(u.get("input") or {}) if spec else f"unknown tool {u['name']!r}; available: {sorted(by_name)}"
                    status = "success"
                except Exception as exc:  # noqa: BLE001 - the model should see tool errors
                    result, status = f"error: {type(exc).__name__}: {exc}", "error"
                results.append({"toolResult": {"toolUseId": u["toolUseId"], "content": [{"text": str(result)}], "status": status}})
            messages.append({"role": "user", "content": results})
            if stop_when and stop_when():
                return done("submitted", text)
            if budget.reached:
                return done("token_budget", text)
        return done("max_iterations")


def _die(msg: str):
    raise SystemExit(msg)
