import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from dbtriage.deliver import deliver
from dbtriage.redact import redact_sql
from dbtriage.storage import LocalStorage
from dbtriage.triggers import parse_payload


def test_redact_literals():
    assert redact_sql("select * from t where name = 'bob' and id = 42 and x = 3.5") == \
        "select * from t where name = '?' and id = ? and x = ?"
    assert redact_sql("select $1, col2 from t2") == "select $1, col2 from t2"   # placeholders and identifiers survive
    assert redact_sql("  a   \n b ") == "a b"
    assert redact_sql(None) == ""


def test_local_storage_snapshots(tmp_path):
    st = LocalStorage(tmp_path)
    for ts in ("20261105T100000Z", "20261105T110000Z", "20261105T120000Z"):
        st.put_json(f"postgres/demo/2026-11-05/{ts}/manifest.json", {"id": ts})
    ids = st.list_snapshots("postgres", "demo")
    assert len(ids) == 3 and ids[-1].endswith("120000Z")
    assert st.latest("postgres", "demo") == ids[-1]
    assert st.previous("postgres", "demo", ids[-1]) == ids[1]
    assert st.previous("postgres", "demo", ids[0]) is None
    assert st.list_snapshots("postgres", "other") == []


@pytest.mark.parametrize("payload,target,source,mode", [
    ({"target": "prod1", "mode": "adhoc"}, "prod1", "webhook", "adhoc"),
    ({"receiver": "x", "alerts": [{"labels": {"alertname": "HighDBLatency", "target": "prod1"}, "annotations": {"summary": "p99 up"}}]}, "prod1", "alertmanager", "incident"),
    ({"title": "[FIRING] x", "orgId": 1, "alerts": [{"labels": {"db": "prod2"}}]}, "prod2", "grafana", "incident"),
    ({"alert_id": "1", "event_msg": "x", "tags": "env:prod,target:prod3"}, "prod3", "datadog", "incident"),
    ({"ProblemTitle": "Slow DB", "ProblemDetails": {"tags": [{"key": "target", "value": "prod4"}]}}, "prod4", "dynatrace", "incident"),
    ({"event": {"data": {"custom_details": {"target": "prod5"}}}}, "prod5", "pagerduty", "incident"),
])
def test_parse_alert_payloads(payload, target, source, mode):
    r = parse_payload(payload)
    assert (r.target, r.source, r.mode) == (target, source, mode)


def test_parse_rejects_unknown():
    with pytest.raises(ValueError):
        parse_payload({"hello": "world"})


def test_deliver_webhook_and_severity_gate():
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200); self.end_headers()

        def log_message(self, *a): pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    cfg = {"channels": ["webhook", "bogus"], "webhook": {"url": url, "min_severity": "medium"}}
    low = {"severity": "low", "target": "t", "engine": "postgres", "title": "x"}
    high = dict(low, severity="high")
    assert deliver(cfg, low, "md") == []
    assert deliver(cfg, high, "md") == ["webhook"]
    assert got[0]["triage"]["severity"] == "high" and got[0]["markdown"] == "md"
    srv.shutdown()
    srv.server_close()


def test_ticket_channels_default_to_high(monkeypatch):
    from dbtriage import deliver as d

    sent = []
    monkeypatch.setitem(d.CHANNELS, "jira", lambda cfg, t, md: sent.append(("jira", t["severity"])))
    monkeypatch.setitem(d.CHANNELS, "slack", lambda cfg, t, md: sent.append(("slack", t["severity"])))
    cfg = {"channels": ["jira", "slack"], "jira": {}, "slack": {}}
    for sev in ("low", "medium", "high"):
        d.deliver(cfg, {"severity": sev, "target": "t", "engine": "e", "title": "x"}, "md")
    assert sent == [("slack", "low"), ("slack", "medium"), ("jira", "high"), ("slack", "high")]
    sent.clear()
    d.deliver({"channels": ["jira"], "jira": {"min_severity": "medium"}}, {"severity": "medium", "target": "t", "engine": "e", "title": "x"}, "md")
    assert sent == [("jira", "medium")]  # an explicit setting wins


def test_pushgateway_counts_the_rules_findings_the_model_dropped(monkeypatch):
    from dbtriage import deliver as d

    sent = []

    class R:
        def read(self):
            return b""

    monkeypatch.setattr("dbtriage.egress.urlopen", lambda req, timeout: sent.append(req) or R())
    t = {"severity": "high", "target": "t", "engine": "postgres", "title": "x", "ts": 5,
         "agreement": [{"mark": "rules_only", "severity": "high"}, {"mark": "rules_only", "severity": "low"}, {"mark": "both", "severity": "high"}]}
    d.send_pushgateway({"url": "http://pg:9091"}, t, "md")
    body = sent[0].data.decode()
    assert 'dbtriage_severity{target="t",engine="postgres"} 3' in body and 'dbtriage_rules_dropped{target="t",engine="postgres"} 1' in body
    assert sent[0].full_url == "http://pg:9091/metrics/job/dbtriage/target/t" and sent[0].get_method() == "PUT"
    d.send_pushgateway({"url": "http://pg:9091"}, dict(t, agreement=None), "md")  # a rules-only review drops nothing
    assert 'dbtriage_rules_dropped{target="t",engine="postgres"} 0' in sent[1].data.decode()


def test_report_literals_are_masked_without_breaking_layout():
    from dbtriage.redact import mask_report_literals

    awr = ("SQL Id        Elapsed (s)  SQL Text\n"
           "d8vkav3yzdfuz      46.77  select count(*) from orders where note like '%Jane Doe%' and amount > 100\n"
           "abc                 1.00  update t set c = 'it''s' where id = 7\n"
           "Note: a stray ' quote on its own stays put\n")
    out = mask_report_literals(awr)
    assert "Jane Doe" not in out and "like '?' and amount > 100" in out and "set c = '?' where" in out
    assert out.splitlines()[0] == awr.splitlines()[0] and "a stray ' quote on its own stays put" in out  # layout and lone quotes kept
    assert mask_report_literals(None) == ""


def test_reviewer_reads_only_listed_reports(tmp_path):
    from dbtriage.agent import TriageAgent
    from dbtriage.storage import LocalStorage

    st = LocalStorage(tmp_path)
    sid = "oracle/o/2026-11-05/T1"
    st.put_json(f"{sid}/manifest.json", {"id": sid, "reports": ["awr.txt"]})
    st.put_text(f"{sid}/awr.txt", "WORKLOAD REPOSITORY report")
    st.put_json(f"{sid}/raw.json", {"dba_hist_sqlstat": [{"sql_text": "secret"}]})
    tool = next(t for t in TriageAgent(st, None).tool_specs("oracle", "o") if t.name == "get_raw_report")
    assert "WORKLOAD REPOSITORY" in tool.fn({"snapshot_id": sid, "name": "awr.txt"})
    assert tool.fn({"snapshot_id": sid, "name": "raw.json"}) == "no report 'raw.json'; available: ['awr.txt']"
    assert "secret" not in tool.fn({"snapshot_id": sid, "name": "../raw.json"})


def test_config_paths_and_missing_dsn(tmp_path, monkeypatch, capsys):
    from dbtriage.cli import collect
    from dbtriage.config import load_config
    from dbtriage.triggers import TriageRequest

    (tmp_path / "conf").mkdir()
    cfg_file = tmp_path / "conf" / "targets.toml"
    cfg_file.write_text('[storage]\nurl = "file://./data"\n[targets.a]\nengine = "postgres"\ndsn_env = "DBT_NOT_SET_A"\nchanges_file = "changes.json"\n'
                        '[targets.b]\nengine = "postgres"\ndsn = "postgresql://u@h/b"\n')
    monkeypatch.delenv("DBT_NOT_SET_A", raising=False)
    monkeypatch.delenv("DBTRIAGE_STORAGE", raising=False)
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    monkeypatch.chdir(tmp_path)  # paths must follow the config file, not the working directory
    cfg = load_config(cfg_file)
    assert cfg.storage_url == f"file://{tmp_path / 'conf' / 'data'}" and cfg.targets["a"].changes_file == str(tmp_path / "conf" / "changes.json")
    assert "DBT_NOT_SET_A is not set" in capsys.readouterr().err and cfg.targets["b"].dsn == "postgresql://u@h/b"  # the other target is fine
    with pytest.raises(SystemExit) as e:
        collect(cfg, None, TriageRequest(target="a"))
    assert "DBT_NOT_SET_A is not set" in str(e.value)


def test_blank_oracle_user_and_password_are_not_sent(monkeypatch):
    """An edit-form save stores user/password as "" when the credentials live in the DSN; the collector and the
    connection test must not pass explicit empty credentials to the driver (agent review on PR #1, 071f22b)."""
    import oracledb

    from dbtriage.collectors.oracle import OracleCollector
    from dbtriage.collectors.base import Window
    from dbtriage.config import Target
    from dbtriage.settings import probe

    seen = []

    def fake_connect(**kw):
        seen.append(kw)
        raise oracledb.InterfaceError("stop here")

    monkeypatch.setattr(oracledb, "connect", fake_connect)
    t = Target("o", "oracle", "dbtriage/pw@h:1521/S", extra={"user": "", "password": ""})
    assert probe(t)[0] is False
    from datetime import datetime, timezone
    with pytest.raises(Exception):
        OracleCollector(t).collect(Window(start=datetime.now(timezone.utc), end=datetime.now(timezone.utc)))
    assert len(seen) == 2 and all(k["user"] is None and k["password"] is None for k in seen)
