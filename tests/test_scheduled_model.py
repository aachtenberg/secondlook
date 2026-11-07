"""[agent] scheduled_*: runs the schedule starts can use a cheaper or local model; everything else keeps the main one."""
from dbtriage.config import Config, Target
from dbtriage.llm import agent_for, scheduled_problem

MAIN = {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1", "effort": "high", "token_budget": 200000, "max_iterations": 20}


def test_only_scheduled_runs_take_the_scheduled_model():
    cfg = {**MAIN, "scheduled_provider": "ollama", "scheduled_model": "gemma4:26b",
           "scheduled_base_url": "http://ollama.lan:11434/v1"}
    for source in ("web", "cli", "webhook", None):
        assert agent_for(cfg, source) is cfg
    a = agent_for(cfg, "schedule")
    # a different provider: none of the main provider's key, URL or effort carry over; the budget and iterations do
    assert a == {"provider": "ollama", "model": "gemma4:26b", "base_url": "http://ollama.lan:11434/v1",
                 "token_budget": 200000, "max_iterations": 20}
    # the same provider, a cheaper model: everything else is the main one's
    a = agent_for({**MAIN, "scheduled_model": "deepseek/deepseek-v4.1-flash"}, "schedule")
    assert a == {**MAIN, "model": "deepseek/deepseek-v4.1-flash"}
    assert agent_for(MAIN, "schedule") is MAIN  # nothing scheduled set: the main model


def test_a_different_scheduled_provider_needs_its_model():
    assert scheduled_problem({**MAIN, "scheduled_provider": "ollama"})
    assert scheduled_problem({**MAIN, "scheduled_provider": "openrouter"}) is None  # same provider: the main model
    assert scheduled_problem({**MAIN, "scheduled_provider": "ollama", "scheduled_model": "gemma4:26b"}) is None


def test_the_settings_card_saves_and_checks_them(tmp_path):
    from dbtriage.settings import Settings
    from tests.test_settings_charts import seed

    cfg = seed(tmp_path)
    cfg.agent = dict(MAIN)
    st = Settings.for_config(cfg)
    assert st.put_agent({"scheduled_provider": "nope"}) == ["scheduled_provider: one of " + ", ".join(__import__("dbtriage.settings").settings.AGENT_PROVIDERS)]
    assert st.put_agent({"scheduled_provider": "ollama"}, base=cfg.agent) == ["scheduled_model: required when scheduled_provider differs from provider"]
    assert st.put_agent({"scheduled_provider": "ollama", "scheduled_model": "gemma4:26b",
                         "scheduled_base_url": "http://ollama.lan:11434/v1"}, base=cfg.agent) == []
    assert st.data["agent"]["scheduled_model"] == "gemma4:26b"


def test_a_scheduled_run_reviews_with_the_scheduled_model(tmp_path, monkeypatch):
    from dbtriage import cli, llm
    from tests.conftest import seed_store

    st = seed_store(tmp_path / "data")
    cfg = Config(storage_url="x", targets={"demo": Target("demo", "postgres", "d")}, base_dir=str(tmp_path),
                 agent={**MAIN, "scheduled_provider": "ollama", "scheduled_model": "gemma4:26b"})
    used = []

    def backend(a):
        used.append((a["provider"], a["model"]))
        raise RuntimeError("no model in tests")  # the run falls back to the rules review

    monkeypatch.setattr(llm, "make_backend", backend)
    sid, base = "postgres/demo/2026-11-05/20261105T100000Z", "postgres/demo/2026-11-05/20261105T090000Z"
    cli.triage(cfg, st, sid, base, "scheduled", True, source="schedule")
    cli.triage(cfg, st, sid, base, "scheduled", True, source="web")
    assert used == [("ollama", "gemma4:26b"), ("openrouter", "deepseek/deepseek-v4-pro")]


def test_the_settings_page_shows_them(tmp_path):
    from tests.test_overlay_semantics import app_for

    p = tmp_path / "targets.toml"
    p.write_text('[agent]\nprovider = "openrouter"\nmodel = "m"\nscheduled_provider = "ollama"\nscheduled_model = "gemma4:26b"\n'
                 '[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    page = app_for(p).settings_page().decode()
    assert 'name="scheduled_model" value="gemma4:26b"' in page and "<option value='ollama' selected>ollama</option>" in page
    assert "(same as above)" in page and '<fieldset class="chan"><legend>Scheduled runs' in page


def test_a_half_set_scheduled_provider_is_refused_at_load_and_webhooks_cannot_claim_the_schedule(tmp_path):
    """targets.toml with scheduled_provider but no scheduled_model degraded every scheduled review to the rules
    silently; a webhook body saying source "schedule" picked the scheduled model (agent review on #15, ffd6dd9)."""
    import pytest

    from dbtriage.config import load_config
    from dbtriage.triggers import parse_payload

    p = tmp_path / "targets.toml"
    p.write_text('[agent]\nprovider = "openrouter"\nmodel = "m"\nscheduled_provider = "ollama"\n'
                 '[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    with pytest.raises(SystemExit, match="scheduled_model: required"):
        load_config(p)
    assert parse_payload({"target": "demo", "source": "schedule"}).source == "webhook"
    assert parse_payload({"target": "demo", "source": "pagerduty"}).source == "pagerduty"
