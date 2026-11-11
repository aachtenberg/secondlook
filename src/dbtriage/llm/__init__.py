"""LLM provider plugins. The agent only needs two things from a model: tool calling
with JSON-schema tools, and a loop. A backend provides exactly that:

    backend.run(system, task, tools, max_iterations) -> AgentResult

Every run keeps to a token budget, [agent] token_budget (default DEFAULT_TOKEN_BUDGET, 0 = none): the input and output
tokens of all its model calls together, cached input included. When the next call would pass it, the model is told to
finish with what it has, and that call is its last.

Backends:
  anthropic_backend.AnthropicBackend  Claude via the Anthropic SDK: direct API, Amazon Bedrock,
                                      Google Vertex AI, Microsoft Foundry
  openai_backend.OpenAIBackend        anything speaking the OpenAI chat-completions API: OpenRouter,
                                      Groq, Ollama (local or cloud), vLLM, Azure OpenAI, OpenAI,
                                      Gemini on Vertex AI (provider "vertex-gemini", project credentials)
  bedrock_converse.BedrockConverseBackend  any Bedrock model via the Converse API with IAM auth:
                                      Amazon Nova needs no third-party agreement; Claude once accepted

targets.toml:
    [agent]
    provider = "anthropic"      # anthropic | bedrock | vertex | foundry | openai | openrouter | groq | ollama | ollama-cloud | vllm | azure-openai
    model = "claude-opus-5-5"   # bedrock ids carry the "anthropic." prefix; ollama e.g. "qwen3:8b"
    effort = "high"             # anthropic family only
    # region = "us-east-1"      # bedrock / vertex;  project_id = "..." for vertex;  resource = "..." for foundry
    # base_url = "http://ollama.lan:11434/v1"   api_key_env = "MY_KEY"   (openai family overrides)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], str]  # takes the parsed JSON arguments, returns the tool result text


@dataclass
class AgentResult:
    text: str
    stop_reason: str
    model: str
    usage: dict = field(default_factory=dict)  # input_tokens (cached included), output_tokens, budget fields
    tool_calls: int = 0
    refused: bool = False


DEFAULT_TOKEN_BUDGET = 300_000  # the lab's reviews used 90k (Postgres), 217k (MySQL) and 669k (Oracle, paging AWR text)


def token_budget(cfg: dict) -> int:
    """[agent] token_budget: tokens one review (or one discussion answer) may spend; 0 = no limit, unset = the default."""
    v = cfg.get("token_budget")
    return DEFAULT_TOKEN_BUDGET if v in (None, "") else max(0, int(v))


def wrap_up_text(require_tool: str | None) -> str:
    if require_tool:
        return (f"The token budget for this review is used up. Do not call any more data tools: call {require_tool} now with "
                "what you have found, and say in `checked` what you did not get to.")
    return "The token budget for this answer is used up. Answer now from what you have, without calling any tools."


@dataclass
class Budget:
    """Tokens a run has spent against its limit. Each model call resends the conversation so far, so the next call
    costs at least what the last one's input did: stop before that would pass the limit."""
    limit: int = 0
    spent: int = 0
    last_input: int = 0
    reached: bool = False

    def add(self, input_tokens: int | None, output_tokens: int | None) -> None:
        self.last_input = int(input_tokens or 0)
        self.spent += self.last_input + int(output_tokens or 0)

    def exhausted(self) -> bool:
        return bool(self.limit) and self.spent + self.last_input > self.limit

    def over(self) -> bool:
        """Spent the whole budget already (the last call crossed it), whether or not a wrap-up call was made."""
        return bool(self.limit) and self.spent >= self.limit

    def stop_reason(self, reason: str) -> str:
        """How a run that ends on the model's own text turn is reported: a budget stop once the budget is spent."""
        return "token_budget" if self.reached or self.over() else reason

    def report(self, usage: dict) -> dict:
        return {**usage, "token_budget": self.limit, "budget_reached": self.reached or self.over()}


class Backend(Protocol):
    name: str

    def run(self, system: str, task: str, tools: list[ToolSpec], max_iterations: int = 25,
            require_tool: str | None = None, stop_when: Callable[[], bool] | None = None) -> AgentResult:
        """require_tool: a tool the model must call before finishing; backends may nudge once if it doesn't.
        stop_when: checked after each round of tool calls; true ends the run there (the review is in), saving the
        model call that would only acknowledge it."""
        ...


ANTHROPIC_PROVIDERS = ("anthropic", "bedrock", "vertex", "foundry")
OPENAI_PROVIDERS = ("openai", "openrouter", "groq", "ollama", "ollama-cloud", "vllm", "azure-openai", "vertex-gemini")
CONVERSE_PROVIDERS = ("bedrock-converse",)


SCHEDULED_FIELDS = ("scheduled_provider", "scheduled_model", "scheduled_base_url", "scheduled_api_key_env")
# what belongs to the main provider and is not carried over to a different scheduled provider
PROVIDER_FIELDS = ("model", "base_url", "api_key_env", "region", "project_id", "resource", "effort", "api_key_cmd", "aws_profile")


def agent_for(cfg: dict[str, Any], source: str | None) -> dict[str, Any]:
    """The [agent] settings a run uses. A run the schedule starts (source "schedule") takes scheduled_provider,
    scheduled_model, scheduled_base_url and scheduled_api_key_env where they are set, e.g. a cheap or local model for
    routine snapshots while the Run button, webhooks and discussion keep the main one. A different scheduled
    provider starts from none of the main provider's model, URL, key or region; the budget and iterations carry over."""
    if source != "schedule" or not (cfg.get("scheduled_provider") or cfg.get("scheduled_model")):
        return cfg
    out = {k: v for k, v in cfg.items() if k not in SCHEDULED_FIELDS}
    if cfg.get("scheduled_provider") and cfg["scheduled_provider"] != cfg.get("provider"):
        for k in PROVIDER_FIELDS:
            out.pop(k, None)
        out["provider"] = cfg["scheduled_provider"]
    for k in ("model", "base_url", "api_key_env"):
        if cfg.get(f"scheduled_{k}"):
            out[k] = cfg[f"scheduled_{k}"]
    return out


def scheduled_problem(cfg: dict[str, Any]) -> str | None:
    """A scheduled provider other than the main one needs its own model (the main one's would not exist there)."""
    p = cfg.get("scheduled_provider")
    if p and p != cfg.get("provider") and not cfg.get("scheduled_model"):
        return "scheduled_model: required when scheduled_provider differs from provider"
    return None


def make_backend(cfg: dict[str, Any]) -> Backend:
    cfg = dict(cfg)
    cfg["provider"] = os.environ.get("DBTRIAGE_PROVIDER") or cfg.get("provider", "anthropic")
    cfg["model"] = os.environ.get("DBTRIAGE_MODEL") or cfg.get("model") or ("claude-opus-5-5" if cfg["provider"] in ANTHROPIC_PROVIDERS else None)
    if cfg["provider"] in ANTHROPIC_PROVIDERS:
        from dbtriage.llm.anthropic_backend import AnthropicBackend

        return AnthropicBackend(cfg)
    if cfg["provider"] in OPENAI_PROVIDERS:
        from dbtriage.llm.openai_backend import OpenAIBackend

        return OpenAIBackend(cfg)
    if cfg["provider"] in CONVERSE_PROVIDERS:
        from dbtriage.llm.bedrock_converse import BedrockConverseBackend

        return BedrockConverseBackend(cfg)
    raise SystemExit(f"[agent] provider {cfg['provider']!r} unknown; choose from {ANTHROPIC_PROVIDERS + OPENAI_PROVIDERS + CONVERSE_PROVIDERS}")


def test_model(cfg: dict[str, Any]) -> tuple[bool, str]:
    """One cheap round trip with no tools: proves credentials, endpoint and model id. Used by the UI's 'Test model' button."""
    import time

    try:
        t0 = time.time()
        provider = os.environ.get("DBTRIAGE_PROVIDER") or cfg.get("provider")
        if provider in ("bedrock", "bedrock-converse"):
            from dbtriage.awscred import probe_caller, profile_name
            from dbtriage.egress import plain_provider_error

            who = probe_caller(profile_name(cfg), cfg.get("region") or None)  # account and ARN, or expired / none
            if who == "expired":
                return False, plain_provider_error("ExpiredTokenException: The security token included in the request is expired")
            if who == "none":
                return False, plain_provider_error("NoCredentialsError: Unable to locate credentials")
        backend = make_backend(cfg)
        res = backend.run("You are a connectivity check. Reply with the single word OK.", "Say OK.", [], max_iterations=1)
        text = (res.text or "").strip().replace("\n", " ")
        tokens = f"{res.usage.get('input_tokens', '?')} in / {res.usage.get('output_tokens', '?')} out"
        return True, f"{backend.name}:{res.model} answered {text[:40]!r} ({tokens}, {time.time() - t0:.1f}s)"
    except SystemExit as exc:
        return False, _provider_failure(exc)
    except Exception as exc:  # noqa: BLE001 - the message is the point
        return False, _provider_failure(exc)


def _provider_failure(exc: BaseException) -> str:
    """The Test model button shows the plain sentence when we know it (expired session, TLS, proxy)."""
    from dbtriage.awscred import note_from_error
    from dbtriage.egress import plain_provider_error

    note_from_error(exc)
    raw = f"{type(exc).__name__}: {str(exc)[:300]}"
    plain = plain_provider_error(raw)
    if plain != raw.split("\n")[0][:140]:
        return plain
    # SystemExit is already a sentence ([agent] set KEY). Other errors keep the type name.
    return str(exc) if isinstance(exc, SystemExit) else raw


def configured(cfg: dict[str, Any]) -> tuple[bool, str]:
    """Cheap check, no network: can a backend plausibly authenticate? Used by the UI to warn before a question is
    typed. Cloud providers (IAM, ADC, managed identity) resolve credentials at run time, so they count as configured."""
    provider = os.environ.get("DBTRIAGE_PROVIDER") or cfg.get("provider", "anthropic")
    if provider == "anthropic":
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True, ""
        return False, "no ANTHROPIC_API_KEY in the server's environment"
    if provider in OPENAI_PROVIDERS:
        from dbtriage.llm.openai_backend import PRESETS

        _, key_env, default_key = PRESETS[provider]
        env = cfg.get("api_key_env") or key_env
        # vertex-gemini refreshes ADC; azure-openai uses a key or DefaultAzureCredential. Both resolve at call time.
        if os.environ.get(env) or default_key or cfg.get("api_key_cmd") or provider in ("vertex-gemini", "azure-openai"):
            return True, ""
        return False, f"no {env} in the server's environment"
    if provider in ANTHROPIC_PROVIDERS + CONVERSE_PROVIDERS:
        return True, ""
    return False, f"unknown provider {provider!r}"


_NOT_CHAT = ("whisper", "tts", "embed", "moderation", "dall-e", "image", "audio", "transcribe", "realtime", "rerank", "guard", "speech")
_MODEL_CACHE: dict[tuple, tuple[float, list[str], str]] = {}


def list_models(cfg: dict[str, Any], ttl_s: float = 300) -> tuple[list[str], str]:
    """Model ids the provider says this account can use, for the Settings page's model picker. Returns (ids, note);
    ids is empty when the provider cannot list (then note says what to type). One short network call, cached for
    five minutes per provider/endpoint. Never raises."""
    import time

    provider = cfg.get("provider") or "anthropic"
    key = (provider, cfg.get("base_url"), cfg.get("region"), cfg.get("api_key_env"), cfg.get("project_id"), cfg.get("aws_profile"))
    hit = _MODEL_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1], hit[2]
    try:
        ids, note = _list_models(provider, dict(cfg))
    except SystemExit as exc:  # missing key and friends: the message is the useful part
        ids, note = [], str(exc)
    except Exception as exc:  # noqa: BLE001
        ids, note = [], f"could not list models: {type(exc).__name__}: {str(exc)[:160]}"
    ids = sorted({i for i in ids if not any(w in i.lower() for w in _NOT_CHAT)})
    if ids:
        _MODEL_CACHE[key] = (time.monotonic(), ids, note)
    return ids, note


def _list_models(provider: str, cfg: dict) -> tuple[list[str], str]:
    if provider in OPENAI_PROVIDERS:
        if provider == "vertex-gemini":
            return [], "Vertex does not list Gemini models here; type an id such as gemini-3.8-flash."
        from dbtriage.llm.openai_backend import OpenAIBackend

        b = OpenAIBackend({**cfg, "provider": provider, "model": cfg.get("model") or "list"})
        ids = [m.id for m in b.client.with_options(timeout=10).models.list()]
        if provider == "azure-openai":
            return ids, "Azure needs your deployment name, which may differ from these base model names."
        return ids, ""
    if provider == "anthropic":
        import anthropic

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return [], "ANTHROPIC_API_KEY is not set: add it to the .env next to targets.toml"
        return [m.id for m in anthropic.Anthropic(timeout=10).models.list(limit=100)], ""
    if provider in ("bedrock", "bedrock-converse"):
        from dbtriage.awscred import client as aws_client
        from dbtriage.awscred import profile_name

        bed = aws_client("bedrock", region=cfg.get("region") or os.environ.get("AWS_REGION") or None, profile=profile_name(cfg))
        fms = bed.list_foundation_models(byOutputModality="TEXT")["modelSummaries"]
        if provider == "bedrock":
            ids = [m["modelId"].split(":")[0].rsplit("-v", 1)[0] for m in fms if m.get("providerName") == "Anthropic"]
            return ids, "Claude ids as Bedrock Mantle expects them (no version suffix); your account may still need the model agreement."
        ids = [m["modelId"] for m in fms if "ON_DEMAND" in (m.get("inferenceTypesSupported") or [])]
        try:
            ids += [p["inferenceProfileId"] for p in bed.list_inference_profiles()["inferenceProfileSummaries"]]
        except Exception:  # noqa: BLE001 - older accounts or missing permission: on-demand ids are enough
            pass
        return ids, ""
    if provider in ("vertex", "foundry"):
        return [], "This platform does not list Claude models here; type an id such as claude-opus-5-5."
    return [], f"unknown provider {provider!r}"
