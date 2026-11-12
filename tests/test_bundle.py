"""The record you can hand someone: dbtriage.bundle, its CLI and its web route."""
import base64
import contextlib
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
from dbtriage.storage import LocalStorage, StorageUnavailable

SID = "postgres/demo/2026-11-05/20261105T100000Z"
BASE = "postgres/demo/2026-11-05/20261105T090000Z"
DSN = "postgresql://monitor:hunter2@db.internal:5432/app"
KEYWORD_DSN = "host=db.internal user=monitor password=hunter2 dbname=app"
HONEST_REASON = "latency alert"


def store(tmp_path) -> LocalStorage:
    """One reviewed snapshot with every file the store can hold, and credentials no real collector writes (secret-named
    manifest keys, a keyword DSN in the trigger's reason, a token in a feed link, a password in a chat message), so the
    bundle has to prove it drops or masks them. token_env names an environment variable, not a secret, and stays."""
    st = LocalStorage(tmp_path / "data")
    st.put_json(f"{BASE}/manifest.json", {"id": BASE, "target": "demo", "engine": "postgres", "captured_at": "2026-11-05T09:00:00+00:00",
                                          "window": {"mode": "scheduled"}, "trigger": {"source": "scheduler", "reason": ""}, "reports": []})
    st.put_json(f"{SID}/manifest.json", {
        "id": SID, "target": "demo", "engine": "postgres", "captured_at": "2026-11-05T10:00:00+00:00",
        "window": {"mode": "incident", "start": "2026-11-05T09:00:00+00:00", "end": "2026-11-05T10:00:00+00:00"},
        "trigger": {"source": "webhook", "reason": f"{HONEST_REASON}; collector ran with {KEYWORD_DSN}", "actor": "pagerduty"},
        "baseline": {"snapshot": BASE, "policy": "previous", "reason": "previous snapshot"},
        "files": ["summary.json", "raw.json", "changes.json"], "reports": ["awr.txt", "addm.txt"], "changes_sources": ["github"],
        "dsn": DSN, "feed": {"url": "https://ci:tok3n@ci.example/deploys", "api_token": "ghp_secret", "token_env": "GITHUB_TOKEN"},  # never written by collect
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
                                      "calls": [{"n": 1, "tool": "compare_snapshots", "args": {}, "result": "...", "ms": 3},
                                                {"n": 2, "tool": "get_report", "args": {"section": "top_sql"}, "result": "...", "ms": 2},
                                                {"n": 3, "tool": "get_recent_changes", "args": {}, "result": "...", "ms": 1}]})
    st.put_json(f"{SID}/changes.json", [{"id": "billing@1.4.2", "at": "2026-11-05T09:54:00+00:00", "source": "github",
                                         "url": "https://x-access-token:ghs_l1nkt0ken@github.com/org/billing/commit/abc"}])
    st.put_text(f"{SID}/awr.txt", "WORKLOAD REPOSITORY\nwhere name = '[masked]'\n")
    st.put_text(f"{SID}/addm.txt", "ADDM\n")
    st.put_json(f"{SID}/chat/review.json", {"snapshot": SID, "key": "review", "messages": [
        {"role": "user", "text": "why now? the app connects with password=hunter2", "at": "2026-11-05T10:05:00+00:00"},
        {"role": "assistant", "text": "the deploy", "at": "2026-11-05T10:05:09+00:00", "provider": "anthropic", "model": "claude-test-1"}]})
    st.put_json(f"{SID}/chat/finding-1.json", {"snapshot": SID, "key": "finding-1", "messages": [{"role": "user", "text": "fix?", "at": "2026-11-05T10:06:00+00:00"}]})
    return st


def honest(st: LocalStorage) -> None:
    """Take the planted credentials back out of the manifest, so it is what collect really writes."""
    m = {k: v for k, v in st.get_json(f"{SID}/manifest.json").items() if k not in ("dsn", "feed")}
    m["trigger"]["reason"] = HONEST_REASON
    st.put_json(f"{SID}/manifest.json", m)


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {i.filename: z.read(i) for i in z.infolist()}


def test_members_readme_and_checksums(tmp_path):
    st = store(tmp_path)
    data = bundle.build(st, SID)
    files = unzip(data)
    assert sorted(files) == ["README.md", "addm.txt", "awr.txt", "changes.json", "chat/finding-1.json", "chat/review.json", "checksums.txt",
                             "compare.json", "manifest.json", "summary.json", "tools.json", "triage.json", "triage.md"]
    # stored files are byte for byte what the store holds, except the three that carry a planted credential (below)
    for name in ("summary.json", "compare.json", "triage.json", "triage.md", "tools.json", "awr.txt", "addm.txt", "chat/finding-1.json"):
        assert files[name] == st.get(f"{SID}/{name}")
    sums = dict(line.split("  ", 1)[::-1] for line in files["checksums.txt"].decode().splitlines())
    assert set(sums) == set(files) - {"checksums.txt"}
    assert all(hashlib.sha256(files[n]).hexdigest() == h for n, h in sums.items())
    md = files["README.md"].decode()
    assert md.startswith(f"# Second Look snapshot {SID}") and "Target `demo` (postgres), captured 2026-11-05T10:00:00+00:00" in md
    assert "- Mode: incident from 2026-11-05T09:00:00+00:00 to 2026-11-05T10:00:00+00:00" in md and "minute" not in md
    assert "- Source: webhook, by pagerduty" in md and "- Reason: latency alert; collector ran with host=db.internal user=monitor password=*** dbname=app" in md
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
    assert "no connection string" not in md and "never holds" not in md  # the README claims only what the export did


def test_secrets_are_dropped_or_masked_and_named(tmp_path):
    st = store(tmp_path)
    files = unzip(bundle.build(st, SID, include_raw=True))
    blob = b"".join(files.values()).decode(errors="replace")
    for secret in (DSN, "hunter2", "tok3n", "ghp_secret", "ci:tok3n@", "ghs_l1nkt0ken", "x-access-token"):
        assert secret not in blob
    m = json.loads(files["manifest.json"])
    assert "dsn" not in m and m["target"] == "demo" and m["reports"] == ["awr.txt", "addm.txt"]
    assert m["feed"] == {"url": "https://***@ci.example/deploys", "token_env": "GITHUB_TOKEN"}  # an env var's name is not a secret
    assert m["trigger"]["reason"] == f"{HONEST_REASON}; collector ran with host=db.internal user=monitor password=*** dbname=app"
    assert json.loads(files["changes.json"])[0]["url"] == "https://***@github.com/org/billing/commit/abc"
    assert json.loads(files["chat/review.json"])["messages"][0]["text"] == "why now? the app connects with password=***"
    md = files["README.md"].decode()
    assert "- Secret-named keys withheld from manifest.json: dsn, feed.api_token" in md
    assert "- Credential-shaped values were masked (replaced by `***`): 1 in changes.json, 1 in chat/review.json, 2 in manifest.json" in md
    assert "found nothing to mask" not in md
    # an honest manifest is kept byte for byte, and the README says the scan found nothing
    honest(st)
    files = unzip(bundle.build(st, SID))
    md = files["README.md"].decode()
    assert files["manifest.json"] == st.get(f"{SID}/manifest.json") and "withheld" not in md
    assert "1 in changes.json, 1 in chat/review.json" in md and "manifest.json" not in md.split("masked (replaced")[1].split("\n")[0]


def test_mask_shapes():
    aws = "AKIA" + "ABCDEFGHIJKLMNOP"  # assembled at run time: a literal of this shape would trip the repo's secrets scan
    text = ('Bearer abcdefghijklmnop ' + aws + ' sk-abcdefghijklmnopqrstuvwxyz pwd=x1 "token": "t0k" '
            '"token_env": "GITHUB_TOKEN" "token_budget": 200000 "input_tokens": 12345 12,345 tokens')
    out, n = bundle.mask(text)
    assert n == 5 and out == ('Bearer *** *** *** pwd=*** "token": "***" "token_env": "GITHUB_TOKEN" "token_budget": 200000 '
                              '"input_tokens": 12345 12,345 tokens')
    dropped: list[str] = []
    kept = bundle.scrub({"token_env": "A", "secret_name": "B", "password_file": "C", "db_password": "D", "tools_file": "E", "x": {"Authorization": "F"}}, dropped)
    assert kept == {"token_env": "A", "secret_name": "B", "password_file": "C", "tools_file": "E", "x": {}} and dropped == ["db_password", "x.Authorization"]


def test_raw_flag_and_determinism(tmp_path):
    st = store(tmp_path)
    a, b = bundle.build(st, SID), bundle.build(st, SID)
    assert a == b and "raw.json" not in unzip(a)
    with_raw = bundle.build(st, SID, include_raw=True)
    files = unzip(with_raw)
    md = files["README.md"].decode()
    assert files["raw.json"] == st.get(f"{SID}/raw.json") and "- `raw.json`: the engine-native payload the summary was built from (literals masked); included on request" in md
    assert "`raw.json`, the engine-native payload" not in md.split("## What is not here")[1]  # listed as present, so not as absent too
    assert "- `raw.json`, the engine-native payload: not stored with this snapshot" in unzip(bundle.build(st, BASE, include_raw=True))["README.md"].decode()
    assert with_raw == bundle.build(st, SID, include_raw=True) and with_raw != a
    with zipfile.ZipFile(io.BytesIO(a)) as z:
        assert {i.date_time for i in z.infolist()} == {bundle.ZIP_TIME} and [i.filename for i in z.infolist()][:2] == ["README.md", "checksums.txt"]
        assert {i.create_system for i in z.infolist()} == {3}  # the same header on a Windows and a Linux export
    with pytest.raises(FileNotFoundError):
        bundle.build(st, "postgres/demo/2026-11-05/nope")
    with pytest.raises(FileNotFoundError):
        bundle.build(st, "../etc")


def test_untriaged_snapshot_still_bundles(tmp_path):
    st = store(tmp_path)
    md = unzip(bundle.build(st, BASE))["README.md"].decode()
    assert "Not reviewed: there is no triage.json." in md and "- No baseline: nothing earlier to compare with" in md
    assert "No discussion threads were stored with this snapshot." in md and "- No compare.json: the snapshot was not compared" in md
    assert "- Credentials: the scan for secret-named manifest keys and credential-shaped values found nothing to mask" in md


class NoListing(LocalStorage):
    """A bucket that allows GetObject and denies ListBucket."""

    def list_keys(self, prefix):
        raise PermissionError("AccessDenied: ListBucket")


class LyingExists(LocalStorage):
    """A store whose head requests fail while its reads work (S3Storage.exists on any ClientError)."""

    def exists(self, key):
        return False


class Down(LocalStorage):
    """An outage: every remote call raises."""

    def get(self, key):
        raise StorageUnavailable("connection refused")

    def get_json(self, key):
        raise StorageUnavailable("connection refused")

    def exists(self, key):
        return False  # ResilientStorage.exists during an outage

    def list_keys(self, prefix):
        return []  # ResilientStorage.list_keys during an outage


def test_listing_failure_is_said_not_hidden(tmp_path):
    store(tmp_path)
    files = unzip(bundle.build(NoListing(tmp_path / "data"), SID))
    assert sorted(files) == ["README.md", "addm.txt", "awr.txt", "changes.json", "checksums.txt", "compare.json", "manifest.json",
                             "summary.json", "tools.json", "triage.json", "triage.md"]  # threads live only in the listing
    md = files["README.md"].decode()
    assert "No discussion threads were stored" not in md
    assert "The store could not be listed at export time, so discussion threads and any other file not named in the manifest may be missing from this bundle." in md
    assert "- Whatever only a listing of the store would have named: the store could not be listed at export time" in md


def test_exists_that_lies_does_not_thin_the_record(tmp_path):
    st = store(tmp_path)
    assert bundle.build(LyingExists(tmp_path / "data"), SID) == bundle.build(st, SID)


def test_listing_is_kept_to_the_snapshot(tmp_path):
    st = store(tmp_path)
    st.put_text(f"{SID}-old/stray.txt", "x")  # the same prefix string, another snapshot
    assert "stray.txt" not in unzip(bundle.build(st, SID)) and "../stray.txt" not in unzip(bundle.build(st, SID))


def test_outage_is_not_a_missing_snapshot(tmp_path, monkeypatch):
    store(tmp_path)
    with pytest.raises(StorageUnavailable):
        bundle.build(Down(tmp_path / "data"), SID)
    (tmp_path / "targets.toml").write_text(f'[storage]\nurl = "file://{tmp_path / "data"}"\n[targets.demo]\nengine = "postgres"\ndsn = "x"\n')
    monkeypatch.setattr(cli, "open_storage", lambda url, spool=None: Down(tmp_path / "data"))
    with pytest.raises(SystemExit, match="the store is unavailable: connection refused"):
        cli.main(["--config", str(tmp_path / "targets.toml"), "bundle", SID])
    with serve(tmp_path, Down(tmp_path / "data")) as url:
        with pytest.raises(HTTPError) as e:
            get(f"{url}/bundle/{SID}")
        assert e.value.code == 503


def test_filename_is_kept_to_the_key_alphabet(tmp_path):
    assert bundle.filename({"engine": "postgres", "target": "demo"}, SID) == "postgres-demo-20261105T100000Z.zip"
    for bad in ("pg/x", 'de"mo', "a\r\nX-Injected: 1", "", None, "nul"):
        assert bundle.filename({"engine": bad, "target": bad}, SID) == "snapshot-snapshot-20261105T100000Z.zip"
    st = store(tmp_path)
    sid = "postgres/demo/2026-11-05/20261105T110000Z"
    st.put_json(f"{sid}/manifest.json", {"id": sid, "target": 'de"mo\r\nX-Injected: 1', "engine": "pg/../x", "captured_at": "2026-11-05T11:00:00+00:00"})
    with serve(tmp_path, st) as url:
        status, headers, body = get(f"{url}/bundle/{sid}")
    assert status == 200 and headers["Content-Disposition"] == 'attachment; filename="snapshot-snapshot-20261105T110000Z.zip"'
    assert "X-Injected" not in headers and "manifest.json" in unzip(body)


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


@contextlib.contextmanager
def serve(tmp_path, st, web: dict | None = None):
    from dbtriage.web import make_server
    from tests.conftest import free_port, wait_for

    cfg = Config(storage_url="x", targets={"demo": Target("demo", "postgres", "dsn")}, web=web or {}, base_dir=str(tmp_path))
    srv = make_server(cfg, st, "127.0.0.1", free_port(), False, "s3cret", False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    try:
        yield url
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def server(tmp_path):
    with serve(tmp_path, store(tmp_path)) as url:
        yield url


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


def test_web_route_keeps_to_the_drawer_list(tmp_path):
    """What the drawer hides, the bundle hides too, and the README names it; threads and the reports stay in."""
    st = store(tmp_path)
    with serve(tmp_path, st, web={"files": ["triage.md", "compare.json", "summary.json", "manifest.json", "changes.json"]}) as url:
        files = unzip(get(f"{url}/bundle/{SID}?raw=1")[2])
        with pytest.raises(HTTPError) as e:
            get(f"{url}/f/{SID}/tools.json")
        assert e.value.code == 404
    assert sorted(files) == ["README.md", "addm.txt", "awr.txt", "changes.json", "chat/finding-1.json", "chat/review.json", "checksums.txt",
                             "compare.json", "manifest.json", "summary.json", "triage.json", "triage.md"]  # triage.json: the snapshot API serves it whole
    md = files["README.md"].decode()
    assert "- Hidden by the server's `[web] files` setting, so not exported from the web: `raw.json`, `tools.json`" in md
    assert "3 tool calls; 12.5s" in md and "transcript is tools.json" not in md and "`raw.json`, the engine-native payload" not in md
    assert unzip(bundle.build(st, SID, allowed=lambda name: name != "awr.txt"))["README.md"].decode().count("`awr.txt`") == 1  # the CLI's own gate works too
