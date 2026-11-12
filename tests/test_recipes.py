"""The recipes and references stay true to the code.

- Every config key and environment variable the code reads is mentioned in recipes/reference.toml and
  recipes/reference.env (found by scanning the source, so a new option fails this test until it is documented).
- Every recipe loads, and uses only known sections, keys, providers, channels, engines, schedules and baselines.
- Every recipe's .env.example covers every variable that recipe names.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "dbtriage"
RECIPES = REPO / "recipes"
RECIPE_DIRS = sorted(p.parent for p in RECIPES.glob("*/targets.toml"))


def _src(*globs: str) -> str:
    return "\n".join(p.read_text() for g in globs for p in sorted(SRC.glob(g)))


def _keys(text: str, var: str) -> set[str]:
    return set(re.findall(rf'\b{var}(?:\.get\(|\[)"([a-z_]+)"', text))


def known_keys() -> dict[str, set[str]]:
    from dbtriage.config import Target
    from dbtriage.settings import AGENT_FIELDS, CHANNEL_FIELDS, TARGET_FIELDS, WEB_FIELDS
    from dbtriage.web import DEFAULTS

    agent = set(AGENT_FIELDS) | _keys(_src("llm/*.py", "agent.py", "cli.py", "chat.py"), "cfg") | {"extra"}
    agent -= {"provider_opts"}
    target = (set(TARGET_FIELDS) | _keys(_src("**/*.py"), "extra") | set(Target.__dataclass_fields__)) - {"name", "extra"}
    web = set(DEFAULTS) | set(WEB_FIELDS) | _keys(_src("*.py"), "web") | {"settings_file"}
    deliver_src = _src("deliver.py")
    channel = {n for spec in CHANNEL_FIELDS.values() for n, _, _ in spec} | _keys(deliver_src, "cfg") \
        | set(re.findall(r'_env\(cfg, "([a-z_]+)"\)', deliver_src)) | {"min_severity"}
    from dbtriage.dbsnap import COLLECT_FIELDS
    from dbtriage.mcp_server import FIELDS as MCP_FIELDS

    return {"agent": agent, "target": target, "web": web, "channel": channel, "collect": set(COLLECT_FIELDS),
            "mcp": set(MCP_FIELDS)}


def known_env() -> set[str]:
    from dbtriage.llm.openai_backend import PRESETS

    code = _src("**/*.py")
    names = set(re.findall(r'environ(?:\.get\(|\[)"([A-Z][A-Z0-9_]+)"', code))
    names |= {env for _, env, _ in PRESETS.values()}
    names |= set(re.findall(r'"([A-Z][A-Z0-9_]*_API_KEY)"', _src("llm/*.py")))
    return names


def mentioned(text: str, key: str) -> bool:
    """`key = ...` or a `[section.key]` table header, commented out or not."""
    k = re.escape(key)
    return re.search(rf"^\s*#?\s*({k}\s*=|\[[a-z_.-]*\.{k}\])", text, re.M) is not None  # a target's name may carry '-'


# --------------------------------------------------------------------------- the references are complete
@pytest.mark.parametrize("section", ["agent", "target", "web", "channel", "collect", "mcp"])
def test_reference_toml_mentions_every_option(section):
    ref = (RECIPES / "reference.toml").read_text()
    missing = sorted(k for k in known_keys()[section] if not mentioned(ref, k))
    assert missing == [], f"recipes/reference.toml does not explain these {section} options: {missing}"


def test_reference_env_mentions_every_variable():
    ref = (RECIPES / "reference.env").read_text()
    missing = sorted(v for v in known_env() if not mentioned(ref, v))
    assert missing == [], f"recipes/reference.env does not mention: {missing}"


def test_reference_toml_itself_loads(monkeypatch):
    load(RECIPES / "reference.toml", monkeypatch)


# --------------------------------------------------------------------------- every recipe is valid
def load(path: Path, monkeypatch):
    from dbtriage.config import load_config

    data = tomllib.loads(path.read_text())
    for t in data.get("targets", {}).values():
        if t.get("dsn_env"):
            monkeypatch.setenv(t["dsn_env"], "postgresql://u:p@h/db")
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    monkeypatch.delenv("DBTRIAGE_STORAGE", raising=False)
    return data, load_config(path)


def env_names_in(data: dict) -> set[str]:
    names = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.endswith("_env") and isinstance(v, str):
                    names.add(v)
                walk(v)
    walk(data)
    return names


@pytest.mark.parametrize("recipe", RECIPE_DIRS, ids=lambda p: p.name)
def test_recipe_is_valid(recipe, monkeypatch):
    from dbtriage import scheduler
    from dbtriage.baseline import POLICIES
    from dbtriage.deliver import CHANNELS
    from dbtriage.settings import AGENT_PROVIDERS, ENGINES

    data, cfg = load(recipe / "targets.toml", monkeypatch)
    keys = known_keys()
    assert set(data) <= {"storage", "agent", "delivery", "web", "targets", "collect", "mcp"}, set(data)
    from dbtriage.dbsnap import COLLECT_FIELDS

    assert set(data.get("collect", {})) <= set(COLLECT_FIELDS), set(data["collect"])
    assert set(data.get("agent", {})) <= keys["agent"], set(data["agent"]) - keys["agent"]
    assert cfg.agent.get("provider") in AGENT_PROVIDERS
    assert re.match(r"^(file|s3|az|azure|postgres|postgresql)://", cfg.storage_url), cfg.storage_url
    web = data.get("web", {})
    assert set(web) - {"severity_colors"} <= keys["web"], set(web) - keys["web"]
    d = data.get("delivery", {})
    assert set(d.get("channels", [])) <= set(CHANNELS)
    for ch, conf in d.items():
        if ch == "digest":  # [delivery.digest]: the fleet digest's schedule and channels (dbtriage.fleet), not a channel
            assert set(conf) <= {"schedule", "channels"} and set(conf.get("channels", [])) <= set(CHANNELS), conf
            scheduler.normalize(conf.get("schedule"))
            continue
        if isinstance(conf, dict):
            assert ch in CHANNELS, f"unknown channel section [delivery.{ch}]"
            assert set(conf) - {"headers"} <= keys["channel"], (ch, set(conf) - keys["channel"])
    assert cfg.targets, "a recipe needs at least one target"
    for name, t in data["targets"].items():
        assert set(t) <= keys["target"], (name, set(t) - keys["target"])
        assert t["engine"] in ENGINES
        if t.get("schedule"):
            scheduler.normalize(t["schedule"])
        if t.get("baseline_policy"):
            assert t["baseline_policy"] in POLICIES
    # its .env.example covers every variable the recipe names
    env_text = (recipe / ".env.example").read_text()
    wanted = env_names_in(data) | {t["dsn_env"] for t in data["targets"].values() if t.get("dsn_env")}
    if re.match(r"^postgres(ql)?://[^:@/]+@", cfg.storage_url):
        wanted.add("PGPASSWORD")
    if cfg.agent.get("provider") in ("openai", "openrouter", "groq", "azure-openai", "ollama-cloud"):
        from dbtriage.llm.openai_backend import PRESETS

        wanted.add(cfg.agent.get("api_key_env") or PRESETS[cfg.agent["provider"]][1])
    missing = sorted(v for v in wanted if not mentioned(env_text, v))
    assert missing == [], f"{recipe.name}/.env.example is missing {missing}"


def test_recipe_env_examples_hold_no_real_secrets():
    for f in RECIPES.glob("*/.env.example"):
        for line in f.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                value = line.split("=", 1)[1]
                assert "CHANGE" in value or value.startswith(("postgres", "mysql", "DBTRIAGE/", "https://", "dbtriage")) or "@" in value, f"{f}: {line}"


def test_snapshot_settings_say_where_writes_are_allowed():
    """A config that asks a database to take snapshots must show the [collect] switch that allows it (commented out
    is fine: dbtriage only reads by default), or the setting silently does nothing (agent review on PR #4)."""
    from dbtriage.dbsnap import mode_of

    for path in [*RECIPES.rglob("*.toml"), RECIPES.parent / "targets.toml", *(RECIPES.parent / "examples").glob("*.toml")]:
        data = tomllib.loads(path.read_text())
        asks = [n for n, t in data.get("targets", {}).items() if mode_of(t.get("create_snapshot")) != "off"]
        if asks:
            assert re.search(r"(?m)^#?\s*\[collect\]", path.read_text()), f"{path}: {asks} set create_snapshot but no [collect] section"
