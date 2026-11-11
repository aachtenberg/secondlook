"""The change feed: each source's shape, a failure as one item, the window, and the capture storing it."""
import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dbtriage import changes
from dbtriage.config import Target

REPO = Path(__file__).resolve().parents[1]
END = datetime(2026, 11, 5, 10, 20, tzinfo=timezone.utc)
START = END - timedelta(hours=48)


def tgt(**extra):
    return Target("billing-db", "postgres", "dsn", extra=extra)


class _Resp(io.BytesIO):
    """What urlopen hands back: a context manager with read()."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_http(monkeypatch, routes: dict):
    """Serve each URL prefix from `routes`; record every request so a test can check headers and query strings."""
    calls = []

    def urlopen(req, timeout):
        calls.append(req)
        assert timeout == changes.TIMEOUT
        for prefix, body in routes.items():
            if req.full_url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return _Resp(json.dumps(body).encode())
        raise AssertionError(f"unexpected request {req.full_url}")

    monkeypatch.setattr(changes, "urlopen", urlopen)
    return calls


# --------------------------------------------------------------------------- file
def test_file_source_keeps_the_example_working():
    t = tgt(changes={"source": "file", "path": "examples/changes.json"})
    end = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    got = changes.fetch(t, end - timedelta(hours=48), end, base_dir=str(REPO))
    assert [(c["kind"], c["source"]) for c in got] == [("job", "file"), ("deploy", "file")]  # oldest first; maintenance is a job
    assert got[1]["description"].startswith("orders-api v2.14.0") and got[1]["link"].startswith("https://gitlab.example.com/")
    assert changes.fetch(t, end - timedelta(hours=1), end, base_dir=str(REPO)) == []  # both older than an hour


def test_changes_file_is_the_short_form_and_no_feed_is_empty():
    t = Target("d", "postgres", "dsn", changes_file=str(REPO / "examples/changes.json"))
    assert changes.spec_of(t) == {"source": "file", "path": str(REPO / "examples/changes.json")}
    assert len(changes.fetch(t, datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 6, tzinfo=timezone.utc))) == 2
    assert changes.spec_of(tgt()) is None and changes.fetch(tgt(), START, END) == []
    assert changes.spec_of(tgt(changes={"path": "x.json"})) is None  # a table without a source is not a feed


def test_file_errors_become_one_item(tmp_path, capsys):
    t = tgt(changes={"source": "file", "path": str(tmp_path / "missing.json")})
    got = changes.fetch(t, START, END)
    assert len(got) == 1 and got[0]["kind"] == "other" and got[0]["error"] and got[0]["description"].startswith("file: ")
    assert got[0]["time"] == END.isoformat() and "changes feed file for billing-db" in capsys.readouterr().err
    (tmp_path / "obj.json").write_text("{}")
    got = changes.fetch(tgt(changes={"source": "file", "path": str(tmp_path / "obj.json")}), START, END)
    assert "expected a JSON list" in got[0]["description"]
    got = changes.fetch(tgt(changes={"source": "jenkins"}), START, END)
    assert got[0]["description"].startswith("jenkins: unknown source")


# --------------------------------------------------------------------------- window and shape
def test_within_normalizes_filters_and_sorts():
    items = [{"time": "2026-11-05T10:00:00+00:00", "kind": "Deploy", "description": "b"},
             {"time": "2026-11-05T09:00:00", "kind": "release", "description": "a", "link": ""},   # naive: UTC
             {"time": "2026-11-03T09:00:00+00:00", "kind": "deploy", "description": "too old"},
             {"time": "soon", "kind": "deploy", "description": "unreadable time"}, "not a dict",
             {"time": "2026-11-05T10:30:00+00:00", "kind": "deploy", "description": "after the end"}]
    got = changes.within(items, END - timedelta(hours=2), END, "file")
    assert got == [{"time": "2026-11-05T09:00:00+00:00", "kind": "deploy", "description": "a", "link": None, "source": "file"},
                   {"time": "2026-11-05T10:00:00+00:00", "kind": "deploy", "description": "b", "link": None, "source": "file"}]
    assert [changes.kind_of(k) for k in ("deploy", "maintenance", "config", "switchover", "incident", None)] == \
        ["deploy", "job", "parameter", "failover", "other", "other"]
    assert changes.parse_time("2026-11-05T10:00:00Z") == datetime(2026, 11, 5, 10, tzinfo=timezone.utc)


def test_lookback_and_margin_defaults():
    assert changes.lookback(None) == timedelta(hours=48) and changes.lookback({"lookback_hours": 6}) == timedelta(hours=6)
    assert changes.correlate_minutes(None) == 30 and changes.correlate_minutes({"correlate_minutes": 5}) == 5
    assert changes.correlate_minutes({"correlate_minutes": ""}) == 30


# --------------------------------------------------------------------------- github
def test_github_deployments_with_statuses(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "t0k")
    api = "https://api.github.com/repos/acme/billing"
    deployments = [
        {"id": 3, "sha": "c3c3c3c3c3", "ref": "v1.4.3", "environment": "production", "created_at": "2026-11-05T11:00:00Z"},  # after the window
        {"id": 2, "sha": "b2b2b2b2b2", "ref": "v1.4.2", "environment": "production", "created_at": "2026-11-05T10:10:00Z"},
        {"id": 1, "sha": "a1a1a1a1a1", "ref": "v1.4.1", "environment": "production", "created_at": "2026-11-05T09:00:00Z"},
        {"id": 0, "sha": "0000000000", "ref": "v1.4.0", "environment": "production", "created_at": "2026-11-01T09:00:00Z"},  # older than start: stop
    ]
    calls = fake_http(monkeypatch, {
        f"{api}/deployments/2/statuses": [{"state": "success", "created_at": "2026-11-05T10:14:00Z", "environment_url": "https://billing.example.com"}],
        f"{api}/deployments/1/statuses": [{"state": "failure", "created_at": "2026-11-05T09:05:00Z"}, {"state": "pending", "created_at": "2026-11-05T09:01:00Z"}],
        f"{api}/deployments?": deployments,
    })
    t = tgt(changes={"source": "github", "repo": "acme/billing", "environment": "production", "token_env": "GH_TOKEN"})
    got = changes.fetch(t, END - timedelta(hours=2), END)
    assert got == [
        {"time": "2026-11-05T09:05:00+00:00", "kind": "deploy", "description": "billing@v1.4.1 to production (failure)",
         "link": "https://github.com/acme/billing/commit/a1a1a1a1a1", "source": "github"},  # requested at 09:00, failed at 09:05; no url from the status
        {"time": "2026-11-05T10:14:00+00:00", "kind": "deploy", "description": "billing@v1.4.2 to production",
         "link": "https://billing.example.com", "source": "github"},  # the success status is when it landed
    ]
    first = calls[0]
    assert first.full_url == f"{api}/deployments?per_page=100&environment=production"
    assert first.get_header("Authorization") == "Bearer t0k" and first.get_header("Accept") == "application/vnd.github+json"
    assert len(calls) == 3  # one statuses call per deployment in the window, none for the one after it or the one before start


def test_github_releases_when_asked_and_no_deployments(monkeypatch):
    api = "https://api.github.com/repos/acme/billing"
    calls = fake_http(monkeypatch, {
        f"{api}/deployments?": [],
        f"{api}/releases?": [{"tag_name": "1.4.2", "published_at": "2026-11-05T10:05:00Z", "html_url": "https://github.com/acme/billing/releases/tag/1.4.2"},
                             {"tag_name": "1.4.3", "draft": True, "created_at": "2026-11-05T10:06:00Z"}],
    })
    without = changes.fetch(tgt(changes={"source": "github", "repo": "acme/billing"}), START, END)
    assert without == [] and len(calls) == 1 and calls[0].get_header("Authorization") is None  # no token: anonymous, no releases call
    got = changes.fetch(tgt(changes={"source": "github", "repo": "acme/billing", "releases": True}), START, END)
    assert got == [{"time": "2026-11-05T10:05:00+00:00", "kind": "deploy", "description": "billing@1.4.2 released",
                    "link": "https://github.com/acme/billing/releases/tag/1.4.2", "source": "github"}]


def test_github_enterprise_host_and_bad_config(monkeypatch):
    calls = fake_http(monkeypatch, {"https://ghe.example.com/api/v3/repos/acme/billing/deployments?": [
        {"id": 9, "sha": "abcdef0123", "ref": "r1", "created_at": "2026-11-05T10:00:00Z"}],
        "https://ghe.example.com/api/v3/repos/acme/billing/deployments/9/statuses": []})
    got = changes.fetch(tgt(changes={"source": "github", "repo": "acme/billing", "base_url": "https://ghe.example.com/api/v3"}), START, END)
    assert got[0]["link"] == "https://ghe.example.com/acme/billing/commit/abcdef0123" and got[0]["description"] == "billing@r1"
    got = changes.fetch(tgt(changes={"source": "github", "repo": "billing"}), START, END)
    assert got[0]["kind"] == "other" and "owner/name" in got[0]["description"]
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    got = changes.fetch(tgt(changes={"source": "github", "repo": "acme/billing", "token_env": "NOPE_TOKEN"}), START, END)
    assert got[0]["description"] == "github: environment variable NOPE_TOKEN is not set" and len(calls) == 2  # nothing was fetched


# --------------------------------------------------------------------------- gitlab
def test_gitlab_deployments(monkeypatch):
    monkeypatch.setenv("GL_TOKEN", "glpat")
    calls = fake_http(monkeypatch, {"https://gitlab.com/api/v4/projects/42/deployments?": [], "https://gitlab.example.com/api/v4/projects/acme%2Fbilling/deployments?": [
        {"id": 1, "ref": "1.4.2", "sha": "abc", "status": "success", "updated_at": "2026-11-05T10:12:00Z",
         "environment": {"name": "production", "external_url": "https://billing.example.com"},
         "deployable": {"finished_at": "2026-11-05T10:14:00Z", "web_url": "https://gitlab.example.com/acme/billing/-/jobs/77", "pipeline": {"web_url": "p"}}},
        {"id": 2, "ref": "1.4.3", "sha": "def", "status": "failed", "updated_at": "2026-11-05T10:16:00Z", "environment": {"name": "production"}, "deployable": None},
        {"id": 3, "ref": "1.4.4", "sha": "fed", "status": "success", "updated_at": "2026-11-05T10:30:00Z", "environment": {"name": "production"}},  # after the end
    ]})
    t = tgt(changes={"source": "gitlab", "project": "acme/billing", "environment": "production", "token_env": "GL_TOKEN",
                     "base_url": "https://gitlab.example.com/"})
    got = changes.fetch(t, END - timedelta(hours=2), END)
    assert got == [
        {"time": "2026-11-05T10:14:00+00:00", "kind": "deploy", "description": "billing@1.4.2 to production",
         "link": "https://gitlab.example.com/acme/billing/-/jobs/77", "source": "gitlab"},
        {"time": "2026-11-05T10:16:00+00:00", "kind": "deploy", "description": "billing@1.4.3 to production (failed)", "link": None, "source": "gitlab"},
    ]
    req = calls[0]
    assert req.get_header("Private-token") == "glpat"
    assert "updated_after=2026-11-05T08%3A20%3A00%2B00%3A00" in req.full_url and "environment=production" in req.full_url
    got = changes.fetch(tgt(changes={"source": "gitlab", "project": "42"}), START, END)  # gitlab.com, numeric id, anonymous
    assert calls[-1].full_url.startswith("https://gitlab.com/api/v4/projects/42/deployments?") and got == []


def test_http_failure_is_one_item_and_a_stderr_line(monkeypatch, capsys):
    fake_http(monkeypatch, {"https://gitlab.com/": urllib.error.URLError("name resolution failed")})
    got = changes.fetch(tgt(changes={"source": "gitlab", "project": "acme/billing"}), START, END)
    assert got == [{"time": END.isoformat(), "kind": "other", "description": "gitlab: URLError: <urlopen error name resolution failed>",
                    "link": None, "source": "gitlab", "error": True}]
    assert "changes feed gitlab for billing-db: URLError" in capsys.readouterr().err
    fake_http(monkeypatch, {"https://api.github.com/": urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)})
    got = changes.fetch(tgt(changes={"source": "github", "repo": "acme/billing"}), START, END)
    assert got[0]["description"] == "github: HTTPError: HTTP Error 401: Unauthorized"


# --------------------------------------------------------------------------- capture
def test_collect_stores_the_feed_with_the_snapshot(monkeypatch, tmp_path):
    from dbtriage import cli
    from dbtriage.collectors.base import Snapshot
    from dbtriage.config import Config
    from dbtriage.storage import LocalStorage
    from dbtriage.triggers import TriageRequest

    class Collector:
        def collect(self, window):
            return Snapshot(summary={"captured_at": END.isoformat(), "metrics_kind": "interval"}, raw={}, reports={})

    monkeypatch.setattr(cli, "get_collector", lambda t: Collector())
    seen = {}

    def fetch(target, start, end, base_dir=None, store=None):
        seen.update(start=start, end=end, base_dir=base_dir)
        assert store is st  # the inbox is read from the store the capture writes to
        if not changes.spec_of(target):
            return []  # what the real fetch answers with no source and an empty inbox
        return [{"time": "2026-11-05T10:14:00+00:00", "kind": "deploy", "description": "billing@1.4.2 to production", "link": None, "source": "github"}]

    monkeypatch.setattr(changes, "fetch", fetch)
    st = LocalStorage(tmp_path / "s")
    t = tgt(changes={"source": "github", "repo": "acme/billing", "lookback_hours": 6, "correlate_minutes": 10})
    cfg = Config(storage_url="", targets={"billing-db": t}, base_dir=str(tmp_path))
    req = TriageRequest(target="billing-db", mode="scheduled", window_minutes=60, end=END, source="test")
    sid = cli.collect(cfg, st, req)
    m = st.get_json(f"{sid}/manifest.json")
    assert m["changes_sources"] == ["github"] and m["correlate_minutes"] == 10 and "changes.json" in m["files"]
    assert st.get_json(f"{sid}/changes.json")[0]["description"] == "billing@1.4.2 to production"
    assert seen == {"start": END - timedelta(hours=7), "end": END, "base_dir": str(tmp_path)}  # the window plus the lookback
    assert cli.stored_changes(st, sid) == (st.get_json(f"{sid}/changes.json"), 10)

    plain = cli.collect(Config(storage_url="", targets={"billing-db": tgt()}, base_dir=str(tmp_path)), st, req)  # no feed: no file
    m = st.get_json(f"{plain}/manifest.json")
    assert m["changes_sources"] == [] and "correlate_minutes" not in m and not st.exists(f"{plain}/changes.json")
    assert cli.stored_changes(st, plain) == (None, 30)

    def boom(*a, **k):
        raise RuntimeError("feed exploded")

    monkeypatch.setattr(changes, "fetch", boom)  # fetch never raises, but even if it did the capture must land
    sid = cli.collect(cfg, st, req)
    assert st.exists(f"{sid}/manifest.json") and not st.exists(f"{sid}/changes.json")
