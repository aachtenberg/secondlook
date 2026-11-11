"""Postgres store and settings, and how the app behaves when a remote store is down.

The live tests run only when DBTRIAGE_TEST_PG_URL names a database (for the lab container, the URL from
docker-compose.yml); they work in a throwaway schema and skip otherwise."""
import json
import os
import uuid

import pytest

from dbtriage.settings import Settings, SettingsUnavailable
from dbtriage.storage import LocalStorage, PostgresStorage, ResilientStorage, StorageUnavailable, describe, is_unavailable, open_storage

PG = os.environ.get("DBTRIAGE_TEST_PG_URL", "")


class Flaky(LocalStorage):
    """A remote store that can be switched off."""
    up = True
    where = "fake://store"

    def _check(self):
        if not self.up:
            raise ConnectionError("store is down")

    def put(self, key, data):
        self._check(); super().put(key, data)

    def get(self, key):
        self._check(); return super().get(key)

    def exists(self, key):
        self._check(); return super().exists(key)

    def list_keys(self, prefix):
        self._check(); return super().list_keys(prefix)

    def version_of(self, key):
        self._check(); return super().version_of(key)

    def put_if(self, key, data, expected):
        self._check(); super().put_if(key, data, expected)


def resilient(tmp_path):
    ResilientStorage.RETRY_S = 0.0
    remote = Flaky(tmp_path / "remote")
    return remote, ResilientStorage(remote, LocalStorage(tmp_path / "spool"))


def test_outage_spools_writes_and_syncs_back(tmp_path):
    remote, st = resilient(tmp_path)
    st.put_json("pg/a/1/manifest.json", {"n": 1})
    assert remote.exists("pg/a/1/manifest.json") and st.spooled() == [] and st.degraded is None
    remote.up = False
    st.put_json("pg/a/2/summary.json", {"n": 2})
    st.put_json("pg/a/2/manifest.json", {"n": 2})
    assert st.spooled() == ["pg/a/2/manifest.json", "pg/a/2/summary.json"] and "store is down" in st.degraded
    # reads: the spool answers, the store's own keys raise a clear StorageUnavailable, listings degrade to the spool
    assert st.get_json("pg/a/2/summary.json") == {"n": 2} and st.exists("pg/a/2/manifest.json") and not st.exists("pg/a/1/manifest.json")
    with pytest.raises(StorageUnavailable):
        st.get("pg/a/1/manifest.json")
    assert st.list_snapshots("pg", "a") == ["pg/a/2"]
    assert st.flush() == (0, 2)  # still down: nothing lost, nothing raised
    remote.up = True
    assert st.flush() == (2, 0) and st.spooled() == [] and not any((tmp_path / "spool").iterdir())
    assert remote.get_json("pg/a/2/manifest.json") == {"n": 2} and st.list_snapshots("pg", "a") == ["pg/a/1", "pg/a/2"] and st.degraded is None


def test_a_successful_write_flushes_and_newer_wins(tmp_path):
    remote, st = resilient(tmp_path)
    remote.up = False
    st.put_text("k/old.txt", "spooled")
    st.put_text("k/same.txt", "old")
    remote.up = True
    st.put_text("k/same.txt", "new")  # direct write succeeds, the stale spooled copy is dropped, the rest is flushed
    assert remote.get("k/same.txt") == b"new" and remote.get("k/old.txt") == b"spooled" and st.spooled() == []


def test_is_unavailable_and_open_storage(tmp_path, monkeypatch):
    class OperationalError(Exception): pass
    class EndpointConnectionError(Exception): pass
    assert is_unavailable(OperationalError()) and is_unavailable(EndpointConnectionError()) and is_unavailable(TimeoutError())
    assert not is_unavailable(ValueError()) and not is_unavailable(FileNotFoundError())
    assert isinstance(open_storage(f"file://{tmp_path}"), LocalStorage)
    st = open_storage("postgres://u:pw@db.example:5432/x?schema=dbt", tmp_path / "sp")
    assert isinstance(st, ResilientStorage) and isinstance(st.primary, PostgresStorage) and describe(st) == "postgres://u:***@db.example:5432/x?schema=dbt"
    monkeypatch.setenv("DBTRIAGE_NO_SPOOL", "1")
    assert isinstance(open_storage("postgresql://h/x"), PostgresStorage)


def test_unreachable_postgres_degrades_instead_of_failing(tmp_path):
    ResilientStorage.RETRY_S = 0.0
    st = open_storage("postgres://u:p@127.0.0.1:1/x?connect_timeout=1", tmp_path / "spool")  # nothing listens on port 1
    st.put_json("pg/a/1/manifest.json", {"n": 1})
    assert st.spooled() == ["pg/a/1/manifest.json"] and "postgres store postgres://u:***@127.0.0.1:1" in st.degraded and "p@" not in st.degraded
    assert st.list_snapshots("pg", "a") == ["pg/a/1"]
    s = Settings("postgres://u:p@127.0.0.1:1/x?connect_timeout=1", cache=tmp_path / "cache.json")
    assert s.degraded and "last good copy" in s.degraded and s.data["targets"] == {}
    (tmp_path / "cache.json").write_text(json.dumps({"targets": {"t": {"engine": "postgres", "dsn_env": "X"}}}))
    s.load()
    assert s.data["targets"] == {"t": {"engine": "postgres", "dsn_env": "X"}}  # runs on the last good copy
    with pytest.raises(SettingsUnavailable):
        s.put_web({"brand": "B"})
    assert s.data["web"] == {}  # the unsaved change is not kept in memory either


def test_web_shows_banner_and_503_while_the_store_is_down(tmp_path):
    from dbtriage.config import Config, Target
    from dbtriage.web import App

    remote, st = resilient(tmp_path)
    sid = "postgres/demo/2026-11-05/20261105T100000Z"
    st.put_json(f"{sid}/manifest.json", {"id": sid, "target": "demo", "engine": "postgres", "captured_at": "2026-11-05T10:00:00+00:00",
                                         "window": {"mode": "adhoc"}, "trigger": {"source": "cli"}, "reports": []})
    app = App(Config(storage_url="x", targets={"demo": Target("demo", "postgres", "d")}, base_dir=str(tmp_path)), st, False, None)
    assert "class='notice bad degraded'" not in app.index().decode() and app.status()["store_ok"]
    remote.up = False
    page = app.index().decode()  # the overview still renders, from what is reachable
    assert "class='notice bad degraded'" in page and "fake://store is unreachable" in page and app.status()["store_ok"] is False
    with pytest.raises(StorageUnavailable):  # the handler turns this into a 503 page
        app.snapshot(sid)


@pytest.fixture
def pg_url():
    import psycopg

    if not PG:
        pytest.skip("set DBTRIAGE_TEST_PG_URL to run the live Postgres tests")
    try:
        psycopg.connect(PG.replace("postgres://", "postgresql://", 1), connect_timeout=2).close()
    except Exception:  # noqa: BLE001
        pytest.skip(f"no Postgres at {PG}")
    schema = "dbt_test_" + uuid.uuid4().hex[:8]
    yield f"{PG}{'&' if '?' in PG else '?'}schema={schema}"
    with psycopg.connect(PG.replace("postgres://", "postgresql://", 1), autocommit=True) as c:
        c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_postgres_store_live(pg_url):
    st = PostgresStorage(pg_url)
    st.put_json("oracle/ora/2026-11-05/T1/summary.json", {"load_profile": {"x": 1.5}, "top_sql": [{"sql_id": "a_b%c"}]})
    st.put_json("oracle/ora/2026-11-05/T1/manifest.json", {"id": "T1"})
    st.put_text("oracle/ora/2026-11-05/T1/awr.txt", "WORKLOAD REPOSITORY\x00binary-ish")
    st.put_json("oracle/ora_x/2026-11-05/T9/manifest.json", {"id": "T9"})  # '_' in a prefix must not act as a wildcard
    assert st.get_json("oracle/ora/2026-11-05/T1/summary.json") == {"load_profile": {"x": 1.5}, "top_sql": [{"sql_id": "a_b%c"}]}
    assert st.get("oracle/ora/2026-11-05/T1/awr.txt") == b"WORKLOAD REPOSITORY\x00binary-ish"
    assert st.list_keys("oracle/ora/") == ["oracle/ora/2026-11-05/T1/awr.txt", "oracle/ora/2026-11-05/T1/manifest.json", "oracle/ora/2026-11-05/T1/summary.json"]
    assert st.list_snapshots("oracle", "ora") == ["oracle/ora/2026-11-05/T1"] and st.exists("oracle/ora/2026-11-05/T1/manifest.json")
    assert not st.exists("nope") and st.latest("oracle", "ora") == "oracle/ora/2026-11-05/T1"
    with pytest.raises(FileNotFoundError):
        st.get("nope")
    st.put_json("oracle/ora/2026-11-05/T1/manifest.json", {"id": "T1", "v": 2})
    assert st.get_json("oracle/ora/2026-11-05/T1/manifest.json") == {"id": "T1", "v": 2}
    # JSON lands in jsonb, so it can be queried directly
    row = st.client.run("SELECT doc->'load_profile'->>'x' FROM dbtriage_objects WHERE key = %s", ("oracle/ora/2026-11-05/T1/summary.json",), "one")
    assert row == ("1.5",)


def test_postgres_settings_live(pg_url, tmp_path):
    s = Settings(pg_url, cache=tmp_path / "c.json")
    assert s.degraded is None and s.data["targets"] == {}
    s.put_web({"brand": "Acme"})
    assert Settings(pg_url, cache=tmp_path / "other.json").data["web"] == {"brand": "Acme"}  # another host sees it
    assert json.loads((tmp_path / "c.json").read_text())["web"] == {"brand": "Acme"} and oct((tmp_path / "c.json").stat().st_mode)[-3:] == "600"


def test_copy_store_and_settings_cli(pg_url, tmp_path, monkeypatch):
    from dbtriage.cli import main

    src = LocalStorage(tmp_path / "data")
    src.put_json("pg/a/2026-11-05/T1/manifest.json", {"id": 1})
    src.put_text("pg/a/2026-11-05/T1/triage.md", "# review")
    (tmp_path / "targets.toml").write_text(f'[storage]\nurl = "file://{tmp_path}/data"\n')
    (tmp_path / "s.json").write_text(json.dumps({"web": {"brand": "X"}, "targets": {}}))
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    main(["--config", str(tmp_path / "targets.toml"), "copy-store", "--from", f"file://{tmp_path}/data", "--to", pg_url])
    main(["--config", str(tmp_path / "targets.toml"), "copy-store", "--from", f"file://{tmp_path}/data", "--to", pg_url])  # idempotent
    dst = PostgresStorage(pg_url)
    assert dst.get("pg/a/2026-11-05/T1/triage.md") == b"# review" and dst.list_snapshots("pg", "a") == ["pg/a/2026-11-05/T1"]
    main(["--config", str(tmp_path / "targets.toml"), "copy-settings", "--from", str(tmp_path / "s.json"), "--to", pg_url])
    assert Settings(pg_url, cache=tmp_path / "c2.json").data["web"] == {"brand": "X"}


def test_pg_call_deadline(monkeypatch):
    import threading
    import time

    from dbtriage.pgstore import PgClient

    c = PgClient("postgres://u:p@h/x", [])
    c.DEADLINE_S = 0.3
    c.conn = type("Hung", (), {"closed": False, "execute": lambda self, *a: time.sleep(5)})()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError) as e:
        c.run("select 1", (), "one")
    assert time.monotonic() - t0 < 1.5 and "no answer within" in str(e.value) and "p@" not in str(e.value) and c.conn is None
    assert is_unavailable(e.value)


def test_health_probe_recovers_without_page_views(tmp_path):
    remote, st = resilient(tmp_path)
    remote.up = False
    st.put_text("a/b.txt", "x")
    assert st.degraded and st.spooled() == ["a/b.txt"]
    remote.up = True
    assert st.probe() is True and st.degraded is None and st.spooled() == [] and remote.get("a/b.txt") == b"x"
