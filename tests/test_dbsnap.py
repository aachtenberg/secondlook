"""Snapshots on the database: off unless targets.toml allows writes and the target asks; plugins; never loses a run."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from dbtriage import dbsnap
from dbtriage.collectors.base import Window
from dbtriage.config import Target

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
ON = {"allow_db_writes": True}


def win(mode="scheduled"):
    return Window(start=NOW - timedelta(hours=1), end=NOW, mode=mode)


def tgt(engine="oracle", **extra):
    return Target("t", engine, "dsn", diagnostics_pack=True, extra=extra)


def test_modes_and_the_master_switch():
    assert [dbsnap.mode_of(v) for v in (None, False, "", "off", True, "incident", "always")] == \
        ["off", "off", "off", "off", "incident", "incident", "always"]
    with pytest.raises(ValueError):
        dbsnap.mode_of("sometimes")
    assert dbsnap.writes_allowed(ON) and not dbsnap.writes_allowed({}) and not dbsnap.writes_allowed(None)
    assert not dbsnap.writes_allowed({"allow_db_writes": "true"}) and not dbsnap.writes_allowed({"allow_db_writes": 1})


def test_take_runs_only_when_both_switches_are_on(monkeypatch):
    calls = []
    monkeypatch.setitem(dbsnap.BUILTIN, "oracle", lambda t, w: calls.append(w.mode) or "AWR snapshot 7")
    assert dbsnap.take(ON, tgt(), win()) is None  # the target does not ask
    assert dbsnap.take(ON, tgt(create_snapshot="incident"), win("scheduled")) is None  # not an incident run
    off = dbsnap.take({}, tgt(create_snapshot="always"), win())
    assert off["status"] == "skipped" and "allow_db_writes is off" in off["reason"] and calls == []  # nothing written
    assert dbsnap.take(ON, tgt(create_snapshot=True), win("incident")) == {"status": "taken", "detail": "AWR snapshot 7"}
    assert dbsnap.take(ON, tgt(create_snapshot="always"), win("adhoc"))["status"] == "taken" and calls == ["incident", "adhoc"]
    assert dbsnap.take(ON, tgt(create_snapshot="bogus"), win())["status"] == "skipped"


def test_take_reports_and_never_raises(monkeypatch):
    monkeypatch.setitem(dbsnap.BUILTIN, "oracle", lambda t, w: None)
    assert dbsnap.take(ON, tgt(create_snapshot="always"), win())["status"] == "not needed"

    def boom(t, w):
        raise RuntimeError("ORA-20200: no privilege")

    monkeypatch.setitem(dbsnap.BUILTIN, "oracle", boom)
    failed = dbsnap.take(ON, tgt(create_snapshot="always"), win())
    assert failed == {"status": "failed", "reason": "RuntimeError: ORA-20200: no privilege"}
    none = dbsnap.take(ON, tgt("postgres", create_snapshot="always"), win())
    assert none["status"] == "skipped" and "dbtriage.db_snapshot" in none["reason"]


def test_a_plugin_adds_an_engine_and_loads_only_when_used(monkeypatch):
    loaded = []

    class EP:
        name, value = "postgres", "my_pkg.snap:take"

        def load(self):
            loaded.append(1)
            return lambda t, w: "pg_profile sample 12"

    monkeypatch.setattr(dbsnap, "entry_points", lambda group: [EP()] if group == dbsnap.GROUP else [])
    found = dbsnap.takers()
    assert set(found) >= {"oracle", "postgres"} and loaded == []  # listed, not imported
    assert dbsnap.take(ON, tgt("postgres", create_snapshot="always"), win()) == {"status": "taken", "detail": "pg_profile sample 12"}
    assert loaded == [1]


def test_collect_takes_it_first_and_records_it(tmp_path, monkeypatch):
    from dbtriage import cli
    from dbtriage.collectors.base import Snapshot
    from dbtriage.config import Config
    from dbtriage.storage import LocalStorage
    from dbtriage.triggers import TriageRequest

    order = []
    monkeypatch.setitem(dbsnap.BUILTIN, "oracle", lambda t, w: order.append("db snapshot") or "AWR snapshot 9")

    class Collector:
        def collect(self, window):
            order.append("collect")
            return Snapshot(summary={"captured_at": NOW.isoformat(), "metrics_kind": "interval"}, raw={}, reports={})

    monkeypatch.setattr(cli, "get_collector", lambda t: Collector())
    st = LocalStorage(tmp_path / "s")
    t = tgt(create_snapshot="always")
    for collect_cfg, want in ((ON, {"status": "taken", "detail": "AWR snapshot 9"}), ({}, "skipped")):
        cfg = Config(storage_url=f"file://{tmp_path}/s", targets={"t": t}, collect=collect_cfg, base_dir=str(tmp_path))
        sid = cli.collect(cfg, st, TriageRequest(target="t", mode="scheduled", source="test"))
        got = st.get_json(f"{sid}/manifest.json")["db_snapshot"]
        assert got == want if isinstance(want, dict) else got["status"] == want
    assert order == ["db snapshot", "collect", "collect"]  # before collecting; nothing taken with writes off
    t.extra["create_snapshot"] = "off"
    sid = cli.collect(Config(storage_url="", targets={"t": t}, collect=ON), st, TriageRequest(target="t", mode="scheduled", source="test"))
    assert "db_snapshot" not in st.get_json(f"{sid}/manifest.json")


def test_oracle_taker(monkeypatch):
    from dbtriage.collectors import oracle

    class Cur:
        def __init__(self, age):
            self.age, self.description, self.rows, self.called = age, [], [], []

        def execute(self, sql, binds=None):
            if "control_management_pack_access" in sql:
                self.description, self.rows = [("VALUE",)], [("DIAGNOSTIC+TUNING",)]
            elif "from v$database" in sql:
                self.description, self.rows = [("DBID",)], [(42,)]
            else:
                self.description, self.rows = [("AGE_MIN",)], [(self.age,)]

        def fetchall(self):
            return self.rows

        def callfunc(self, name, typ):
            self.called.append(name)
            return 1234

    class Conn:
        def __init__(self, cur):
            self.cur = cur

        def cursor(self):
            return self.cur

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    for age, want in ((60.0, "AWR snapshot 1234"), (None, "AWR snapshot 1234"), (2.0, None)):
        cur = Cur(age)
        monkeypatch.setattr(oracle, "connect", lambda t, cur=cur: Conn(cur))
        assert oracle.take_awr_snapshot(tgt(), win()) == want
        assert cur.called == ([] if want is None else ["dbms_workload_repository.create_snapshot"])
    with pytest.raises(RuntimeError, match="Diagnostics Pack"):
        oracle.take_awr_snapshot(Target("t", "oracle", "dsn"), win())


def test_settings_store_the_mode_and_cannot_allow_writes(tmp_path):
    from tests.test_overlay_semantics import app_for

    conf = tmp_path / "targets.toml"
    conf.write_text('[targets.ora]\nengine = "oracle"\ndsn = "u/p@h:1521/S"\ndiagnostics_pack = true\ncreate_snapshot = true\n')
    app = app_for(conf)
    assert app.save_target("ora", {"create_snapshot": "incident"}) == []  # what true means: not stored
    stored = lambda: json.loads((tmp_path / "dbtriage-settings.json").read_text())["targets"]["ora"]  # noqa: E731
    assert "create_snapshot" not in stored()
    assert app.save_target("ora", {"create_snapshot": "always"}) == [] and stored()["create_snapshot"] == "always"
    assert app.save_target("ora", {"create_snapshot": "off"}) == [] and stored()["create_snapshot"] is False  # beats the file's true
    assert app.save_target("ora", {"create_snapshot": "sometimes"}) == ["create_snapshot: one of off, incident, always"]
    # an overlay never carries the master switch, even hand-edited
    data = json.loads((tmp_path / "dbtriage-settings.json").read_text())
    (tmp_path / "dbtriage-settings.json").write_text(json.dumps({**data, "collect": {"allow_db_writes": True}}))
    assert app_for(conf).cfg.collect == {} and not app_for(conf).api_settings()["allow_db_writes"]


def test_pages_show_it(tmp_path):
    from tests.test_overlay_semantics import app_for

    conf = tmp_path / "targets.toml"
    conf.write_text('[targets.ora]\nengine = "oracle"\ndsn = "u/p@h:1521/S"\ncreate_snapshot = "always"\n')
    app = app_for(conf)
    form = app.target_form("ora").decode()
    assert "<option value='always' selected>Every run</option>" in form and "No effect while targets.toml has dbtriage only read" in form
    assert "dbtriage only reads your databases" in app.settings_page().decode()
    conf.write_text("[collect]\nallow_db_writes = true\n" + conf.read_text())
    app = app_for(conf)
    assert "targets.toml allows it" in app.target_form("ora").decode() and "Database writes are on" in app.settings_page().decode()
    from dbtriage.web import _db_snapshot_note

    assert _db_snapshot_note({"status": "taken", "detail": "AWR snapshot 5"}) == " · took AWR snapshot 5 on the database"
    assert "no database snapshot (skipped)" in _db_snapshot_note({"status": "skipped", "reason": "allow_db_writes is off <x>"})
    assert "&lt;x&gt;" in _db_snapshot_note({"status": "failed", "reason": "<x>"}) and _db_snapshot_note(None) == ""


def test_the_settings_api_refuses_the_master_switch(web_server):
    import urllib.error
    import urllib.request

    for body in ({"collect": {"allow_db_writes": True}}, {"allow_db_writes": True}):
        req = urllib.request.Request(f"{web_server}/api/settings", data=json.dumps(body).encode(), method="PUT",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 400 and "only be set in targets.toml" in exc.value.read().decode()


def test_the_window_ends_after_the_snapshot_it_took(tmp_path, monkeypatch):
    """The request's window ends when the request came in; a snapshot taken after that must still be in it."""
    import time
    from datetime import datetime, timezone

    from dbtriage import cli
    from dbtriage.collectors.base import Snapshot
    from dbtriage.config import Config
    from dbtriage.storage import LocalStorage
    from dbtriage.triggers import TriageRequest

    taken_at = []
    monkeypatch.setitem(dbsnap.BUILTIN, "oracle", lambda t, w: taken_at.append(datetime.now(timezone.utc)) or "AWR snapshot 9")
    seen = []

    class Collector:
        def collect(self, window):
            seen.append(window)
            return Snapshot(summary={"captured_at": NOW.isoformat(), "metrics_kind": "interval"}, raw={}, reports={})

    monkeypatch.setattr(cli, "get_collector", lambda t: Collector())
    st = LocalStorage(tmp_path / "s")
    req = TriageRequest(target="t", mode="scheduled", source="test")
    time.sleep(0.01)
    for collect_cfg in (ON, {}):
        cfg = Config(storage_url="", targets={"t": tgt(create_snapshot="always")}, collect=collect_cfg)
        sid = cli.collect(cfg, st, req)
    taken, read_only = seen
    assert taken.end >= taken_at[0] > req.end and taken.start == req.end - timedelta(minutes=60)
    assert read_only.end == req.end  # nothing taken: the window is the request's
    assert st.get_json(f"{sid}/manifest.json")["window"]["end"] == req.end.isoformat()


def test_a_payload_time_without_an_offset_is_utc_and_can_be_stretched(tmp_path, monkeypatch):
    """A webhook's end like "2026-11-06T10:00:00" made the window stretch compare naive and aware times and kill the run
    (agent review on #8, fa8676f)."""
    from datetime import datetime, timezone

    from dbtriage import cli
    from dbtriage.collectors.base import Snapshot
    from dbtriage.config import Config
    from dbtriage.storage import LocalStorage
    from dbtriage.triggers import TriageRequest, parse_payload

    req = parse_payload({"target": "t", "mode": "incident", "end": "2026-11-06T10:00:00"})
    assert req.end == datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    assert TriageRequest(target="t", end=datetime.fromisoformat("2026-11-06T05:00:00-05:00")).end == req.end
    monkeypatch.setitem(dbsnap.BUILTIN, "oracle", lambda t, w: "AWR snapshot 9")
    seen = []

    class Collector:
        def collect(self, window):
            seen.append(window)
            return Snapshot(summary={"captured_at": NOW.isoformat(), "metrics_kind": "interval"}, raw={}, reports={})

    monkeypatch.setattr(cli, "get_collector", lambda t: Collector())
    cfg = Config(storage_url="", targets={"t": tgt(create_snapshot="always")}, collect=ON)
    cli.collect(cfg, LocalStorage(tmp_path / "s"), req)
    assert seen[0].end > req.end and seen[0].end.tzinfo is not None


def test_the_cli_reads_an_end_without_an_offset_as_utc_too(monkeypatch):
    """--end 2026-11-06T10:00:00 was read as the server's local time while a webhook's same string was UTC: one rule
    for both now (agent review on #8, 0c3699d)."""
    import argparse
    from datetime import datetime, timezone

    from dbtriage import cli

    monkeypatch.setenv("TZ", "Asia/Kolkata")  # +05:30: a local reading would end the window at 04:30Z
    __import__("time").tzset()
    try:
        args = argparse.Namespace(target="t", mode="adhoc", window=60, end="2026-11-06T10:00:00", reason="", channels=None)
        assert cli._req(args).end == datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
        args.end = "2026-11-06T10:00:00+05:30"
        assert cli._req(args).end == datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)  # an offset is honoured
    finally:
        monkeypatch.undo()
        __import__("time").tzset()
