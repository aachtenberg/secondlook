"""SQL Server collector: the plan-cache statistics, wait statistics, sessions and requests, performance counters and
configuration, through the DMVs. Works on SQL Server 2016 and later (spills from 2016 SP2 / 2017 CU3), on Linux or
Windows, and Azure SQL Managed Instance; the counters are cumulative, like Postgres and MySQL, so two snapshots are
compared as an interval. Statements are the connected database's.

The login needs VIEW SERVER STATE (SQL Server 2022: VIEW SERVER PERFORMANCE STATE is enough) and, in the database
it connects to, VIEW DATABASE STATE. It needs no other permission: collection only reads. Driver: Microsoft's
mssql-python (uv sync --extra sqlserver), which needs no separate ODBC install.

dsn: mssql://user:password@host:1433/database?encrypt=yes&trust_server_certificate=no
     or a connection string as is, for anything else (Entra ID, ...):
     Server=tcp:host,1433;Database=app;Authentication=ActiveDirectoryMsi;Encrypt=yes
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import mssql_python  # the optional extra; importing this module without it tells get_collector to say so

from dbtriage.collectors.base import Snapshot, Window, num, utcnow
from dbtriage.config import Target
from dbtriage.redact import redact_sql

TAG = "/* dbtriage */"  # on every statement the collector runs, so the plan-cache view can leave them out

# Waits that only mean "nothing to do" (idle threads, timers, background queues): they would drown the real ones.
IDLE_WAITS = (
    "BROKER_", "CHECKPOINT_QUEUE", "CHKPT", "CLR_AUTO_EVENT", "CLR_MANUAL_EVENT", "CLR_SEMAPHORE", "DBMIRROR",
    "DIRTY_PAGE_POLL", "DISPATCHER_QUEUE_SEMAPHORE", "FT_IFTS", "FT_IFTSHC_MUTEX", "HADR_CLUSAPI_CALL",
    "HADR_FILESTREAM_IOMGR_IOCOMPLETION", "HADR_LOGCAPTURE_WAIT", "HADR_NOTIFICATION_DEQUEUE", "HADR_TIMER_TASK",
    "HADR_WORK_QUEUE", "KSOURCE_WAKEUP", "LAZYWRITER_SLEEP", "LOGMGR_QUEUE", "MEMORY_ALLOCATION_EXT",
    "ONDEMAND_TASK_QUEUE", "PARALLEL_REDO_", "PREEMPTIVE_OS_FLUSHFILEBUFFERS", "PREEMPTIVE_XE_", "PWAIT_ALL_COMPONENTS",
    "PWAIT_DIRECTLOGCONSUMER_GETNEXT", "PWAIT_EXTENSIBILITY_CLEANUP_TASK", "QDS_", "REDO_THREAD_PENDING_WORK",
    "REQUEST_FOR_DEADLOCK_SEARCH", "RESOURCE_QUEUE", "SERVER_IDLE_CHECK", "SLEEP_", "SNI_HTTP_ACCEPT",
    "SOS_WORK_DISPATCHER", "SP_SERVER_DIAGNOSTICS_SLEEP", "SQLTRACE_", "UCS_SESSION_REGISTRATION", "VDI_CLIENT_OTHER",
    "WAIT_FOR_RESULTS", "WAITFOR", "WAIT_XTP_", "XE_", "STARTUP_DEPENDENCY_MANAGER",
    "AZURE_IMDS_VERSIONS",  # SQL Server on Linux polling for Azure instance metadata in the background
    "CXCONSUMER",  # the consumer side of a parallel plan (2016 SP2+): benign; CXPACKET is the one to watch
)
# wait type prefix -> class, most specific first (what Query Store calls wait categories, roughly)
WAIT_CLASSES = (
    ("LCK_", "Lock"), ("PAGEIOLATCH_", "Buffer IO"), ("PAGELATCH_", "Buffer latch"), ("LATCH_", "Latch"),
    ("WRITELOG", "Log IO"), ("LOGBUFFER", "Log IO"), ("LOG_RATE_GOVERNOR", "Log IO"), ("ASYNC_NETWORK_IO", "Network"),
    ("NETWORK", "Network"), ("CXPACKET", "Parallelism"), ("CXSYNC", "Parallelism"),
    ("SOS_SCHEDULER_YIELD", "CPU"), ("THREADPOOL", "Worker thread"), ("RESOURCE_SEMAPHORE", "Memory"),
    ("CMEMTHREAD", "Memory"), ("IO_COMPLETION", "Other disk IO"), ("ASYNC_IO_COMPLETION", "Other disk IO"),
    ("BACKUP", "Backup"), ("HADR_", "Replication"), ("PREEMPTIVE_", "Preemptive"),
)
# cumulative per-second counters (cntr_type 272696576) worth comparing, and what they are called in the summary
COUNTERS = {
    ("SQL Statistics", "Batch Requests/sec"): "batch_requests",
    ("SQL Statistics", "SQL Compilations/sec"): "sql_compilations",
    ("SQL Statistics", "SQL Re-Compilations/sec"): "sql_recompilations",
    ("Databases", "Transactions/sec"): "transactions",
    ("Databases", "Log Flushes/sec"): "log_flushes",
    ("Buffer Manager", "Page reads/sec"): "page_reads",
    ("Buffer Manager", "Page lookups/sec"): "page_lookups",
    ("Buffer Manager", "Page writes/sec"): "page_writes",
    ("Locks", "Lock Waits/sec"): "lock_waits",
    ("Locks", "Number of Deadlocks/sec"): "deadlocks",
    ("Access Methods", "Full Scans/sec"): "full_scans",
    ("Access Methods", "Index Searches/sec"): "index_searches",
    ("General Statistics", "Logins/sec"): "logins",
}
SETTINGS = ("max server memory (MB)", "min server memory (MB)", "max degree of parallelism",
            "cost threshold for parallelism", "optimize for ad hoc workloads", "max worker threads")


def wait_class(wait_type: str) -> str:
    return next((c for prefix, c in WAIT_CLASSES if wait_type.startswith(prefix)), "Other")


def conn_string(target: Target) -> str:
    """The driver's connection string for a target's dsn (a mssql:// URL, or a connection string as is)."""
    dsn = target.dsn
    if not dsn.startswith(("mssql://", "sqlserver://")):
        if "=" in dsn and ";" in dsn:
            return dsn
        raise SystemExit(f"target {target.name!r}: a SQL Server dsn is mssql://user:pass@host:1433/db or a connection "
                         "string like Server=host,1433;Database=db;...")
    u = urlparse(dsn)
    q = {k.lower(): v[-1] for k, v in parse_qs(u.query).items()}
    host = u.hostname or "localhost"
    host = f"tcp:[{host}]" if ":" in host else host  # IPv6
    parts = [f"Server={host},{u.port or 1433}", f"Database={unquote(u.path.lstrip('/')) or 'master'}"]
    if u.username:
        parts += [f"UID={unquote(u.username)}", "PWD={" + unquote(u.password or "").replace("}", "}}") + "}"]
    parts += [f"Encrypt={_encrypt(q.get('encrypt'))}", f"TrustServerCertificate={_trust(q.get('trust_server_certificate'))}"]
    if q.get("authentication"):
        parts.append(f"Authentication={q['authentication']}")
    return ";".join(parts)


def _encrypt(v: str | None) -> str:
    """strict (TDS 8.0) passes through; an explicit no turns it off; anything else, unknown included, stays on."""
    v = (v or "yes").lower()
    return "strict" if v == "strict" else "no" if v in ("no", "false", "0") else "yes"  # "optional" included: on


def _trust(v: str | None) -> str:
    """Trusting any server certificate only when asked; anything unknown keeps checking it."""
    return "yes" if (v or "").lower() in ("yes", "true", "1") else "no"


def connect(target: Target, login_timeout_s: int | None = None):
    """A connection in autocommit: a DB-API connection otherwise opens a transaction on the first statement and keeps it
    for the whole collection, which can hold the log-truncation point while the plan cache is read."""
    conn = mssql_python.connect(conn_string(target), autocommit=True,
                                timeout=int(login_timeout_s or target.extra.get("connect_timeout_s", 10)))
    ms = int(target.extra.get("call_timeout_ms", 120_000))
    conn.timeout = max(1, -(-ms // 1000))  # per statement, whole seconds; 0 would mean no limit at all
    return conn


def _rows(cur, sql: str, *params) -> list[dict]:
    cur.execute(f"{TAG}\n{sql}", *params) if params else cur.execute(f"{TAG}\n{sql}")
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


class SQLServerCollector:
    def __init__(self, target: Target):
        self.target = target

    def _statements(self, cur, n: int) -> list[dict]:
        """The connected database's statements from the plan cache: one row per query hash and plan (the comparison
        merges them and sees a plan change), the text of the plan that used the most time, the oldest plan's cache
        time as the counters' start. Hash 0 means "no hash" and is left out; so are dbtriage's own statements."""
        sql = """
            with q as (
                select qs.query_hash, qs.query_plan_hash, qs.execution_count, qs.total_elapsed_time, qs.total_worker_time,
                       qs.total_logical_reads, qs.total_physical_reads, qs.total_rows, {spills} as total_spills,
                       qs.creation_time, qs.sql_handle, qs.statement_start_offset, qs.statement_end_offset,
                       row_number() over (partition by qs.query_hash, qs.query_plan_hash order by qs.total_elapsed_time desc) as rn
                from sys.dm_exec_query_stats qs
                cross apply sys.dm_exec_plan_attributes(qs.plan_handle) pa
                where pa.attribute = 'dbid' and convert(int, pa.value) = db_id() and qs.query_hash <> 0x0000000000000000
            ), g as (
                select query_hash, query_plan_hash, sum(execution_count) as calls, sum(total_elapsed_time) as elapsed,
                       sum(total_worker_time) as worker, sum(total_logical_reads) as logical_reads,
                       sum(total_physical_reads) as physical_reads, sum(total_rows) as rows_, sum(total_spills) as spills,
                       min(creation_time) as since
                from q group by query_hash, query_plan_hash
            )
            select top ({n}) convert(varchar(18), g.query_hash, 1) as sql_id, convert(varchar(18), g.query_plan_hash, 1) as plan_hash,
                   g.calls, g.elapsed / 1000.0 as total_time_ms, g.worker / 1000.0 as cpu_time_ms, g.logical_reads,
                   g.physical_reads, g.rows_, g.spills, g.since,
                   substring(t.text, q.statement_start_offset / 2 + 1,
                             (case q.statement_end_offset when -1 then datalength(t.text) else q.statement_end_offset end
                              - q.statement_start_offset) / 2 + 1) as text
            from g
            join q on q.query_hash = g.query_hash and q.query_plan_hash = g.query_plan_hash and q.rn = 1
            cross apply sys.dm_exec_sql_text(q.sql_handle) t
            where t.text not like '%/* dbtriage */%'
            order by g.elapsed desc"""
        try:
            return _rows(cur, sql.format(spills="qs.total_spills", n=n))
        except Exception as exc:  # noqa: BLE001 - total_spills is 2016 SP2 / 2017 CU3 and later
            if "total_spills" not in str(exc):
                raise
            return _rows(cur, sql.format(spills="0", n=n))

    def collect(self, window: Window) -> Snapshot:
        conn = connect(self.target)
        try:
            cur = conn.cursor()
            info = _rows(cur, """select @@version as version, db_name() as db, sqlserver_start_time,
                                        cpu_count, physical_memory_kb
                                 from sys.dm_os_sys_info""")[0]
            n = int(self.target.top_n)
            statements = self._statements(cur, n)
            waits = _rows(cur, """select wait_type, waiting_tasks_count, wait_time_ms, signal_wait_time_ms
                                  from sys.dm_os_wait_stats where wait_time_ms > 0""")
            counters = _rows(cur, """select rtrim(object_name) as object_name, rtrim(counter_name) as counter_name,
                                            rtrim(instance_name) as instance_name, cntr_value, cntr_type
                                     from sys.dm_os_performance_counters
                                     where (object_name not like '%:Databases%' and instance_name in ('', '_Total'))
                                        or (object_name like '%:Databases%' and instance_name = db_name())
                                        or counter_name = 'Page life expectancy'""")
            requests = _rows(cur, """
                select r.session_id, r.status, r.wait_type, r.wait_time, r.blocking_session_id,
                       r.total_elapsed_time / 1000.0 as duration_s, s.login_name, s.program_name, t.text
                from sys.dm_exec_requests r
                join sys.dm_exec_sessions s on s.session_id = r.session_id
                outer apply sys.dm_exec_sql_text(r.sql_handle) t
                where s.is_user_process = 1 and r.session_id <> @@spid""")
            user_sessions = _rows(cur, "select count(*) as n from sys.dm_exec_sessions where is_user_process = 1")[0]["n"]
            tables = _rows(cur, f"""
                select top ({n}) s.name + '.' + o.name as name,
                       sum(case when u.index_id in (0, 1) then u.user_scans else 0 end) as scans,  -- heap / clustered only
                       sum(u.user_seeks + u.user_lookups) as seeks,
                       (select sum(p.rows) from sys.partitions p where p.object_id = o.object_id and p.index_id in (0, 1)) as rows_,
                       (select max(stats_date(st.object_id, st.stats_id)) from sys.stats st where st.object_id = o.object_id) as stats_date
                from sys.dm_db_index_usage_stats u
                join sys.objects o on o.object_id = u.object_id and o.type = 'U'
                join sys.schemas s on s.schema_id = o.schema_id
                where u.database_id = db_id()
                group by s.name, o.name, o.object_id
                order by sum(case when u.index_id in (0, 1) then u.user_scans else 0 end) desc""")
            config = {r["name"]: r["value_in_use"] for r in _rows(
                cur, "select name, value_in_use from sys.configurations where name in (%s)" % ",".join("?" * len(SETTINGS)),
                *SETTINGS)}
            db = _rows(cur, """select compatibility_level, recovery_model_desc, is_read_committed_snapshot_on,
                                      (select actual_state_desc from sys.database_query_store_options) as query_store
                               from sys.databases where database_id = db_id()""")[0]
        finally:
            conn.close()

        # ---- normalize ----
        idle = tuple(IDLE_WAITS)
        real_waits = sorted((w for w in waits if not w["wait_type"].startswith(idle)), key=lambda w: -w["wait_time_ms"])
        perf = {(r["object_name"].split(":")[-1], r["counter_name"]): r for r in counters}
        load = {name: num(perf[k]["cntr_value"]) for k, name in COUNTERS.items() if k in perf}
        nodes = [num(r["cntr_value"]) for r in counters if r["object_name"].endswith("Buffer Node") and r["counter_name"] == "Page life expectancy"]
        overall = perf.get(("Buffer Manager", "Page life expectancy"))
        ple = min(nodes) if nodes else (num(overall["cntr_value"]) if overall else None)  # the worst NUMA node
        lookups, reads = load.pop("page_lookups", None), load.get("page_reads")
        blocked = [r for r in requests if r["blocking_session_id"]]
        sessions = [{
            "pid": r["session_id"], "state": r["status"], "wait_class": wait_class(r["wait_type"]) if r["wait_type"] else None,
            "wait_event": r["wait_type"], "duration_s": num(r["duration_s"]), "blocked_by": r["blocking_session_id"] or None,
            "user": r["login_name"], "program": r["program_name"], "text": redact_sql(r["text"]),
        } for r in requests if r["blocking_session_id"] or (num(r["duration_s"]) or 0) >= 5]
        summary = {
            "engine": "sqlserver",
            "target": self.target.name,
            "db": info["db"],
            "version": info["version"].splitlines()[0].strip(),
            "captured_at": utcnow().isoformat(),
            "window": window.as_dict(),
            # wait statistics and performance counters start over when the instance restarts; each statement's counters
            # start when its plan was cached (its "since"), so an evicted and recompiled plan is a reset of that row only
            "counters_since": info["sqlserver_start_time"].isoformat() if info["sqlserver_start_time"] else None,
            "load_profile": {
                **load,
                "sessions_total": num(user_sessions),
                "sessions_active": sum(1 for r in requests if r["status"] in ("running", "runnable", "suspended")),
                "sessions_blocked": len(blocked),
                # cumulative page lookups and physical reads, as Postgres's blks_hit / blks_read: the comparison then works
                # out the hit ratio for the interval, not the lifetime ratio that barely moves on a long-running instance
                "blks_hit": (lookups - reads) if lookups is not None and reads is not None else None,
                "blks_read": reads,
                "cache_hit_ratio": round((lookups - reads) / lookups, 4) if lookups and reads is not None else None,
                "page_life_expectancy_s": ple,
            },
            "top_sql": [{
                "sql_id": s["sql_id"], "text": redact_sql(s["text"]), "calls": num(s["calls"]),
                "total_time_ms": num(s["total_time_ms"]),
                "mean_time_ms": round(num(s["total_time_ms"]) / num(s["calls"]), 3) if num(s["calls"]) else None,
                "cpu_time_ms": num(s["cpu_time_ms"]), "rows": num(s["rows_"]), "io_blocks_hit": num(s["logical_reads"]),
                "io_blocks_read": num(s["physical_reads"]), "temp_blocks": num(s["spills"]), "plan_hash": s["plan_hash"],
                # this plan's counters start when it was cached, not when the instance did: a new value means they started over
                "since": s["since"].isoformat() if s["since"] else None,
            } for s in statements],
            "wait_events": [{"class": wait_class(w["wait_type"]), "event": w["wait_type"], "count": num(w["waiting_tasks_count"]),
                             "time_ms": num(w["wait_time_ms"]), "signal_ms": num(w["signal_wait_time_ms"])} for w in real_waits[:40]],
            "sessions": sessions,
            "tables": [{"name": t["name"], "seq_scan": num(t["scans"]), "idx_scan": num(t["seeks"]), "live_rows": num(t["rows_"]),
                        "last_analyze": t["stats_date"].isoformat() if t["stats_date"] else None} for t in tables],
            "settings": {**{k: num(v) for k, v in config.items()},
                         "compatibility_level": num(db["compatibility_level"]), "recovery_model": db["recovery_model_desc"],
                         "read_committed_snapshot": bool(db["is_read_committed_snapshot_on"]), "query_store": db["query_store"],
                         "cpu_count": num(info["cpu_count"]), "physical_memory_mb": round(num(info["physical_memory_kb"]) / 1024)},
            "findings": [],
            "notes": [] if statements else ["the plan cache returned no statements (just restarted, or cleared)"],
        }
        raw = {
            "dm_exec_query_stats": [dict(s, text=redact_sql(s["text"])) for s in statements],
            "dm_os_wait_stats": real_waits,
            "dm_os_performance_counters": counters,
            "dm_exec_requests": [dict(r, text=redact_sql(r["text"])) for r in requests],
            "dm_db_index_usage_stats": tables,
        }
        return Snapshot(summary=summary, raw=raw)
