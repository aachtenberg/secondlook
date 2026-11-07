"""The SQL Server collector without a server: DSN forms, masking, wait classes, and one snapshot normalized from
canned DMV rows the way the lab's SQL Server 2022 returns them."""
from datetime import datetime

import pytest

pytest.importorskip("mssql_python")  # the sqlserver extra

from dbtriage.config import Target  # noqa: E402
from dbtriage.collectors import sqlserver as ss  # noqa: E402
from dbtriage.collectors.base import Window
from dbtriage.settings import mask_dsn


def t(dsn, **extra):
    return Target("mssql", "sqlserver", dsn, extra=extra)


def test_connection_strings():
    cs = ss.conn_string(t("mssql://dbtriage:p%40ss%7D;x@db.corp:1444/app?encrypt=yes&trust_server_certificate=yes"))
    assert cs == "Server=db.corp,1444;Database=app;UID=dbtriage;PWD={p@ss}};x};Encrypt=yes;TrustServerCertificate=yes"
    assert "Encrypt=yes;TrustServerCertificate=no" in ss.conn_string(t("sqlserver://u:p@h/db"))  # verify the cert by default
    assert ss.conn_string(t("mssql://h/db?authentication=ActiveDirectoryMsi")).endswith("Authentication=ActiveDirectoryMsi")
    raw = "Server=tcp:x.database.windows.net,1433;Database=app;Authentication=ActiveDirectoryMsi;Encrypt=yes"
    assert ss.conn_string(t(raw)) == raw  # a connection string is used as is
    with pytest.raises(SystemExit, match="mssql://"):
        ss.conn_string(t("db.corp:1433"))


def test_connection_strings_are_masked():
    assert mask_dsn("mssql://dbtriage:secret@h:1433/app") == "mssql://dbtriage:***@h:1433/app"
    assert mask_dsn("Server=h,1433;UID=u;PWD={se;cr}}et};Encrypt=yes") == "Server=h,1433;UID=u;PWD=***;Encrypt=yes"
    assert mask_dsn("Server=h;User Id=u;Password=secret;Database=app") == "Server=h;User Id=u;Password=***;Database=app"
    # an Oracle easy-connect password with '=' and ';' in it is still masked (agent review on #11, 5af68c3)
    assert mask_dsn("scott/ti=ger;x@h:1521/FREE") == "scott/***@h:1521/FREE"


def test_wait_classes():
    assert [ss.wait_class(w) for w in ("LCK_M_X", "PAGEIOLATCH_SH", "PAGELATCH_EX", "WRITELOG", "CXPACKET",
                                       "SOS_SCHEDULER_YIELD", "RESOURCE_SEMAPHORE", "ASYNC_NETWORK_IO", "WHATEVER")] == \
        ["Lock", "Buffer IO", "Buffer latch", "Log IO", "Parallelism", "CPU", "Memory", "Network", "Other"]


ROWS = {
    "from sys.dm_os_sys_info": [{"version": "Microsoft SQL Server 2022 (RTM-CU27-GDR) - 16.0.4305.5 (X64)\n\tJul 1 2026", "db": "app",
                                 "sqlserver_start_time": datetime(2026, 11, 6, 19, 2), "cpu_count": 12, "physical_memory_kb": 4194304}],
    "from sys.dm_exec_query_stats": [
        {"sql_id": "0xB858F14FAB5DAA6A", "plan_hash": "0x1111", "calls": 300, "total_time_ms": 450.5, "cpu_time_ms": 400.0,
         "logical_reads": 90000, "physical_reads": 12, "rows_": 300, "spills": 0, "since": datetime(2026, 11, 6, 19, 10),
         "text": "SELECT @n = COUNT(*) FROM dbo.orders WHERE note LIKE '%ab%' AND amount > 42"},
        {"sql_id": "0xB858F14FAB5DAA6A", "plan_hash": "0x2222", "calls": 10, "total_time_ms": 50.0, "cpu_time_ms": 40.0,
         "logical_reads": 900, "physical_reads": 0, "rows_": 10, "spills": 0, "since": datetime(2026, 11, 6, 19, 20),
         "text": "SELECT @n = COUNT(*) FROM dbo.orders WHERE note LIKE '%ab%' AND amount > 7"}],
    "from sys.dm_os_wait_stats": [
        {"wait_type": "SLEEP_TASK", "waiting_tasks_count": 9, "wait_time_ms": 99999, "signal_wait_time_ms": 1},
        {"wait_type": "LCK_M_U", "waiting_tasks_count": 3, "wait_time_ms": 1908, "signal_wait_time_ms": 2},
        {"wait_type": "AZURE_IMDS_VERSIONS", "waiting_tasks_count": 1, "wait_time_ms": 60059, "signal_wait_time_ms": 0}],
    "from sys.dm_os_performance_counters": [
        {"object_name": "SQLServer:SQL Statistics", "counter_name": "Batch Requests/sec", "instance_name": "", "cntr_value": 535, "cntr_type": 272696576},
        {"object_name": "SQLServer:Buffer Manager", "counter_name": "Page life expectancy", "instance_name": "", "cntr_value": 771, "cntr_type": 65792},
        {"object_name": "SQLServer:Buffer Node", "counter_name": "Page life expectancy", "instance_name": "000", "cntr_value": 900, "cntr_type": 65792},
        {"object_name": "SQLServer:Buffer Node", "counter_name": "Page life expectancy", "instance_name": "001", "cntr_value": 120, "cntr_type": 65792},
        {"object_name": "SQLServer:Buffer Manager", "counter_name": "Page lookups/sec", "instance_name": "", "cntr_value": 100000, "cntr_type": 272696576},
        {"object_name": "SQLServer:Buffer Manager", "counter_name": "Page reads/sec", "instance_name": "", "cntr_value": 1000, "cntr_type": 272696576},
        {"object_name": "SQLServer:Databases", "counter_name": "Transactions/sec", "instance_name": "app", "cntr_value": 1487, "cntr_type": 272696576}],
    "from sys.dm_exec_requests": [
        {"session_id": 61, "status": "suspended", "wait_type": "LCK_M_X", "wait_time": 12000, "blocking_session_id": 58,
         "duration_s": 12.0, "login_name": "app", "program_name": "orders-api", "text": "UPDATE dbo.orders SET status = 'paid' WHERE id = 7"},
        {"session_id": 62, "status": "running", "wait_type": None, "wait_time": 0, "blocking_session_id": 0,
         "duration_s": 0.2, "login_name": "app", "program_name": "orders-api", "text": "SELECT 1"}],
    "from sys.dm_exec_sessions where is_user_process": [{"n": 4}],
    "from sys.dm_db_index_usage_stats": [{"name": "dbo.orders", "scans": 600, "seeks": 300, "rows_": 100000,
                                          "stats_date": datetime(2026, 11, 6, 19, 3)}],
    "from sys.configurations": [{"name": "max degree of parallelism", "value_in_use": 0},
                                {"name": "cost threshold for parallelism", "value_in_use": 5}],
    "from sys.databases": [{"compatibility_level": 160, "recovery_model_desc": "FULL", "is_read_committed_snapshot_on": False,
                            "query_store": "READ_WRITE"}],
}


class FakeCursor:
    def __init__(self):
        self.sql = []

    def execute(self, sql, *params):
        self.sql.append(sql)
        key = next(k for k in ROWS if k in " ".join(sql.split()))
        self.rows = ROWS[key]
        self.description = [(c,) for c in self.rows[0]] if self.rows else []

    def fetchall(self):
        return [tuple(r.values()) for r in self.rows]


class FakeConn:
    def __init__(self):
        self.cur = FakeCursor()

    def cursor(self):
        return self.cur

    def close(self):
        pass


def test_one_snapshot_from_the_dmvs(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(ss, "connect", lambda target: conn)
    s = ss.SQLServerCollector(t("mssql://u:p@h/app")).collect(Window(start=datetime(2026, 11, 6, 18), end=datetime(2026, 11, 6, 19))).summary
    assert all(q.startswith(ss.TAG) for q in conn.cur.sql)  # every statement tagged, so the plan cache view leaves it out
    assert s["engine"] == "sqlserver" and s["version"].startswith("Microsoft SQL Server 2022") and "\n" not in s["version"]
    assert s["counters_since"] == "2026-11-06T19:02:00"
    lp = s["load_profile"]
    assert lp["batch_requests"] == 535 and lp["transactions"] == 1487
    assert lp["blks_hit"] == 99000 and lp["blks_read"] == 1000 and lp["cache_hit_ratio"] == 0.99 and "page_lookups" not in lp
    assert lp["page_life_expectancy_s"] == 120 and lp["sessions_total"] == 4 and lp["sessions_blocked"] == 1  # worst node
    q = s["top_sql"][0]
    assert q["sql_id"] == "0xB858F14FAB5DAA6A" and q["mean_time_ms"] == 1.502 and q["since"] == "2026-11-06T19:10:00"
    assert [r["plan_hash"] for r in s["top_sql"]] == ["0x1111", "0x2222"]  # one row per plan: the comparison merges them
    assert "'%ab%'" not in q["text"] and "42" not in q["text"]  # literals masked before anything is stored
    assert [w["event"] for w in s["wait_events"]] == ["LCK_M_U"]  # idle and background waits left out
    assert s["sessions"] == [{"pid": 61, "state": "suspended", "wait_class": "Lock", "wait_event": "LCK_M_X", "duration_s": 12.0,
                              "blocked_by": 58, "user": "app", "program": "orders-api",
                              "text": s["sessions"][0]["text"]}] and "'paid'" not in s["sessions"][0]["text"]
    assert s["tables"][0] == {"name": "dbo.orders", "seq_scan": 600, "idx_scan": 300, "live_rows": 100000, "last_analyze": "2026-11-06T19:03:00"}
    assert s["settings"]["query_store"] == "READ_WRITE" and s["settings"]["physical_memory_mb"] == 4096


def test_gauges_are_not_differenced():
    """Page life expectancy and the hit ratio are levels, not counters: the comparison must not subtract them."""
    from dbtriage.compare import _is_gauge

    assert _is_gauge("cache_hit_ratio") and _is_gauge("page_life_expectancy_s") and _is_gauge("sessions_blocked")
    assert not _is_gauge("batch_requests")


def test_the_driver_extra_is_named_when_missing(monkeypatch):
    """The friendly message never showed: the driver was imported only inside connect() (agent review on #11)."""
    import builtins
    import sys

    from dbtriage.collectors import get_collector

    real = builtins.__import__

    def no_driver(name, *a, **k):
        if name == "mssql_python":
            raise ModuleNotFoundError("No module named 'mssql_python'")
        return real(name, *a, **k)

    monkeypatch.delitem(sys.modules, "dbtriage.collectors.sqlserver", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_driver)
    with pytest.raises(SystemExit, match="uv sync --extra sqlserver"):
        get_collector(t("mssql://u:p@h/db"))
    from dbtriage.settings import probe

    assert probe(t("mssql://u:p@h/db")) == (False, "SQL Server needs its driver: uv sync --extra sqlserver")


def test_an_empty_dsn_is_a_failed_probe_not_an_exit():
    from dbtriage.settings import probe

    ok, msg = probe(t(""))
    assert ok is False and "SystemExit" in msg


def test_sql_server_targets_get_a_trend():
    from dbtriage.web import App

    assert "batch_requests" in App.LOAD_METRICS


def test_connection_details():
    assert "Encrypt=strict" in ss.conn_string(t("mssql://u:p@h/db?encrypt=strict"))  # TDS 8.0 passes through
    assert "Encrypt=yes" in ss.conn_string(t("mssql://u:p@h/db?encrypt=sometimes"))  # unknown stays encrypted
    assert "Encrypt=no" in ss.conn_string(t("mssql://u:p@h/db?encrypt=no"))
    assert ss.conn_string(t("mssql://u:p@[::1]:1444/db")).startswith("Server=tcp:[::1],1444;")  # IPv6
    calls = {}

    class C:
        timeout = None

    import types

    fake = types.SimpleNamespace(connect=lambda cs, **kw: calls.update(kw) or C())
    real = ss.mssql_python
    ss.mssql_python = fake
    try:
        c = ss.connect(t("mssql://u:p@h/db", call_timeout_ms=500), login_timeout_s=3)
    finally:
        ss.mssql_python = real
    assert calls == {"autocommit": True, "timeout": 3} and c.timeout == 1  # no open transaction; 500 ms is not "no limit"


def test_cxconsumer_is_idle_and_the_old_build_fallback(monkeypatch):
    assert "CXCONSUMER".startswith(tuple(ss.IDLE_WAITS)) and ss.wait_class("CXPACKET") == "Parallelism"

    class Old(FakeCursor):
        def execute(self, sql, *params):
            if "qs.total_spills" in sql:
                raise RuntimeError("Invalid column name 'total_spills'.")
            super().execute(sql, *params)

    cur = Old()
    rows = ss.SQLServerCollector(t("mssql://u:p@h/db"))._statements(cur, 5)
    assert rows and "0 as total_spills" in cur.sql[-1]


def test_a_plan_evicted_and_recompiled_is_a_reset_of_that_statement():
    """The plan cache forgets a statement when its plan is evicted; without the plan's own epoch the next total would
    be subtracted from the old one (review on #11)."""
    from dbtriage.compare import interval_between

    def snap(at, calls, since):
        return {"captured_at": at, "counters_since": "2026-11-06T19:02:00", "load_profile": {},
                "top_sql": [{"sql_id": "q1", "calls": calls, "total_time_ms": calls * 2.0, "plan_hash": "p", "since": since}]}

    same = interval_between(snap("2026-11-06T20:00:00", 100, "a"), snap("2026-11-06T20:10:00", 150, "a"))
    again = interval_between(snap("2026-11-06T20:00:00", 100, "a"), snap("2026-11-06T20:10:00", 150, "b"))
    assert same["sql"]["q1"]["calls"] == 50 and again["sql"]["q1"]["calls"] == 150


def test_plans_are_differenced_one_by_one():
    """Merging plans before differencing hid a new plan behind the old one, and an evicted secondary plan made the
    whole history look like one interval (agent review on #11, e5c5bfe)."""
    from dbtriage.compare import interval_between

    def snap(at, rows):
        return {"captured_at": at, "counters_since": "s", "load_profile": {}, "top_sql": rows}

    def row(plan, calls, since):
        return {"sql_id": "q1", "plan_hash": plan, "calls": calls, "total_time_ms": calls * 2.0, "since": since, "text": plan}

    a = snap("2026-11-06T20:00:00", [row("p1", 1000, "t1"), row("p2", 400, "t2")])
    # p2 evicted; p1 ran 50 more
    s = interval_between(a, snap("2026-11-06T20:10:00", [row("p1", 1050, "t1")]))["sql"]["q1"]
    assert s["calls"] == 50 and "plan_changed_from" not in s
    # a new plan p3 while p1 still dominates: reported
    s = interval_between(a, snap("2026-11-06T20:10:00", [row("p1", 1100, "t1"), row("p2", 400, "t2"), row("p3", 30, "t3")]))["sql"]["q1"]
    assert s["calls"] == 130 and s["plans"] == ["p1", "p3"] and s["plan_changed_from"] == "p1" and s["plan_hash"] == "p1"
    assert s["plan_changed_to"] == "p3"  # the new plan, though p1 still did most of the work
    assert ss._encrypt("optional") == "yes" and ss._encrypt("false") == "no"


def test_the_plan_change_finding_names_the_new_plan():
    """The finding said before p1, after p1 when p3 appeared beside a still-dominant p1 (agent review on #11, f26eee8)."""
    from dbtriage.compare import diff, interval_between

    def snap(at, rows):
        return {"captured_at": at, "counters_since": "s", "load_profile": {}, "wait_events": [], "tables": [],
                "top_sql": [{"sql_id": "q1", "plan_hash": p, "calls": c, "total_time_ms": c * 2.0, "since": f"t{p}"} for p, c in rows]}

    s0, s1 = snap("2026-11-06T20:00:00", [("p1", 1000)]), snap("2026-11-06T20:10:00", [("p1", 1100)])
    s2 = snap("2026-11-06T20:20:00", [("p1", 1200), ("p3", 30)])
    out = diff(interval_between(s0, s1), interval_between(s1, s2))
    f = next(f for f in out["findings"] if f["kind"] == "plan_change")
    assert f["evidence"]["plan_before"] == "p1" and f["evidence"]["plan_after"] == "p3"


def test_the_probe_is_tagged(monkeypatch):
    from dbtriage import settings

    seen = []

    class Cur:
        def execute(self, sql):
            seen.append(sql)

        def fetchone(self):
            return ("Microsoft SQL Server 2022\nmore",)

    class Conn:
        def cursor(self):
            return Cur()

        def close(self):
            pass

    monkeypatch.setattr(ss, "connect", lambda target, login_timeout_s=None: Conn())
    assert settings.probe(t("mssql://u:p@h/db")) == (True, "Microsoft SQL Server 2022")
    assert seen[0].startswith(ss.TAG)


def test_cpu_time_reaches_the_comparison_and_the_pages(tmp_path):
    """Snapshots recorded cpu_time_ms but the comparison dropped it, so no page could show it."""
    import copy

    from dbtriage.compare import diff, interval_between
    from dbtriage.config import Config, Target
    from dbtriage.web import App
    from tests.conftest import LONG_SQL, seed_store

    def snap(at, calls, cpu):
        return {"captured_at": at, "counters_since": "s", "load_profile": {}, "wait_events": [], "tables": [],
                "top_sql": [{"sql_id": "q1", "plan_hash": "p1", "calls": calls, "total_time_ms": calls * 2.0, "cpu_time_ms": cpu, "since": "t"}]}

    s0, s1, s2 = snap("2026-11-06T20:00:00", 100, 150.0), snap("2026-11-06T20:10:00", 200, 300.0), snap("2026-11-06T20:20:00", 400, 700.0)
    top = diff(interval_between(s0, s1), interval_between(s1, s2))["sql"]["top"][0]
    assert top["cpu_time_ms"] == 400.0

    st = seed_store(tmp_path / "data")  # postgres: no CPU time recorded, so no CPU column
    cfg = Config(storage_url="x", targets={"demo": Target("demo", "postgres", "d"), "ms": Target("ms", "sqlserver", "d")}, base_dir=str(tmp_path))
    app = App(cfg, st, False, None)
    pg = "postgres/demo/2026-11-05/20261105T100000Z"
    assert "CPU ms" not in app.snapshot(pg).decode() and "cpu ms" not in app.sql_page("demo", LONG_SQL).decode()
    ms = "sqlserver/ms/2026-11-05/20261105T100000Z"
    for name in ("summary.json", "manifest.json", "compare.json", "triage.json"):
        doc = copy.deepcopy(st.get_json(f"{pg}/{name}"))
        for q in doc.get("top_sql", []) + (doc.get("sql") or {}).get("top", []):
            q["cpu_time_ms"] = 31337.0
        if name == "manifest.json":
            doc.update(id=ms, target="ms", engine="sqlserver")
            doc.pop("baseline", None)
        st.put_json(f"{ms}/{name}", doc)
    page = app.snapshot(ms).decode()
    assert "CPU ms" in page and "31.3k" in page
    assert "cpu ms" in app.sql_page("ms", LONG_SQL).decode()
