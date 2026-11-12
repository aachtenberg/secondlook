"""The SQL Server collector without a server: DSN forms, masking, wait classes, and one snapshot normalized from
canned DMV rows the way the lab's SQL Server 2022 returns them."""
from datetime import datetime, timedelta, timezone

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
                            "query_store": "READ_WRITE", "qs_interval_minutes": 60}],
    # Query Store on, but nothing recorded for the window: auto reads the plan cache
    "from sys.query_store_runtime_stats_interval where": [{"intervals": 0, "start_": None, "end_": None}],
}


class FakeCursor:
    def __init__(self, rows=None):
        self.sql, self.params, self.all = [], [], {**ROWS, **(rows or {})}

    def execute(self, sql, *params):
        self.sql.append(sql)
        self.params.append(params)
        key = next(k for k in self.all if k in " ".join(sql.split()))
        self.rows = self.all[key]
        if isinstance(self.rows, Exception):
            raise self.rows
        self.description = [(c,) for c in self.rows[0]] if self.rows else []

    def fetchall(self):
        return [tuple(r.values()) for r in self.rows]


class FakeConn:
    def __init__(self, rows=None):
        self.cur = FakeCursor(rows)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def test_one_snapshot_from_the_dmvs(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(ss, "connect", lambda target: conn)
    s = ss.SQLServerCollector(t("mssql://u:p@h/app")).collect(Window(start=datetime(2026, 11, 6, 18), end=datetime(2026, 11, 6, 19))).summary
    # every statement tagged after its first word, so the plan cache and Query Store both leave it out
    assert all(q.split(None, 1)[1].startswith(ss.TAG) for q in conn.cur.sql)
    assert s["top_sql_source"] == "plan_cache" and "top_sql_kind" not in s
    assert s["notes"] == ["top SQL from the plan cache: Query Store holds no runtime statistics for the window"]
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


# --------------------------------------------------------------------------- Query Store as the top-SQL source
UTC = timezone.utc
A, B = "0xB858F14FAB5DAA6A", "0x0123456789ABCDEF"
P1, P2, P3 = "0x1111111111111111", "0x2222222222222222", "0x3333333333333333"


def qs_row(sql_id, plan, text_id, n, dur_us, cpu_us, logical, physical, rows, tempdb, last):
    return {"sql_id": sql_id, "plan_hash": plan, "query_text_id": text_id, "count_executions": n, "avg_duration": dur_us,
            "avg_cpu_time": cpu_us, "avg_logical_io_reads": logical, "avg_physical_io_reads": physical, "avg_rowcount": rows,
            "avg_tempdb_space_used": tempdb, "last_execution_time": last}


# what the server returns for a window of 18:30 to 19:30: the top pairs' rows per interval (18:00 and the open 19:00),
# one per execution type, for statement A under two plans and two literal variants, and statement B
QS = {
    "from sys.query_store_runtime_stats_interval where": [{"intervals": 2, "start_": datetime(2026, 11, 6, 18, tzinfo=UTC),
                                                           "end_": datetime(2026, 11, 6, 19, 30, tzinfo=UTC)}],
    "from sys.query_store_runtime_stats rs": [
        qs_row(A, P1, 7, 100, 1500.0, 1000.0, 300.0, 1.5, 1.0, 0.0, datetime(2026, 11, 6, 18, 59, 59, tzinfo=UTC)),
        qs_row(A, P1, 7, 50, 3000.0, 2500.0, 300.0, 0.0, 1.0, 2.0, datetime(2026, 11, 6, 19, 29, tzinfo=UTC)),
        qs_row(A, P1, 8, 10, 100.0, 50.0, 20.0, 0.0, 1.0, 0.0, datetime(2026, 11, 6, 19, 5, tzinfo=UTC)),
        qs_row(A, P1, 7, 2, 30000.0, 100.0, 10.0, 0.0, 0.0, 0.0, datetime(2026, 11, 6, 19, 20, tzinfo=UTC)),  # aborted
        qs_row(A, P2, 7, 5, 20000.0, 19000.0, 5000.4, 10.0, 1.0, 0.0, datetime(2026, 11, 6, 19, 10, tzinfo=UTC)),
        qs_row(B, P3, 9, 1000, 50.0, 40.0, 3.0, 0.0, 1.0, 0.0, datetime(2026, 11, 6, 18, 40, tzinfo=UTC))],
    "from sys.query_store_query_text where": [
        {"query_text_id": 7, "text": "SELECT @n = COUNT(*) FROM dbo.orders WHERE note LIKE '%ab%' AND amount > 42"},
        {"query_text_id": 8, "text": "SELECT @n = COUNT(*) FROM dbo.orders WHERE note LIKE '%cd%' AND amount > 7"},
        {"query_text_id": 9, "text": "UPDATE dbo.orders SET status = 'paid' WHERE id = 3"}],
    "from sys.query_store_wait_stats ws": [
        {"sql_id": A, "plan_hash": P1, "category": "Lock", "time_ms": 120},
        {"sql_id": A, "plan_hash": P1, "category": "CPU", "time_ms": 300},
        {"sql_id": A, "plan_hash": P2, "category": "Buffer IO", "time_ms": 40}],
}
WINDOW = Window(start=datetime(2026, 11, 6, 18, 30), end=datetime(2026, 11, 6, 19, 30))


def collect(monkeypatch, rows=None, window=WINDOW, cursor=None, **extra):
    conn = FakeConn(rows)
    if cursor:
        conn.cur = cursor
    monkeypatch.setattr(ss, "connect", lambda target: conn)
    return ss.SQLServerCollector(t("mssql://u:p@h/app", **extra)).collect(window), conn.cur


def test_auto_reads_the_window_from_query_store(monkeypatch):
    snap, cur = collect(monkeypatch, QS)
    s = snap.summary
    assert s["top_sql_source"] == "query_store" and s["top_sql_kind"] == "interval"
    assert not any("dm_exec_query_stats" in q for q in cur.sql)  # the plan cache is not read at all
    # the window goes to the server as UTC datetimeoffset literals (a naive window time is UTC)
    assert ("2026-11-06 19:30:00.000000 +00:00", "2026-11-06 18:30:00.000000 +00:00") in cur.params
    # the intervals start at 18:00, before the window: the span is recorded and said
    assert s["top_sql_window"] == {"start": "2026-11-06T18:00:00+00:00", "end": "2026-11-06T19:30:00+00:00"}
    assert s["notes"] == ["top SQL from Query Store",
                          "Query Store keeps runtime statistics per 60-minute interval, so top SQL covers 2026-11-06 18:00 "
                          "to 2026-11-06 19:30 UTC, not the window's 2026-11-06 18:30 to 2026-11-06 19:30"]
    p1, p2, b = s["top_sql"]
    # A under P1: four rows over two intervals, aborted runs included; average (microseconds) times count, in ms
    assert (p1["sql_id"], p1["plan_hash"], p1["calls"]) == (A, P1, 162)
    assert p1["total_time_ms"] == 361.0  # (100*1500 + 50*3000 + 10*100 + 2*30000) us
    assert p1["mean_time_ms"] == 2.228 and p1["cpu_time_ms"] == 225.7
    assert (p1["io_blocks_hit"], p1["io_blocks_read"], p1["rows"], p1["temp_blocks"]) == (45220, 150, 160, 100)
    assert p1["last_execution_time"] == "2026-11-06T19:29:00+00:00" and "since" not in p1
    assert p1["wait_categories"] == {"CPU": 300, "Lock": 120}  # heaviest first
    # the text of the literal variant that used the most time, masked
    assert p1["text"].startswith("SELECT @n = COUNT(*)") and "'%ab%'" not in p1["text"] and "42" not in p1["text"]
    assert (p2["plan_hash"], p2["calls"], p2["total_time_ms"], p2["mean_time_ms"], p2["io_blocks_hit"]) == (P2, 5, 100.0, 20.0, 25002)
    assert p2["wait_categories"] == {"Buffer IO": 40}
    assert (b["sql_id"], b["calls"], b["total_time_ms"], b["mean_time_ms"]) == (B, 1000, 50.0, 0.05) and "wait_categories" not in b
    assert snap.raw["query_store_runtime_stats"][0]["sql_id"] == A and "dm_exec_query_stats" not in snap.raw
    assert s["settings"]["query_store"] == "READ_WRITE" and s["load_profile"]["batch_requests"] == 535  # the rest unchanged


def test_a_window_on_interval_boundaries_gets_no_span_note(monkeypatch):
    span = [{"intervals": 1, "start_": datetime(2026, 11, 6, 18, tzinfo=UTC), "end_": datetime(2026, 11, 6, 19, tzinfo=UTC)}]
    s = collect(monkeypatch, {**QS, "from sys.query_store_runtime_stats_interval where": span},
                Window(start=datetime(2026, 11, 6, 18, tzinfo=UTC), end=datetime(2026, 11, 6, 19, tzinfo=UTC)))[0].summary
    assert s["notes"] == ["top SQL from Query Store"] and s["top_sql_window"]["end"] == "2026-11-06T19:00:00+00:00"


def test_the_two_sources_name_a_statement_alike(monkeypatch):
    """sql_id is the query hash and plan_hash the query plan hash, both as convert(varchar(18), ..., 1) gives them, from
    either source: findings, ledger identities and the fleet's text hash then survive a switch of source."""
    import re

    forms = (r"convert\(varchar\(18\), \w+\.query_hash, 1\) as sql_id", r"convert\(varchar\(18\), \w+\.query_plan_hash, 1\) as plan_hash")
    _, qs_cur = collect(monkeypatch, QS)
    _, pc_cur = collect(monkeypatch, top_sql_source="plan_cache")
    for cur, view in ((qs_cur, "sys.query_store_runtime_stats rs"), (pc_cur, "sys.dm_exec_query_stats qs")):
        q = next(q for q in cur.sql if view in q)
        assert all(re.search(f, q) for f in forms), view
    pc = collect(monkeypatch, top_sql_source="plan_cache")[0].summary["top_sql"][0]
    qs = collect(monkeypatch, QS)[0].summary["top_sql"][0]
    assert (pc["sql_id"], qs["sql_id"]) == (A, A) and set(pc) - set(qs) == {"since"}


def test_auto_falls_back_to_the_plan_cache_and_says_why(monkeypatch):
    off = {"from sys.databases": [dict(ROWS["from sys.databases"][0], query_store="OFF")]}
    s, cur = collect(monkeypatch, {**QS, **off})
    assert s.summary["top_sql_source"] == "plan_cache" and "top_sql_kind" not in s.summary
    assert s.summary["notes"] == ["top SQL from the plan cache: Query Store is OFF on app"]
    assert not any("query_store_runtime" in q for q in cur.sql) and s.summary["top_sql"][0]["since"]
    # on, but the server refuses it: still a snapshot, from the plan cache, with the reason
    denied = RuntimeError("The SELECT permission was denied on the object 'query_store_runtime_stats_interval'.")
    s = collect(monkeypatch, {"from sys.query_store_runtime_stats_interval where": denied})[0].summary
    assert s["top_sql_source"] == "plan_cache" and s["notes"][0].startswith(
        "top SQL from the plan cache: Query Store could not be read (RuntimeError: The SELECT permission was denied")
    # forced to the plan cache, Query Store is not looked at
    s, cur = collect(monkeypatch, QS, top_sql_source="plan_cache")
    assert s.summary["notes"] == ["top SQL from the plan cache (top_sql_source = plan_cache)"]
    assert not any("query_store_runtime" in q for q in cur.sql)


def test_forced_query_store_fails_clearly_when_it_is_off(monkeypatch):
    off = {"from sys.databases": [dict(ROWS["from sys.databases"][0], query_store="OFF")]}
    with pytest.raises(SystemExit, match="top_sql_source = \"query_store\", but Query Store is OFF on app"):
        collect(monkeypatch, off, top_sql_source="query_store")
    denied = RuntimeError("The SELECT permission was denied")
    with pytest.raises(RuntimeError, match="permission was denied"):  # the server's reason, not a quiet fallback
        collect(monkeypatch, {"from sys.query_store_runtime_stats_interval where": denied}, top_sql_source="query-store")
    # on but empty for the window: Query Store's answer is that nothing ran, and the snapshot says so
    s = collect(monkeypatch, top_sql_source="query_store")[0].summary
    assert s["top_sql_source"] == "query_store" and s["top_sql"] == [] and s["top_sql_window"] is None
    assert s["notes"] == ["top SQL from Query Store: it holds no runtime statistics for the window"]
    ro = {"from sys.databases": [dict(ROWS["from sys.databases"][0], query_store="READ_ONLY")]}
    s = collect(monkeypatch, {**QS, **ro})[0].summary
    assert s["top_sql_source"] == "query_store" and "READ_ONLY" in s["notes"][1]


def test_a_misspelt_source_fails_before_connecting(monkeypatch):
    monkeypatch.setattr(ss, "connect", lambda target: pytest.fail("connected"))
    with pytest.raises(SystemExit, match="top_sql_source is auto, query_store or plan_cache, not 'querystore'"):
        ss.SQLServerCollector(t("mssql://u:p@h/app", top_sql_source="querystore")).collect(WINDOW)


def test_query_store_on_sql_server_2016(monkeypatch):
    """No avg_tempdb_space_used and no sys.query_store_wait_stats before 2017: the figures without them, quietly."""

    class Old(FakeCursor):
        def execute(self, sql, *params):
            if "rs.avg_tempdb_space_used" in sql:
                raise RuntimeError("Invalid column name 'avg_tempdb_space_used'.")
            super().execute(sql, *params)

    missing = RuntimeError("Invalid object name 'sys.query_store_wait_stats'.")
    s, cur = collect(monkeypatch, cursor=Old({**QS, "from sys.query_store_wait_stats ws": missing}))
    assert s.summary["top_sql_source"] == "query_store" and s.summary["top_sql"][0]["total_time_ms"] == 361.0
    assert not any("wait_categories" in q for q in s.summary["top_sql"])
    assert any("0 as avg_tempdb_space_used" in q for q in cur.sql)


def test_the_manifest_carries_the_notes(tmp_path, monkeypatch):
    from dbtriage import cli
    from dbtriage.config import Config
    from dbtriage.storage import LocalStorage
    from dbtriage.triggers import TriageRequest

    monkeypatch.setattr(ss, "connect", lambda target: FakeConn(QS))
    st = LocalStorage(tmp_path / "s")
    cfg = Config(storage_url=f"file://{tmp_path}/s", targets={"mssql": t("mssql://u:p@h/app")}, base_dir=str(tmp_path))
    sid = cli.collect(cfg, st, TriageRequest(target="mssql", mode="scheduled", source="test"))
    assert st.get_json(f"{sid}/manifest.json")["notes"][0] == "top SQL from Query Store"
    assert st.get_json(f"{sid}/summary.json")["top_sql_source"] == "query_store"


def test_the_reviewers_sql_history_says_which_rows_are_the_window(tmp_path):
    """The snapshot's counters stay cumulative, but Query Store's rows are the window's: the model must not read them
    as totals since the plan was cached."""
    import json

    from dbtriage.agent import TriageAgent
    from dbtriage.storage import LocalStorage

    st = LocalStorage(tmp_path)
    for hh, kind in (("20", "cumulative"), ("21", "interval")):
        sid = f"sqlserver/ms/2026-11-06/20261106T{hh}0000Z"
        st.put_json(f"{sid}/summary.json", qsnap(f"2026-11-06T{hh}:00:00+00:00", [("q1", "p1", 10, 10.0)], kind=kind))
        st.put_json(f"{sid}/manifest.json", {"id": sid, "engine": "sqlserver", "target": "ms", "captured_at": f"2026-11-06T{hh}:00:00+00:00"})
    tools = {x.name: x for x in TriageAgent(st, backend=None)._tools("sqlserver", "ms")}
    hist = json.loads(tools["get_sql_history"].call({"sql_id": "q1", "limit": 5}))
    assert [h["metrics_kind"] for h in hist] == ["interval", "cumulative"]  # newest first


# --------------------------------------------------------------------------- the comparison with Query Store
def qsnap(at, rows, window=None, since="2026-11-06T19:02:00", batches=0, kind="interval"):
    """A SQL Server summary: cumulative load profile, top SQL from Query Store (kind="interval") or the plan cache."""
    s = {"captured_at": at, "counters_since": since, "load_profile": {"batch_requests": batches}, "wait_events": [],
         "tables": [], "top_sql": [{"sql_id": sid, "plan_hash": p, "calls": c, "total_time_ms": ms}
                                   for sid, p, c, ms in rows]}
    if kind == "interval":
        end = datetime.fromisoformat(at)
        s.update(top_sql_kind="interval", top_sql_window=window or {"start": (end - timedelta(hours=1)).isoformat(), "end": at})
    else:
        for r in s["top_sql"]:
            r["since"] = "t"  # the plan's cache time: the same plan epoch in both snapshots
    return s


def test_query_store_top_sql_is_not_subtracted():
    """Query Store's figures are the window's own: subtracting the baseline's would count the previous window against
    this one. The load profile beside them is still cumulative and still differenced."""
    from dbtriage.compare import interval_between

    a = qsnap("2026-11-06T20:00:00+00:00", [("q1", "p1", 100, 200.0)], batches=1000)
    b = qsnap("2026-11-06T21:00:00+00:00", [("q1", "p1", 120, 600.0)], batches=4000)
    iv = interval_between(a, b)
    assert (iv["sql"]["q1"]["calls"], iv["sql"]["q1"]["total_time_ms"], iv["sql"]["q1"]["mean_time_ms"]) == (120, 600.0, 5.0)
    assert iv["load_profile"]["batch_requests"] == 3000 and iv["sql_kind"] == "interval" and iv["sql_elapsed_s"] == 3600
    assert iv["sql_window"] == b["top_sql_window"]
    # fewer calls than the previous window is not a counter reset either
    fewer = interval_between(a, qsnap("2026-11-06T21:00:00+00:00", [("q1", "p1", 30, 90.0)], batches=4000))
    assert fewer["sql"]["q1"]["calls"] == 30 and fewer["counters_reset"] is False


def test_the_plan_cache_path_is_unchanged():
    """The same rows without the marker are cumulative, as before: differenced per plan."""
    from dbtriage.compare import diff, interval_between

    a = qsnap("2026-11-06T20:00:00+00:00", [("q1", "p1", 100, 200.0)], batches=1000, kind="cumulative")
    b = qsnap("2026-11-06T21:00:00+00:00", [("q1", "p1", 120, 600.0)], batches=4000, kind="cumulative")
    iv = interval_between(a, b)
    assert (iv["sql"]["q1"]["calls"], iv["sql"]["q1"]["total_time_ms"]) == (20, 400.0) and iv["load_profile"]["batch_requests"] == 3000
    assert not {"sql_kind", "sql_window", "sql_elapsed_s"} & set(iv)
    assert "sql_window" not in diff(None, iv)["current"]


def test_a_new_plan_in_the_window_is_a_plan_change():
    """Against the reference window's plans, as the plan cache's are; an instance restart between them does not hide
    it, since Query Store outlives the restart."""
    from dbtriage.compare import diff, interval_between

    s0 = qsnap("2026-11-06T19:00:00+00:00", [("q1", "p1", 100, 100.0), ("q2", "p9", 100, 100.0)])
    s1 = qsnap("2026-11-06T20:00:00+00:00", [("q1", "p1", 100, 100.0), ("q2", "p9", 100, 100.0)])
    s2 = qsnap("2026-11-06T21:00:00+00:00", [("q1", "p1", 50, 50.0), ("q1", "p2", 50, 500.0), ("q2", "p9", 100, 100.0)],
               since="2026-11-06T20:30:00")
    cur = interval_between(s1, s2)
    q1 = cur["sql"]["q1"]
    assert cur["counters_reset"] is True and (q1["calls"], q1["total_time_ms"]) == (100, 550.0)
    assert q1["plans"] == ["p1", "p2"] and q1["plan_changed_from"] == "p1" and q1["plan_changed_to"] == "p2"
    out = diff(interval_between(s0, s1), cur)
    f = next(f for f in out["findings"] if f["evidence"].get("sql_id") == "q1")
    assert f["kind"] == "sql_regressed" and f["evidence"]["plan_changed"] is True and f["evidence"]["mean_after_ms"] == 5.5
    assert "plan_changed_from" not in cur["sql"]["q2"]


def test_switching_source_between_snapshots():
    """Plan cache to Query Store: the window's figures as they are, the statement the same. Query Store to the plan
    cache (turned off): there is nothing cumulative to subtract, so the plan cache's figures count whole, as after a
    reset, and no plan change is claimed."""
    from dbtriage.compare import diff, interval_between

    pc0 = qsnap("2026-11-06T19:00:00+00:00", [("q1", "p1", 1000, 1000.0)], kind="cumulative")
    pc1 = qsnap("2026-11-06T20:00:00+00:00", [("q1", "p1", 1100, 1100.0)], kind="cumulative")
    qs2 = qsnap("2026-11-06T21:00:00+00:00", [("q1", "p1", 40, 400.0)])
    out = diff(interval_between(pc0, pc1), interval_between(pc1, qs2))
    assert [c["sql_id"] for c in out["sql"]["changed"]] == ["q1"] and out["sql"]["new"] == []
    assert out["sql"]["changed"][0]["mean_pct"] == 900.0  # 1 ms a call before, 10 now
    back = interval_between(qs2, qsnap("2026-11-06T22:00:00+00:00", [("q1", "p2", 5000, 5000.0)], kind="cumulative"))
    assert back["sql"]["q1"]["calls"] == 5000 and "plan_changed_from" not in back["sql"]["q1"] and back["counters_reset"] is False


def test_statement_rates_are_over_the_span_query_store_covers():
    """A two-hour Query Store span between snapshots an hour apart: calls per second over two hours, not one."""
    from dbtriage.band import measures
    from dbtriage.compare import diff, interval_between

    s0 = qsnap("2026-11-06T19:00:00+00:00", [("q1", "p1", 10, 10.0)])
    s1 = qsnap("2026-11-06T20:00:00+00:00", [("q1", "p1", 100, 100.0)])
    s2 = qsnap("2026-11-06T21:00:00+00:00", [("q1", "p1", 400, 400.0)],
               window={"start": "2026-11-06T19:00:00+00:00", "end": "2026-11-06T21:00:00+00:00"})
    cur = interval_between(s1, s2)
    out = diff(interval_between(s0, s1), cur)
    assert out["current"]["elapsed_s"] == 3600 and out["current"]["sql_window"]["start"] == "2026-11-06T19:00:00+00:00"
    assert out["sql"]["changed"][0]["calls_pct"] == 100.0  # 100 an hour, then 400 in two hours; not +300%
    f = next(f for f in out["findings"] if f["kind"] == "sql_volume")
    assert f["evidence"]["calls_per_s_before"] == 0.028 and f["evidence"]["calls_per_s_after"] == 0.056
    assert measures(cur)["sql_time_ms"] == round(400 / 7200, 6)
