import json
import os

from dbtriage import charts
from dbtriage.config import Config, Target
from dbtriage.settings import Settings, mask_dsn


def seed(tmp_path):
    return Config(storage_url="x", targets={"pg": Target("pg", "postgres", "postgresql://u:pw@h/db")}, web={"brand": "File"}, base_dir=str(tmp_path))


def test_overlay_add_edit_delete(tmp_path):
    cfg = seed(tmp_path)
    st = Settings.for_config(cfg)
    assert st.path == tmp_path / "dbtriage-settings.json"
    assert st.put_target("bad name", {"engine": "postgres", "dsn": "x"}) and st.put_target("ok", {"engine": "db2", "dsn": "x"})
    assert st.put_target("ok", {"engine": "mysql"}) == ["set a dsn or a dsn_env"]
    assert st.put_target("ora1", {"engine": "oracle", "dsn": "h:1521/FREE", "user": "u", "password": "p", "top_n": "30",
                                  "links": [{"label": "g", "url": "https://g"}], "notes": "n"}) == []
    assert oct(st.path.stat().st_mode & 0o777) == "0o600"
    eff = st.apply(cfg)
    assert eff.targets["ora1"].engine == "oracle" and eff.targets["ora1"].top_n == 30 and eff.targets["ora1"].extra["password"] == "p"
    assert eff.targets["pg"].dsn == "postgresql://u:pw@h/db" and st.origin("pg", cfg) == "file" and st.origin("ora1", cfg) == "ui"
    # env-backed dsn, override of a seeded target, delete of a seeded target hides it but keeps the file intact
    os.environ["T_DSN"] = "mysql://a:b@c/d"
    assert st.put_target("my", {"engine": "mysql", "dsn_env": "T_DSN"}) == []
    assert st.put_target("pg", {"engine": "postgres", "dsn": "postgresql://new"}) == [] and st.origin("pg", cfg) == "ui (overrides file)"
    eff = Settings(st.path).apply(cfg)
    assert eff.targets["my"].dsn == "mysql://a:b@c/d" and eff.targets["pg"].dsn == "postgresql://new"
    st.delete_target("pg", seeded=True); st.delete_target("my", seeded=False)
    eff = st.apply(cfg)
    assert "pg" not in eff.targets and "my" not in eff.targets and json.loads(st.path.read_text())["targets"]["pg"] == {"deleted": True}
    st.put_web({"brand": "UI", "accent": "#000000", "bogus": "x"})
    assert st.apply(cfg).web == {"brand": "UI", "accent": "#000000"}


def test_mask_dsn():
    assert mask_dsn("postgresql://u:secret@h:5432/db") == "postgresql://u:***@h:5432/db"
    assert mask_dsn("mysql://a:b@c/d") == "mysql://a:***@c/d" and mask_dsn("host:1521/FREE") == "host:1521/FREE" and mask_dsn("scott/tiger@h:1521/FREE") == "scott/***@h:1521/FREE" and mask_dsn(None) == ""


def test_charts():
    strip = charts.severity_strip([{"id": "a/b", "severity": "high", "captured_at": "2026-11-05T10:00:00", "title": "t", "metric": "aas", "value": 2.0},
                                   {"id": "a/c", "severity": None, "captured_at": "2026-11-05T11:00:00", "title": None, "metric": "aas", "value": 1.0}])
    assert strip.count("<rect") == 2 and "fill='var(--high)'" in strip and "fill='var(--info)'" in strip and "href='/s/a/b'" in strip
    w = charts.waits_chart({"Scheduler": {"share": 0.96, "value": 39566}}, {"Commit": {"share": 0.64, "value": 47756}, "Scheduler": {"share": 0, "value": 1}})
    assert "Wait time by class" in w and "Scheduler" in w and "<b>96%</b>" in w and "vs 64%" in w and w.count("class='pair'") == 2 and "<svg" not in w
    d = charts.delta_chart({"db_cpu_ms": {"current_per_s": 634.0, "reference_per_s": 265.0, "pct": 139.2}, "xact_commit": {"current": 1, "reference": 100, "pct": -99.0},
                            "elapsed_s": {"pct": -0.4}, "nothing": {"pct": None}})
    assert d.count("class='one'") == 2 and "<i class='up' style='width:max(1px,100.0%)'>" in d and "<i class='down' style='width:max(1px,99.0%)'>" in d and "+139%" in d and "-99%" in d and "elapsed" not in d
    assert "<span class='pct'>50%</span>" in charts.share_bar(0.5) and "width:max(1px,50.0%)" in charts.share_bar(0.5)
    assert charts.stat_tiles({"high": 2}).count("class='tile'") == 4 and ">2<" in charts.stat_tiles({"high": 2})
    assert charts.fmt(1234567) == "1.2M" and charts.fmt(0.9634) == "0.96" and charts.fmt(None) == "-"
    assert charts.severity_strip([]) == "" and charts.waits_chart({}, {}) == "" and charts.delta_chart({}) == ""
    assert charts.waits_chart(None, None) == "" and charts.delta_chart(None) == ""  # postgres: no wait classes -> null in compare.json


def test_agent_and_delivery_overlay(tmp_path, monkeypatch):
    cfg = seed(tmp_path)
    cfg.agent = {"provider": "ollama", "model": "qwen3:8b"}
    cfg.delivery = {"channels": ["stdout"], "ntfy": {"url": "https://ntfy.sh/seeded"}}
    st = Settings.for_config(cfg)
    assert st.put_agent({"provider": "nope"}) and st.put_agent({"provider": "bedrock-converse", "model": "us.amazon.nova-lite-v1:0", "region": "us-east-1", "max_iterations": "12", "junk": "x"}) == []
    eff = st.apply(cfg)
    assert eff.agent == {"provider": "bedrock-converse", "model": "us.amazon.nova-lite-v1:0", "region": "us-east-1", "max_iterations": 12}
    assert st.put_delivery({"channels": ["ntfy", "bogus"]}) == ["unknown channel(s): bogus"]
    assert st.put_delivery({"channels": ["stdout", "slack"], "min_severity": "low", "slack": {"webhook_url": "https://hooks.slack.com/services/T/B/secret"},
                            "email": {"to": "a@x, b@x", "smtp_port": "25"}, "ntfy": {"topic": "t1"}}) == []
    eff = st.apply(cfg)
    assert eff.delivery["channels"] == ["stdout", "slack"] and eff.delivery["ntfy"] == {"url": "https://ntfy.sh/seeded", "topic": "t1"}
    assert eff.delivery["email"] == {"to": ["a@x", "b@x"], "smtp_port": 25} and eff.delivery["slack"]["webhook_url"].endswith("/secret")
    view = st.delivery_view(cfg)
    assert view["slack"]["webhook_url"] == "https://hooks.slack.com/***" and view["email"]["to"] == ["a@x", "b@x"]
    # a blank secret on re-save keeps the stored one
    assert st.put_delivery({"channels": ["slack"], "slack": {"webhook_url": ""}}) == [] and st.apply(cfg).delivery["slack"]["webhook_url"].endswith("/secret")
    assert st.put_delivery({"email": {"smtp_port": "x"}}) == ["email.smtp_port: must be a number"]
    # the CLI loader applies the same overlay
    (tmp_path / "targets.toml").write_text('[targets.pg]\nengine = "postgres"\ndsn = "postgresql://u:p@h/db"\n[agent]\nprovider = "ollama"\n')
    from dbtriage.config import load_config
    assert load_config(tmp_path / "targets.toml").agent["provider"] == "bedrock-converse"
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    assert load_config(tmp_path / "targets.toml").agent["provider"] == "ollama"


def test_mask_secret():
    from dbtriage.settings import mask_secret
    assert mask_secret("https://hooks.slack.com/services/T/B/x") == "https://hooks.slack.com/***" and mask_secret("tok") == "***" and mask_secret("") == ""


def test_ntfy_topic_and_test_channel(monkeypatch):
    import dbtriage.deliver as dl
    seen = {}
    monkeypatch.setattr(dl, "_post", lambda url, body, headers=None, json_body=True: seen.update(url=url, headers=headers) or "")
    tr = {"severity": "high", "title": "t", "target": "x", "engine": "pg", "ts": 1}
    assert dl.test_channel({"ntfy": {"url": "https://ntfy.sh/", "topic": "/abc"}}, "ntfy", tr, "md") == (True, "sent via ntfy")
    assert seen["url"] == "https://ntfy.sh/abc" and seen["headers"]["Markdown"] == "yes"
    assert dl.test_channel({}, "slack", tr, "md") == (False, "slack: missing setting 'webhook_url'")
    assert dl.test_channel({}, "nope", tr, "md")[0] is False


def test_test_model_with_fake_backend(monkeypatch):
    import dbtriage.llm as llm

    class Fake:
        name = "fake"
        def __init__(self, cfg): self.cfg = cfg
        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            assert tools == [] and max_iterations == 1
            return llm.AgentResult(text="OK\n", stop_reason="stop", model="fake-1", usage={"input_tokens": 3, "output_tokens": 1})

    monkeypatch.setattr(llm, "make_backend", lambda cfg: Fake(cfg))
    ok, msg = llm.test_model({"provider": "fake"})
    assert ok and "fake:fake-1 answered 'OK'" in msg and "3 in / 1 out" in msg
    monkeypatch.setattr(llm, "make_backend", lambda cfg: (_ for _ in ()).throw(SystemExit("[agent] set KEY")))
    assert llm.test_model({}) == (False, "[agent] set KEY")


def test_probe_reports_a_missing_dsn_instead_of_exiting():
    """A MySQL target whose dsn_env is unset has dsn "": _conn_kwargs raises SystemExit, which the probe let escape,
    crashing Test connection and the live tests' reachable() (agent review on #10, bdd6562)."""
    from dbtriage.config import Target
    from dbtriage.settings import probe

    ok, msg = probe(Target("mariadb-lab", "mysql", ""))
    assert ok is False and "SystemExit" in msg and "mysql://" in msg
