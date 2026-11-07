# AI models

The reviewer needs two things from a model: the ability to call tools, and a loop. Anything that offers those
works. You pick it on the Settings page (Agent card, with a **Test model** button) or in `[agent]`:

```toml
[agent]
provider = "ollama"
model = "qwen3:8b"
```

`DBTRIAGE_PROVIDER` and `DBTRIAGE_MODEL` override both for the process they are set in. Set them on one command
for a one-off run; in `.env` they would override the Settings page for every run.

**Picking the model.** When you choose a provider on the Agent card, dbtriage asks that provider which models
your account can use and offers them as you type. It says so when the saved model is not among them, which
catches a wrong name before the first review fails. Groq, OpenAI, OpenRouter, Ollama, vLLM, Anthropic and Bedrock
can list their models; for Vertex, Foundry and Azure deployments you type the id.

**API keys.** Put them in the `.env` file next to `targets.toml`, for example `GROQ_API_KEY=...`. dbtriage reads
that file itself and notices edits within a second, so a new key works without a restart. A variable set in the
real environment (systemd, a container, a secret store) always wins over the file. `DBTRIAGE_ENV_FILE` points
at another file and `DBTRIAGE_NO_ENV_FILE=1` turns this off.

## Supported providers

| Provider | What it is | How it signs in |
|---|---|---|
| `anthropic` | Claude, directly from Anthropic | `ANTHROPIC_API_KEY` |
| `bedrock` | Claude on Amazon Bedrock | your AWS credentials and `region`; model ids look like `anthropic.claude-...` |
| `vertex` | Claude on Google Vertex AI | Google application credentials, `project_id`, `region`; Claude must be enabled in Model Garden |
| `foundry` | Claude on Microsoft Foundry | `ANTHROPIC_FOUNDRY_API_KEY` and `resource` |
| `openai`, `openrouter`, `groq`, `ollama`, `ollama-cloud`, `vllm`, `azure-openai` | anything that speaks the OpenAI chat API | the provider's key variable (`OPENAI_API_KEY`, `GROQ_API_KEY`, `OLLAMA_API_KEY` for ollama-cloud and so on; see `recipes/reference.env`), or name your own with `api_key_env`; `base_url` points elsewhere. Azure OpenAI uses its `/openai/v1/` address and the deployment name as the model |
| `vertex-gemini` | Google's Gemini on Vertex AI | your Google project credentials and `project_id`; the token comes from `gcloud auth print-access-token`, so the `gcloud` CLI must be installed where dbtriage runs |
| `bedrock-converse` | any model on Bedrock through the Converse API | your AWS credentials and `region` |

The Claude providers retry on a fallback model if the first one declines a request. If the model still cannot
review (no key, an unknown provider, an outage, or a model that never fills in the review), dbtriage writes the
rules-based review instead and says why at the top, so a run is never left without a review.

## Which ones need no sign-up forms

Claude on Bedrock and on Vertex sits behind marketplace agreements and Model Garden enablement. Each cloud's own
models do not. Tested without any forms:

| Model | Result on the lab incident |
|---|---|
| Gemini 3.8 Flash (`vertex-gemini`) | about a minute, 18 tool calls, the best write-up |
| Amazon Nova Lite (`bedrock-converse`, `us.amazon.nova-lite-v1:0`) | about 8 seconds, 4 tool calls, linked the deploy |
| Azure OpenAI `gpt-5-mini` | about a minute, about 2 cents |
| Ollama `qwen3:8b` on a CPU | 3 to 4 minutes, free |

Claude is still the recommendation where the agreement is already in place.

**Which to choose.** In an enterprise, keep the model inside your cloud tenant (Claude on Bedrock, Vertex or
Foundry) so SQL text never leaves it. For a small team or at home, a local Ollama model works well, as do Ollama
Cloud, Groq or OpenRouter. Smaller models sometimes answer in prose instead of filling in the review; with the
OpenAI-style and Bedrock Converse providers dbtriage asks once more, and otherwise falls back to the rules, so a
weak model never blocks a result.

Not yet run live, only tested with fake clients: Claude on Bedrock, Vertex and Foundry, and the hosted
OpenAI-style providers. One `dbtriage triage -t <db> --snapshot <id>` on a stored snapshot is a cheap smoke test.

## Keeping the bill down

Each review keeps to a **token budget**, `token_budget` in `[agent]` (300,000 unless you set it; 0 means no
limit). It counts the input and output of every model call in the review, cached input included, because each
call resends the conversation so far. When the next call would pass the budget, the model is asked to finish with
what it has, and that call is its last. If it still doesn't submit a review, you get the rules review, with a note
saying the budget ran out. A review that reached its budget says so in its footer. Discussion answers keep to the
same budget, each answer on its own. For scale, the lab's reviews used about 90k tokens on Postgres, 217k on MySQL
and 669k on Oracle, where the model pages through the AWR text.

A review also ends as soon as the model submits it, rather than making one more call just to acknowledge it.

Scheduled snapshots can use a **cheaper or local model** than the one you review incidents with. Set
`scheduled_provider` and `scheduled_model` in `[agent]` (or under *Scheduled runs* on the Settings page), plus
`scheduled_base_url` and `scheduled_api_key_env` if that provider needs them. Only runs the schedule starts use
them; the Run button, webhooks and discussion keep the main model. For example, routine snapshots on a local
Ollama, incidents on Claude:

```toml
[agent]
provider = "anthropic"
model = "claude-opus-5-5"
scheduled_provider = "ollama"
scheduled_model = "gemma4:26b"
scheduled_base_url = "http://ollama.lan:11434/v1"
```

When the scheduled provider differs from the main one, none of the main provider's model, URL, key or region
carry over, so `scheduled_model` is required. The token budget and the iteration limit apply to both.

Cloud budgets only send alerts; they do not stop spending. Two worked examples from the lab turn one into a hard
stop (swap in your own IDs), each with a one-line revert and a note about the reporting delay:

- [examples/azure-budget-cap.md](../examples/azure-budget-cap.md): at 100%, switch off the Azure AI account's
  public access.
- [examples/gcp-budget-cap.md](../examples/gcp-budget-cap.md): at 100%, turn off the Vertex AI API on the Google
  Cloud project, through a Pub/Sub-triggered function ([source](../examples/gcp-budget-cap/)).

## Adding a provider

An OpenAI-compatible service is one line in the presets of `src/dbtriage/llm/openai_backend.py`, plus its name
in `OPENAI_PROVIDERS` (`src/dbtriage/llm/__init__.py`) and `AGENT_PROVIDERS` (`src/dbtriage/settings.py`). Another
Claude platform is one client constructor in `src/dbtriage/llm/anthropic_backend.py`, plus the same two lists.

`api_key_cmd`, a command that prints a short-lived token, can only be set in `targets.toml`: it runs on the server,
so the web view never offers it.
