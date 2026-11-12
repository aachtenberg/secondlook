"""The record you can hand someone: dbtriage.bundle, its CLI and its web route."""
import base64
import hashlib
import io
import json
import threading
import urllib.request
import zipfile
from urllib.error import HTTPError

import pytest

from dbtriage import bundle, cli
from dbtriage.config import Config, Target
from dbtriage.storage import LocalStorage

SID = "postgres/demo/2026-11-05/20261105T100000Z"
BASE = "postgres/demo/2026-11-05/20261105T090000Z"
DSN = "postgresql://monitor:hunter2@db.internal:5432/app"


def store(tmp_path) -> LocalStorage:
    """One reviewed snapshot with every file the store can hold, and a manifest carrying secrets no real collector
    writes, so the bundle has to prove it drops them."""
    st = LocalStorage(tmp_path / "data")
    st.put_json(f"{BASE}/manifest.json", {"id": BASE, "target": "demo", "engine": "postgres", "captured_at": "2026-11-05T09:00:00+00:00",
                                          "window": {"mode": "scheduled", "minutes": 60}, "trigger": {"source": "scheduler", "reason": ""}, "reports": []})
    st.put_json(f"{SID}/manifest.json", {
        "id": SID, "target": "demo", "engine": "postgres", "captured_at": "2026-11-05T10:00:00+00:00",
        "window": {"mode": "incident", "minutes": 60, "start": "2026-11-05T09:00:00+00:00", "end": "2026-11-05T10:00:00+00:00"},
        "trigger": {"source": "webhook", "reason": "latency alert", "actor": "pagerduty"},
        "baseline": {"snapshot": BASE, "policy": "previous", "reason": "previous snapshot"},
        "files": ["summary.json", "raw.json", "changes.json"], "reports": ["awr.txt", "addm.txt"], "changes_sources": ["github"],
        "dsn": DSN, "feed": {"url": "https://ci:tok3n@ci.example/deploys", "api_token": "ghp_secret"},  # never written by collect
    })
    st.put_json(f"{SID}/summary.json", {"captured_at": "2026-11-05T10:00:00+00:00", "top_sql": [{"sql_id": "q1", "text": "select ? from t where id = ?"}]})
    st.put_json(f"{SID}/raw.json", {"huge": "x" * 5000})
    st.put_json(f"{SID}/compare.json", {"findings": [{"severity": "high", "kind": "sql_regressed", "title": "q1 is 3x slower", "evidence": {"sql_id": "q1"}},
                                                     {"severity": "medium", "kind": "blocking", "title": "blocking on orders", "evidence": {}}],
                                        "settings_changed": [{"name": "work_mem", "before": "4MB", "after": "64MB"}],
                                        "reference": {"snapshot": BASE, "policy": "previous"}})
    st.put_json(f"{SID}/triage.json", {
        "severity": "high", "title": "q1 regressed after the 1.4.2 deploy", "source": "agent", "confidence": "high",
        "summary": "q1 is three times slower since billing@1.4.2 landed.", "checked": "load profile, top SQL, change feed",
        "provider": "anthropic", "model": "claude-test-1", "usage": {"input_tokens": 12345, "output_tokens": 678, "token_budget": 200000},
        "tool_calls": 3, "tools_file": "tools.json", "duration_s": 12.5,
        "findings": [{"severity": "high", "kind": "sql_regressed", "subject": "q1", "where": "sql_id q1", "summary": "q1 is 3x slower", "impact": "i", "evidence": ["e"], "fix": "f"},
                     {"severity": "low", "kind": "other", "subject": "", "where": "table t", "summary": "bloat on t", "impact": "i", "evidence": [], "fix": "f"}],
        "heuristics": [{"severity": "high", "kind": "sql_regressed", "title": "q1 is 3x slower"}, {"severity": "medium", "kind": "blocking", "title": "blocking on orders"}],
        "agreement": [{"mark": "both", "kind": "sql_regressed", "subject": "q1", "model": 0, "rules": 0, "severity": "high"},
                      {"mark": "model_only", "kind": "other", "subject": "", "model": 1, "rules": None, "severity": "low"},
                      {"mark": "rules_only", "kind": "blocking", "subject": "orders", "model": None, "rules": 1, "severity": "medium"}],
        "life": [{"state": "still_open", "first_seen": BASE, "first_seen_at": "2026-11-05T09:00:00+00:00", "misses": 0, "returns": 0, "period_s": None,
                  "tickets": {"jira": {"key": "DB-42", "id": "10042", "url": "https://jira.example/browse/DB-42"}}},
                 {"state": "new", "first_seen": SID, "first_seen_at": "2026-11-05T10:00:00+00:00", "misses": 0, "returns": 0, "period_s": None, "tickets": {}}],
        "still_open": [{"kind": "wait_shift", "subject": "IO", "severity": "medium", "title": "IO waits up", "state": "still_open", "first_seen_at": "2026-11-04T10:00:00+00:00"}],
        "gone": [{"kind": "deadlocks", "subject": "", "severity": "medium", "title": "deadlocks"}],
        "delivered": ["slack", "jira"], "target": "demo", "engine": "postgres", "snapshot": SID,
    })
    st.put_text(f"{SID}/triage.md", "**[HIGH] demo** - q1 regressed\n")
    st.put_json(f"{SID}/tools.json", {"snapshot": SID, "provider": "anthropic", "model": "claude-test-1",
                                      "calls": [{"name": "compare_snapshots", "args": {}, "result": "..."}, {"name": "get_report", "args": {"section": "top_sql"}, "result": "..."},
                                                {"name": "get_recent_changes", "args": {}, "result": "..."}]})
    st.put_json(f"{SID}/changes.json", [{"id": "billing@1.4.2", "at": "2026-11-05T09:54:00+00:00", "source": "github"}])
    st.put_text(f"{SID}/awr.txt", "WORKLOAD REPOSITORY\nwhere name = '[masked]'\n")
    st.put_text(f"{SID}/addm.txt", "ADDM\n")
    st.put_json(f"{SID}/chat/review.json", {"snapshot": SID, "key": "review", "messages": [
        {"role": "user", "text": "why now?", "at": "2026-11-05T10:05:00+00:00"},
        {"role": "assistant", "text": "the deploy", "at": "2026-11-05T10:05:09+00:00", "provider": "anthropic", "model": "claude-test-1"}]})
    st.put_json(f"{SID}/chat/finding-1.json", {"snapshot": SID, "key": "finding-1", "messages": [{"role": "user", "text": "fix?", "at": "2026-11-05T10:06:00+00:00"}]})
    return st


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def test_members_readme_and_checksums(tmp_path):
    st = store(tmp_path)
    data = bundle.build(st, SID)
    files = unzip(data)
    assert sorted(files) == ["README.md", "addm.txt", "awr.txt", "changes.json", "chat/finding-1.json", "chat/review.json", "checksums.txt",
                             "compare.json", "manifest.json", "summary.json", "tools.json", "triage.json", "triage.md"]
    # stored files are byte for byte what the store holds (the manifest is the one exception, below)
    for name in ("summary.json", "compare.json", "triage.json", "triage.md", "tools.json", "changes.json", "awr.txt", "addm.txt", "chat/review.json"):
        assert files[name] == st.get(f"{SID}/{name}")
    sums = dict(line.split("  ", 1)[::-1] for line in files["checksums.txt"].decode().splitlines())
    assert set(sums) == set(files) - {"checksums.txt"}
    assert all(hashlib.sha256(files[n]).hexdigest() == h for n, h in sums.items())
    md = files["README.md"].decode()
    assert md.startswith(f"# Second Look snapshot {SID}") and "Target `demo` (postgres), captured 2026-11-05T10:00:00+00:00" in md
    assert "- Mode: incident, a 60-minute window" in md and "- Source: webhook, by pagerduty" in md and "- Reason: latency alert" in md
    assert f"- Baseline `{BASE}`, policy previous: previous snapshot" in md and "Parameters changed since the baseline: 1 (work_mem)" in md
    assert "Change feed read at capture: github (1 entry in changes.json)" in md
    assert "| 1 | high | both | still open since 2026-11-05 | sql_id q1 | q1 is 3x slower | jira DB-42 (https://jira.example/browse/DB-42) |" in md
    assert "| 2 | low | model only | new | table t | bloat on t |  |" in md
    assert "Agreement with the rules: 1 both, 1 model only, 1 rules only." in md and "- medium `blocking` blocking on orders" in md
    assert "model `claude-test-1` via anthropic; 13,023 tokens (12,345 in, 678 out); 3 tool calls (the transcript is tools.json); 12.5s" in md
    assert "Tools used: compare_snapshots, get_recent_changes, get_report" in md
    assert "- Delivered to: slack, jira" in md and "- Finding 1: jira DB-42 (https://jira.example/browse/DB-42)" in md
    assert "Still open: medium `wait_shift IO` IO waits up, since 2026-11-04" in md and "Cleared this run: `deadlocks` deadlocks" in md
    assert "`chat/review.json`: 1 question about the whole review, last at 2026-11-05T10:05:09+00:00" in md and "1 question about finding 1" in md
    assert "- `awr.txt`: Oracle AWR report (literals masked)" in md and "- `chat/review.json`: discussion thread (2 messages)" in md and "discussion thread (1 message)" in md
    assert "sha256sum -c checksums.txt" in md and "- `raw.json`, the engine-native payload: it can be large" in md


def test_secrets_are_dropped_and_named(tmp_path):
    st = store(tmp_path)
    files = unzip(bundle.build(st, SID, include_raw=True))
    blob = b"".join(files.values()).decode(errors="replace")
    for secret in (DSN, "hunter2", "tok3n", "ghp_secret", "ci:tok3n@"):
        assert secret not in blob
    m = json.loads(files["manifest.json"])
    assert "dsn" not in m and m["feed"] == {"url": "[credentialed url dropped]"} and m["target"] == "demo" and m["reports"] == ["awr.txt", "addm.txt"]
    assert "also withheld from manifest.json: dsn, feed.api_token, feed.url" in files["README.md"].decode()
    # an honest manifest is kept byte for byte
    m2 = {k: v for k, v in st.get_json(f"{SID}/manifest.json").items() if k not in ("dsn", "feed")}
    st.put_json(f"{SID}/manifest.json", m2)
    files = unzip(bundle.build(st, SID))
    assert files["manifest.json"] == st.get(f"{SID}/manifest.json") and "withheld" not in files["README.md"].decode()


def test_raw_flag_and_determinism(tmp_path):
    st = store(tmp_path)
    a, b = bundle.build(st, SID), bundle.build(st, SID)
    assert a == b and "raw.json" not in unzip(a)
    with_raw = bundle.build(st, SID, include_raw=True)
    files = unzip(with_raw)
    assert files["raw.json"] == st.get(f"{SID}/raw.json") and "(included on request)" in files["README.md"].decode()
    assert with_raw == bundle.build(st, SID, include_raw=True) and with_raw != a
    with zipfile.ZipFile(io.BytesIO(a)) as z:
        assert {i.date_time for i in z.infolist()} == {bundle.ZIP_TIME} and [i.filename for i in z.infolist()][:2] == ["README.md", "checksums.txt"]
    with pytest.raises(FileNotFoundError):
        bundle.build(st, "postgres/demo/2026-11-05/nope")
    with pytest.raises(FileNotFoundError):
        bundle.build(st, "../etc")


def test_untriaged_snapshot_still_bundles(tmp_path):
    st = store(tmp_path)
    md = unzip(bundle.build(st, BASE))["README.md"].decode()
    assert "Not reviewed: there is no triage.json." in md and "- No baseline: nothing earlier to compare with" in md
    assert "No discussion threads were stored with this snapshot." in md and "- No compare.json: the snapshot was not compared" in md


def test_cli_writes_the_zip(tmp_path, monkeypatch, capsys):
    store(tmp_path)
    (tmp_path / "targets.toml").write_text(f'[storage]\nurl = "file://{tmp_path / "data"}"\n[targets.demo]\nengine = "postgres"\ndsn = "x"\n')
    monkeypatch.chdir(tmp_path)
    cli.main(["--config", str(tmp_path / "targets.toml"), "bundle", SID])
    out = tmp_path / "postgres-demo-20261105T100000Z.zip"
    assert out.exists() and out.read_bytes() == bundle.build(LocalStorage(tmp_path / "data"), SID)
    assert capsys.readouterr().out.strip() == f"{out.name}  {out.stat().st_size:,} bytes"
    cli.main(["--config", str(tmp_path / "targets.toml"), "bundle", SID, "--out", str(tmp_path / "x.zip"), "--raw"])
    assert "raw.json" in unzip((tmp_path / "x.zip").read_bytes())
    with pytest.raises(SystemExit, match="no such snapshot"):
        cli.main(["--config", str(tmp_path / "targets.toml"), "bundle", "postgres/demo/2026-11-05/nope"])


@pytest.fixture
def server(tmp_path):
    from dbtriage.web import make_server
    from tests.conftest import free_port, wait_for

    cfg = Config(storage_url="x", targets={"demo": Target("demo", "postgres", "dsn")}, web={}, base_dir=str(tmp_path))
    srv = make_server(cfg, store(tmp_path), "127.0.0.1", free_port(), False, "s3cret", False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    yield url
    srv.shutdown()
    srv.server_close()


def get(url, auth=True):
    headers = {"Authorization": "Basic " + base64.b64encode(b"x:s3cret").decode()} if auth else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
        return r.status, r.headers, r.read()


def test_web_route_serves_the_zip(server, tmp_path):
    status, headers, body = get(f"{server}/bundle/{SID}")
    assert status == 200 and headers["Content-Type"] == "application/zip" and headers["Cache-Control"] == "no-store"
    assert headers["Content-Disposition"] == 'attachment; filename="postgres-demo-20261105T100000Z.zip"'
    assert body == bundle.build(LocalStorage(tmp_path / "data"), SID) and "raw.json" not in unzip(body)
    assert "raw.json" in unzip(get(f"{server}/bundle/{SID}?raw=1")[2])  # raw.json is in the default drawer list
    page = get(f"{server}/s/{SID}")[2].decode()
    assert f'<a class="btn" id="bundle-link" href="/bundle/{SID}" download>' in page and "Download bundle" in page
    with pytest.raises(HTTPError) as e:
        get(f"{server}/bundle/{SID}", auth=False)
    assert e.value.code == 401
    for bad in ("postgres/demo/2026-11-05/nope", "../etc/passwd", ""):
        with pytest.raises(HTTPError) as e:
            get(f"{server}/bundle/{bad}")
        assert e.value.code == 404
