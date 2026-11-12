"""The webhook inbox: the /changes route on `dbtriage serve`, the compare-and-set append, pruning, and the feed
merging posted items with the configured source and into the stored snapshot."""
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dbtriage import changes, inbox
from dbtriage.config import Config, Target
from dbtriage.storage import Conflict, LocalStorage
from dbtriage.triggers import make_server
from tests.test_changes import fake_http

END = datetime(2026, 11, 5, 10, 20, tzinfo=timezone.utc)   # direct calls pass now=END; the route uses the clock
NOW = datetime.now(timezone.utc).replace(microsecond=0)
TOKEN = "s3cret"


def tgt(name="billing-db", **extra):
    return Target(name, "postgres", "dsn", extra=extra)


def iso(delta: timedelta) -> str:
    return (END + delta).isoformat()


@pytest.fixture
def receiver(tmp_path):
    """`dbtriage serve` on a free port, with a fake run so a POST / starts nothing real."""
    st = LocalStorage(tmp_path / "s")
    cfg = Config(storage_url="", targets={"billing-db": tgt(), "crm": tgt("crm", changes={"source": "github", "repo": "acme/crm"})})
    runs = []
    srv = make_server("127.0.0.1", 0, TOKEN, run=runs.append, cfg_loader=lambda: cfg, store=st)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{srv.server_port}", store=st, cfg=cfg, runs=runs)
    srv.shutdown()
    srv.server_close()


def call(url: str, body, token: str | None = TOKEN, path: str = "/changes", method: str = "POST"):
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --------------------------------------------------------------------------- the route
def test_route_records_one_item_and_a_list(receiver):
    code, body = call(receiver.url, {"target": "billing-db", "description": "billing@1.4.2 to production", "link": "https://ci/1"})
    assert code == 202 and body["accepted"] is True and len(body["recorded"]) == 1
    it = body["recorded"][0]
    assert it["target"] == "billing-db" and it["kind"] == "deploy" and it["source"] == "webhook" and it["link"] == "https://ci/1"
    assert datetime.now(timezone.utc) - datetime.fromisoformat(it["time"]) < timedelta(minutes=1)  # time defaults to now
    obj = receiver.store.get_json("postgres/billing-db/changes-inbox.json")
    assert obj["generation"] == 1 and [i["description"] for i in obj["items"]] == ["billing@1.4.2 to production"]

    hour_ago, two_ago = (NOW - timedelta(hours=1)), (NOW - timedelta(hours=2))  # near now: the route prunes against the clock
    code, body = call(receiver.url, [
        {"target": "crm", "time": hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": "release", "description": "crm@7 released"},  # an alias is mapped
        {"target": "billing-db", "time": two_ago.strftime("%Y-%m-%dT%H:%M:%S"), "kind": "parameter", "description": "work_mem 64MB"},  # naive: UTC
    ], path="/changes/")
    assert code == 202 and [(i["target"], i["kind"], i["time"]) for i in body["recorded"]] == [
        ("crm", "deploy", hour_ago.isoformat()), ("billing-db", "parameter", two_ago.isoformat())]
    assert [i["description"] for i in receiver.store.get_json("postgres/crm/changes-inbox.json")["items"]] == ["crm@7 released"]
    obj = receiver.store.get_json("postgres/billing-db/changes-inbox.json")
    assert obj["generation"] == 2 and [i["description"] for i in obj["items"]] == ["work_mem 64MB", "billing@1.4.2 to production"]  # oldest first


def test_route_rejects_bad_requests_and_writes_nothing(receiver):
    good = {"target": "billing-db", "description": "x"}
    assert call(receiver.url, good, token="wrong")[0] == 401
    assert call(receiver.url, good, token=None)[0] == 401
    code, body = call(receiver.url, {**good, "target": "nope"})
    assert code == 404 and body["error"].startswith("unknown target 'nope'; known: billing-db, crm")
    code, body = call(receiver.url, {**good, "kind": "incident"})
    assert code == 400 and body["error"] == "kind must be one of deploy, parameter, job, failover, other"
    code, body = call(receiver.url, {"target": "billing-db", "kind": "deploy"})
    assert code == 400 and body["error"].startswith("description is required")
    code, body = call(receiver.url, {**good, "time": "yesterday"})
    assert code == 400 and body["error"].startswith("time must be ISO 8601")
    assert call(receiver.url, {"description": "x"})[1]["error"].startswith("target is required")
    assert call(receiver.url, [])[1]["error"] == "no changes in the request"
    assert call(receiver.url, ["x"])[0] == 400
    assert call(receiver.url, [good, {**good, "target": "nope"}])[0] == 404  # a list is taken whole or not at all
    assert receiver.store.list_keys("") == []


def test_other_paths_still_start_runs_and_health_is_unchanged(receiver):
    code, body = call(receiver.url, {"target": "billing-db", "mode": "incident", "window_minutes": 30}, path="/")
    assert (code, body) == (202, {"accepted": True, "target": "billing-db", "mode": "incident", "source": "webhook"})
    for _ in range(100):
        if receiver.runs:
            break
        time.sleep(0.01)
    assert receiver.runs[0].target == "billing-db" and receiver.runs[0].window_minutes == 30
    assert call(receiver.url, {"hello": "world"}, path="/hooks/alertmanager")[0] == 400  # any other path is the run payload
    assert call(receiver.url, {"target": "billing-db"}, path="/changes", token="wrong")[0] == 401  # the token check comes first
    assert call(receiver.url, None, method="GET", path="/")[1] == {"ok": True}
    assert not receiver.store.exists("postgres/billing-db/changes-inbox.json")


def test_route_reports_a_contended_inbox(receiver, monkeypatch):
    def always_lose(key, data, expected):
        raise Conflict(key)

    monkeypatch.setattr(receiver.store, "put_if", always_lose)
    monkeypatch.setattr(inbox, "ATTEMPTS", 2)
    code, body = call(receiver.url, {"target": "billing-db", "description": "x"})
    assert code == 503 and "changed under 2 attempts" in body["error"] and body["recorded"] == []


# --------------------------------------------------------------------------- the append
def test_two_writers_posting_at_once_lose_nothing(tmp_path):
    st = LocalStorage(tmp_path)
    t = tgt()

    def post(prefix):
        for n in range(20):
            inbox.record(st, t, [parse(f"{prefix}{n}")], now=END)

    threads = [threading.Thread(target=post, args=(p,)) for p in "ab"]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    obj = st.get_json(changes.inbox_key("postgres", "billing-db"))
    assert len(obj["items"]) == 40 and obj["generation"] == 40
    assert sorted(i["description"] for i in obj["items"]) == sorted([f"a{n}" for n in range(20)] + [f"b{n}" for n in range(20)])


def test_a_lost_race_is_applied_again(tmp_path, capsys):
    st = LocalStorage(tmp_path)
    t = tgt()
    real = st.put_if
    state = {"stolen": False}

    def steal_once(key, data, expected):
        if not state["stolen"]:  # somebody else lands between our read and our save
            state["stolen"] = True
            inbox.record(st, t, [parse("theirs")], now=END)
        return real(key, data, expected)

    st.put_if = steal_once
    assert inbox.record(st, t, [parse("mine")], now=END) == [parse("mine")]
    assert sorted(i["description"] for i in changes.inbox(st, "postgres", "billing-db")) == ["mine", "theirs"]
    assert "changed under us" in capsys.readouterr().err
    with pytest.raises(inbox.InboxContended):
        st.put_if = lambda key, data, expected: (_ for _ in ()).throw(Conflict(key))
        inbox.record(st, t, [parse("never")], now=END, attempts=3)
    assert inbox.record(st, t, [], now=END) == []  # nothing to append: no write


def parse(description: str, **kw) -> dict:
    return inbox.parse_item({"target": "billing-db", "description": description, "time": kw.pop("time", END.isoformat()), **kw})[1]


def test_pruning_by_lookback_and_the_cap(tmp_path, monkeypatch):
    st = LocalStorage(tmp_path)
    t = tgt(changes={"source": "github", "repo": "acme/billing", "lookback_hours": 6})
    inbox.record(st, t, [parse("too old", time=iso(-timedelta(hours=30, minutes=1))),  # past lookback + a day
                         parse("kept", time=iso(-timedelta(hours=29, minutes=59)))], now=END)
    assert [i["description"] for i in changes.inbox(st, "postgres", "billing-db")] == ["kept"]
    plain = tgt()  # default lookback: 48 h, so three days ago goes on the post that brings it and two days ago stays
    inbox.record(st, plain, [parse("three days", time=iso(-timedelta(days=3, minutes=1))), parse("two days", time=iso(-timedelta(days=2)))], now=END)
    assert [i["description"] for i in changes.inbox(st, "postgres", "billing-db")] == ["two days", "kept"]
    inbox.record(st, plain, [parse("later", time=iso(timedelta(days=1)))], now=END + timedelta(days=1, minutes=1))  # a later post prunes what aged out
    assert [i["description"] for i in changes.inbox(st, "postgres", "billing-db")] == ["kept", "later"]

    monkeypatch.setattr(inbox, "MAX_ITEMS", 4)
    inbox.record(st, plain, [parse(f"n{n}", time=iso(timedelta(minutes=n))) for n in range(5)], now=END)
    assert [i["description"] for i in changes.inbox(st, "postgres", "billing-db")] == ["n2", "n3", "n4", "later"]  # the newest stay


def test_parse_item_shape():
    target, it = inbox.parse_item({"target": "d", "description": " x " * 200, "kind": " Job ", "link": 7}, now=END)
    assert target == "d" and it["kind"] == "job" and len(it["description"]) == 300 and it["link"] == "7" and it["time"] == END.isoformat()
    assert inbox.parse_item({"target": "d", "description": "x", "time": "2026-11-05T12:20:00+02:00"})[1]["time"] == END.isoformat()
    one = {"target": "d", "description": "x", "time": END.isoformat()}
    assert inbox.parse(one) == [inbox.parse_item(one)] and inbox.parse([one, one]) == [inbox.parse_item(one)] * 2
    for bad in ({"target": 3, "description": "x"}, {"target": "d", "description": "   "}, {"target": "d", "description": "x", "time": 5}):
        with pytest.raises(ValueError):
            inbox.parse_item(bad)


# --------------------------------------------------------------------------- the feed
def test_fetch_merges_the_inbox_with_github_and_with_no_source(tmp_path, monkeypatch):
    st = LocalStorage(tmp_path)
    t = tgt(changes={"source": "github", "repo": "acme/billing"})
    inbox.record(st, t, [parse("ansible: billing@1.4.2", time=iso(-timedelta(minutes=5))),
                         parse("outside the window", time=iso(-timedelta(hours=3)))], now=END)
    api = "https://api.github.com/repos/acme/billing"
    fake_http(monkeypatch, {f"{api}/deployments?": [{"id": 1, "sha": "a1a1a1a1a1", "ref": "v1.4.1", "created_at": iso(-timedelta(minutes=20))}],
                            f"{api}/deployments/1/statuses": []})
    got = changes.fetch(t, END - timedelta(hours=2), END, store=st)
    assert [(i["source"], i["description"]) for i in got] == [("github", "billing@v1.4.1"), ("webhook", "ansible: billing@1.4.2")]
    assert changes.fetch(t, END - timedelta(hours=2), END) == [got[0]]  # without a store: the configured source alone, as before
    assert changes.fetch(tgt(), END - timedelta(hours=2), END, store=st) == [got[1]]  # no source configured: the inbox alone
    assert changes.fetch(tgt("other"), END - timedelta(hours=2), END, store=st) == []  # nothing was ever posted for this one

    st.put("postgres/billing-db/changes-inbox.json", b"not json")  # an unreadable inbox is one item, and github still counts
    got = changes.fetch(t, END - timedelta(hours=2), END, store=st)
    assert [i["source"] for i in got] == ["github", "webhook"] and got[1]["error"] and got[1]["description"].startswith("webhook: Expecting value")


def test_collect_stores_posted_items_with_the_snapshot(tmp_path, monkeypatch):
    from dbtriage import cli
    from dbtriage.collectors.base import Snapshot
    from dbtriage.triggers import TriageRequest

    class Collector:
        def collect(self, window):
            return Snapshot(summary={"captured_at": END.isoformat(), "metrics_kind": "interval"}, raw={}, reports={})

    monkeypatch.setattr(cli, "get_collector", lambda t: Collector())
    st = LocalStorage(tmp_path / "s")
    cfg = Config(storage_url="", targets={"billing-db": tgt()}, base_dir=str(tmp_path))
    req = TriageRequest(target="billing-db", mode="scheduled", window_minutes=60, end=END, source="test")
    inbox.record(st, cfg.targets["billing-db"], [parse("billing@1.4.2 to production", time=iso(-timedelta(minutes=10)))], now=END)
    sid = cli.collect(cfg, st, req)
    m = st.get_json(f"{sid}/manifest.json")
    assert m["changes_sources"] == ["webhook"] and m["correlate_minutes"] == 30 and "changes.json" in m["files"]
    assert st.get_json(f"{sid}/changes.json") == [{"time": iso(-timedelta(minutes=10)), "kind": "deploy", "description": "billing@1.4.2 to production",
                                                   "link": None, "source": "webhook"}]
    assert cli.stored_changes(st, sid) == (st.get_json(f"{sid}/changes.json"), 30)

    fake_http(monkeypatch, {"https://api.github.com/": []})
    cfg = Config(storage_url="", targets={"billing-db": tgt(changes={"source": "github", "repo": "acme/billing"})}, base_dir=str(tmp_path))
    m = st.get_json(f"{cli.collect(cfg, st, req)}/manifest.json")
    assert m["changes_sources"] == ["github", "webhook"]


def test_an_inbox_is_merged_not_replaced_across_an_outage(tmp_path):
    """The store treats the inbox as append-only: a write from a stale copy, a second append spooled in the same
    outage, and the replay are each merged with what is already there."""
    from dbtriage.storage import LocalStorage, ResilientStorage

    class Flaky(LocalStorage):
        down = False

        def _check(self):
            if self.down:
                from dbtriage.storage import StorageUnavailable
                raise StorageUnavailable("down")

        def put(self, key, data):
            self._check(); super().put(key, data)

        def put_if(self, key, data, expected):
            self._check(); super().put_if(key, data, expected)

        def get(self, key):
            self._check(); return super().get(key)

        def version_of(self, key):
            self._check(); return super().version_of(key)

        def exists(self, key):
            self._check(); return super().exists(key)

    primary = Flaky(tmp_path / "primary")
    st = ResilientStorage(primary, LocalStorage(tmp_path / "spool"))
    st.RETRY_S = 0
    k = "postgres/demo/changes-inbox.json"
    item = lambda n: {"time": f"2026-11-11T0{n}:00:00+00:00", "kind": "deploy", "description": f"d{n}", "link": None, "source": "webhook"}  # noqa: E731
    st.put_json_if(k, {"items": [item(1)], "generation": 1}, None)
    assert [i["description"] for i in primary.get_json(k)["items"]] == ["d1"]
    primary.down = True
    st.put_json_if(k, {"items": [item(2)], "generation": 1}, None)  # appended from a copy that lacks d1 (spool had nothing)
    st.put_json_if(k, {"items": [item(3)], "generation": 1}, None)  # a second append in the same outage
    assert sorted(i["description"] for i in st.spool.get_json(k)["items"]) == ["d2", "d3"]  # both kept in the spool
    primary.down = False
    assert st.flush()[0] == 1
    assert [i["description"] for i in primary.get_json(k)["items"]] == ["d1", "d2", "d3"]  # replay merged, nothing lost
    st.put_json_if(k, {"items": [item(4)], "generation": 2}, st.version_of(k))  # a write from a stale copy after recovery
    assert [i["description"] for i in primary.get_json(k)["items"]] == ["d1", "d2", "d3", "d4"]


def test_reading_a_half_written_inbox_is_retried(tmp_path, monkeypatch):
    from dbtriage import changes
    from dbtriage.storage import LocalStorage

    st = LocalStorage(tmp_path)
    k = changes.inbox_key("postgres", "demo")
    st.put_json(k, {"items": [{"time": "2026-11-11T09:00:00+00:00", "kind": "deploy", "description": "d1", "link": None}], "generation": 1})
    real, calls = st.get_json, {"n": 0}

    def half_written_once(key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return real(key)

    monkeypatch.setattr(st, "get_json", half_written_once)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert [i["description"] for i in changes.inbox(st, "postgres", "demo")] == ["d1"] and calls["n"] == 2
