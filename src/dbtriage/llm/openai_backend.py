"""Any model behind an OpenAI-compatible chat-completions endpoint, with a manual tool loop.

Presets fill in base_url and the API-key variable; `base_url` / `api_key_env` in [agent] override them.
Azure OpenAI: provider = "azure-openai", base_url = "https://<resource>.openai.azure.com/openai/v1/",
api_key_env = "AZURE_OPENAI_API_KEY", model = "<deployment name>".
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any

from dbtriage.llm import AgentResult, Budget, ToolSpec, token_budget, wrap_up_text

PRESETS: dict[str, tuple[str | None, str, str | None]] = {  # provider -> (base_url, api_key_env, default key)
    "openai": (None, "OPENAI_API_KEY", None),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", None),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", None),
    "ollama": ("http://localhost:11434/v1", "OLLAMA_API_KEY", "ollama"),
    "ollama-cloud": ("https://ollama.com/v1", "OLLAMA_API_KEY", None),
    "vllm": ("http://localhost:8000/v1", "VLLM_API_KEY", "none"),
    "azure-openai": (None, "AZURE_OPENAI_API_KEY", None),
    "vertex-gemini": (None, "GOOGLE_OAUTH_ACCESS_TOKEN", None),  # base_url from project_id/region; ADC refreshes the token
}


class OpenAIBackend:
    def __init__(self, cfg: dict[str, Any], client: Any | None = None):
        self.cfg = cfg
        self.name = cfg["provider"]
        self.model = cfg.get("model") or _die(f"[agent] model is required for provider {self.name!r}")
        base_url, key_env, default_key = PRESETS[self.name]
        base_url = cfg.get("base_url") or base_url
        if self.name == "vertex-gemini":
            project, region = cfg.get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT"), cfg.get("region") or "global"
            host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
            base_url = base_url or f"https://{host}/v1beta1/projects/{project}/locations/{region}/endpoints/openapi"
            if "/" not in self.model:
                self.model = "google/" + self.model
        key = os.environ.get(cfg.get("api_key_env") or key_env) or default_key
        if not key and cfg.get("api_key_cmd"):  # a command that prints a short-lived token; never stored, and not refreshed
            key = subprocess.run(shlex.split(cfg["api_key_cmd"]), capture_output=True, text=True, check=True).stdout.strip()
        bearer = None
        if not key and self.name == "vertex-gemini":
            from dbtriage.cloudauth import gemini_access_token

            bearer = gemini_access_token  # called per request, so an expired ADC token is refreshed
        elif not key and self.name == "azure-openai":
            from dbtriage.cloudauth import azure_access_token

            bearer = azure_access_token  # DefaultAzureCredential when no resource key is set
        if client is None:
            from openai import OpenAI

            from dbtriage.egress import httpx_client

            if not key and bearer is None:
                _die(f"[agent] {cfg.get('api_key_env') or key_env} is not set for provider {self.name!r}: add it to the .env next to "
                     "targets.toml (picked up within a second) or to the server's environment")
            http = httpx_client()
            client = OpenAI(base_url=base_url, api_key=bearer or key, **({"http_client": http} if http is not None else {}))
        self.client = client

    def run(self, system: str, task: str, tools: list[ToolSpec], max_iterations: int = 25,
            require_tool: str | None = None, stop_when=None) -> AgentResult:
        oa_tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                    for t in tools]
        by_name = {t.name: t for t in tools}
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        calls, usage = 0, {"input_tokens": 0, "output_tokens": 0}
        model_seen, called, nudged = self.model, set(), False
        budget = Budget(token_budget(self.cfg))
        done = lambda reason, text="": AgentResult(text=text, stop_reason=reason, model=model_seen,  # noqa: E731
                                                   usage=budget.report(usage), tool_calls=calls)
        for _ in range(max_iterations):
            if not budget.reached and budget.exhausted():  # one more call, told to finish
                budget.reached = True
                messages.append({"role": "user", "content": wrap_up_text(require_tool)})
            tool_kw = {"tools": oa_tools, "tool_choice": "auto"} if oa_tools else {}  # several providers reject tools=[]
            resp = self.client.chat.completions.create(model=self.model, messages=messages, **tool_kw, **self.cfg.get("extra", {}))
            if getattr(resp, "usage", None):
                usage["input_tokens"] += resp.usage.prompt_tokens or 0
                usage["output_tokens"] += resp.usage.completion_tokens or 0
                budget.add(resp.usage.prompt_tokens, resp.usage.completion_tokens)
            model_seen = getattr(resp, "model", None) or model_seen
            choice = resp.choices[0]
            msg = choice.message
            if not msg.tool_calls:
                if budget.reached:
                    return done("token_budget", msg.content or "")
                if require_tool and require_tool not in called and not nudged:
                    # smaller models often answer in prose instead of calling the final tool: ask once
                    nudged = True
                    messages.append({"role": "assistant", "content": msg.content or ""})
                    if not budget.exhausted():  # otherwise the budget's wrap-up, next, is the ask (and the last call)
                        messages.append({"role": "user", "content": f"Now record your conclusions by calling the {require_tool} tool. "
                                                                    "Do not answer in text."})
                    continue
                return done(budget.stop_reason(choice.finish_reason or "stop"), msg.content or "")
            # Round-trip tool calls as the provider returned them: Gemini on Vertex, for one, rejects the
            # next request unless its thought_signature (an extra field) comes back with the call.
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                tc.model_dump(exclude_none=True) if hasattr(tc, "model_dump") else
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                calls += 1
                called.add(tc.function.name)
                spec = by_name.get(tc.function.name)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    result = spec.fn(args) if spec else f"unknown tool {tc.function.name!r}; available: {sorted(by_name)}"
                except json.JSONDecodeError:
                    result = "error: tool arguments were not valid JSON; call again with a JSON object"
                except Exception as exc:  # noqa: BLE001 - the model should see tool errors, not crash the run
                    result = f"error: {type(exc).__name__}: {exc}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
            if stop_when and stop_when():
                return done("submitted", msg.content or "")
            if budget.reached:
                return done("token_budget", msg.content or "")
        return done("max_iterations")


def _die(msg: str):
    raise SystemExit(msg)
