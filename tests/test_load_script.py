"""scripts/load.py, the lab load generator: --regress has to plant its slow query on every engine."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("lab_load", Path(__file__).resolve().parents[1] / "scripts" / "load.py")
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


def test_the_planted_query_survives_percent_style_drivers():
    """LIKE '%7a%' reached psycopg and pymysql as a placeholder ('%7'), so every regress thread died at once and the
    script still printed "load done": no slow query was ever planted on Postgres or MySQL/MariaDB."""
    q = load.REGRESS[0][0]
    for engine in ("postgres", "mysql"):
        sql = load.prepare(engine, q)
        assert sql % {"a": 7} == "select count(*) from orders where note like '%7a%' and amount > 7"  # how both drivers fill it
    assert load.prepare("oracle", q) == "select count(*) from orders where note like '%7a%' and amount > :a"


def test_a_dead_load_thread_fails_the_run(monkeypatch):
    class Cur:
        def execute(self, *a):
            raise RuntimeError("boom")

    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(load, "connect", lambda engine: (Conn(), Cur()))
    monkeypatch.setattr(load, "ERRORS", [])
    load.worker("postgres", load.time.time() + 5, [("select 1", 5)])
    assert load.ERRORS == ["RuntimeError: boom"]


def test_an_unreachable_database_fails_the_run(monkeypatch):
    """connect() ran before the try, so a load against a stopped lab still said "load done" (agent review on #19)."""
    def refused(engine):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(load, "connect", refused)
    monkeypatch.setattr(load, "ERRORS", [])
    load.worker("mysql", load.time.time() + 5, [("select 1", 5)])
    assert load.ERRORS == ["ConnectionRefusedError: connection refused"]
