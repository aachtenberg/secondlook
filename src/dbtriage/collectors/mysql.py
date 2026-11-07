"""MySQL / MariaDB collector: Performance Schema statement digests, global status
counters, wait events, table I/O and the process list. Cumulative since restart
(or since TRUNCATE of the summary tables). Needs SELECT on performance_schema.* and
the PROCESS privilege. Pure-python PyMySQL, no client library needed."""
from __future__ import annotations

from datetime import timedelta

import pymysql
import pymysql.cursors

from dbtriage.collectors.base import Snapshot, Window, num, utcnow
from dbtriage.config import Target
from dbtriage.redact import redact_sql

STATUS = (
    "Uptime", "Questions", "Com_commit", "Com_rollback", "Com_select", "Com_insert", "Com_update", "Com_delete",
    "Threads_connected", "Threads_running", "Connections", "Aborted_connects", "Slow_queries",
    "Innodb_buffer_pool_read_requests", "Innodb_buffer_pool_reads", "Innodb_data_writes",
    "Innodb_rows_read", "Innodb_rows_inserted", "Innodb_rows_updated", "Innodb_rows_deleted",
    "Innodb_row_lock_waits", "Innodb_row_lock_time", "Innodb_deadlocks",
    "Created_tmp_disk_tables", "Created_tmp_tables", "Select_full_join", "Select_scan", "Sort_merge_passes",
    "Table_locks_waited", "Bytes_received", "Bytes_sent",
)
VARIABLES = (
    "version", "innodb_buffer_pool_size", "max_connections", "innodb_flush_log_at_trx_commit", "sync_binlog",
    "long_query_time", "slow_query_log", "performance_schema", "tmp_table_size", "max_heap_table_size",
    "innodb_io_capacity", "table_open_cache", "sort_buffer_size", "join_buffer_size",
)


def connect(target: Target):
    """A read-only session: the server refuses any write on it, whatever the user's grants allow."""
    return pymysql.connect(**_conn_kwargs(target), cursorclass=pymysql.cursors.DictCursor, read_timeout=120,
                           program_name="dbtriage", init_command="SET SESSION TRANSACTION READ ONLY")


class MySQLCollector:
    def __init__(self, target: Target):
        self.target = target

    def collect(self, window: Window) -> Snapshot:
        conn = connect(self.target)
        try:
            cur = conn.cursor()
            cur.execute("show global variables where variable_name in (%s)" % ",".join(["%s"] * len(VARIABLES)), VARIABLES)
            variables = {r["Variable_name"].lower(): r["Value"] for r in cur.fetchall()}
            cur.execute("show global status where variable_name in (%s)" % ",".join(["%s"] * len(STATUS)), STATUS)
            status = {r["Variable_name"]: num(r["Value"]) for r in cur.fetchall()}
            cur.execute("select database() as db")
            dbname = cur.fetchone()["db"] or "(all)"

            cur.execute(
                """select schema_name, digest, digest_text, count_star, sum_timer_wait, sum_lock_time,
                          sum_rows_sent, sum_rows_examined, sum_rows_affected, sum_created_tmp_disk_tables,
                          sum_no_index_used, sum_sort_merge_passes
                   from performance_schema.events_statements_summary_by_digest
                   where digest is not null order by sum_timer_wait desc limit %s""", (self.target.top_n,))
            digests = cur.fetchall()

            cur.execute(
                """select event_name, count_star, sum_timer_wait
                   from performance_schema.events_waits_summary_global_by_event_name
                   where event_name <> 'idle' and count_star > 0 order by sum_timer_wait desc limit 40""")
            waits = cur.fetchall()

            cur.execute(
                """select object_schema, object_name, count_read, count_write, count_fetch,
                          sum_timer_wait, sum_timer_read, sum_timer_write
                   from performance_schema.table_io_waits_summary_by_table
                   where object_schema not in ('mysql','performance_schema','sys','information_schema')
                   order by sum_timer_wait desc limit %s""", (self.target.top_n,))
            tables = cur.fetchall()

            cur.execute(
                """select t.processlist_id as id, t.processlist_user as user, t.processlist_command as command,
                          t.processlist_time as time, t.processlist_state as state, t.processlist_info as info
                   from performance_schema.threads t
                   where t.type = 'FOREGROUND' and t.processlist_id <> connection_id()
                     and t.processlist_command not in ('Sleep','Daemon','Binlog Dump')""")
            procs = cur.fetchall()
        finally:
            conn.close()

        uptime = status.get("Uptime") or 0
        since = (utcnow() - timedelta(seconds=uptime)).isoformat() if uptime else None
        reqs, reads = status.get("Innodb_buffer_pool_read_requests") or 0, status.get("Innodb_buffer_pool_reads") or 0
        ps = 1_000_000_000  # performance_schema timers are picoseconds; /1e9 -> ms

        summary = {
            "engine": "mysql",
            "target": self.target.name,
            "db": dbname,
            "version": variables.get("version", "?"),
            "captured_at": utcnow().isoformat(),
            "window": window.as_dict(),
            "metrics_kind": "cumulative",
            "counters_since": since,
            "load_profile": {
                "sessions_total": status.get("Threads_connected"),
                "sessions_active": status.get("Threads_running"),
                "questions": status.get("Questions"),
                "xact_commit": status.get("Com_commit"),
                "xact_rollback": status.get("Com_rollback"),
                "com_select": status.get("Com_select"),
                "com_insert": status.get("Com_insert"),
                "com_update": status.get("Com_update"),
                "com_delete": status.get("Com_delete"),
                "connections": status.get("Connections"),
                "aborted_connects": status.get("Aborted_connects"),
                "slow_queries": status.get("Slow_queries"),
                "blks_hit": reqs,
                "blks_read": reads,
                "cache_hit_ratio": round(1 - reads / reqs, 4) if reqs else None,
                "rows_read": status.get("Innodb_rows_read"),
                "tup_inserted": status.get("Innodb_rows_inserted"),
                "tup_updated": status.get("Innodb_rows_updated"),
                "tup_deleted": status.get("Innodb_rows_deleted"),
                "row_lock_waits": status.get("Innodb_row_lock_waits"),
                "row_lock_time_ms": status.get("Innodb_row_lock_time"),
                "deadlocks": status.get("Innodb_deadlocks"),
                "tmp_disk_tables": status.get("Created_tmp_disk_tables"),
                "tmp_tables": status.get("Created_tmp_tables"),
                "full_joins": status.get("Select_full_join"),
                "full_scans": status.get("Select_scan"),
                "sort_merge_passes": status.get("Sort_merge_passes"),
                "table_locks_waited": status.get("Table_locks_waited"),
            },
            "top_sql": [{
                "sql_id": d["digest"], "schema": d["schema_name"], "text": redact_sql(d["digest_text"]),
                "calls": num(d["count_star"]), "total_time_ms": round((num(d["sum_timer_wait"]) or 0) / ps, 3),
                "mean_time_ms": round((num(d["sum_timer_wait"]) or 0) / ps / (num(d["count_star"]) or 1), 3),
                "lock_time_ms": round((num(d["sum_lock_time"]) or 0) / ps, 3),
                "rows": num(d["sum_rows_sent"]), "rows_examined": num(d["sum_rows_examined"]),
                "rows_affected": num(d["sum_rows_affected"]), "temp_blocks": num(d["sum_created_tmp_disk_tables"]),
                "no_index_used": num(d["sum_no_index_used"]), "sort_merge_passes": num(d["sum_sort_merge_passes"]),
            } for d in digests],
            "wait_events": [{"class": w["event_name"].split("/")[1] if "/" in w["event_name"] else w["event_name"],
                             "event": w["event_name"], "count": num(w["count_star"]),
                             "time_ms": round((num(w["sum_timer_wait"]) or 0) / ps, 1)} for w in waits],
            "sessions": [{"id": p["id"], "user": p["user"], "state": p["state"] or p["command"],
                          "duration_s": num(p["time"]), "text": redact_sql(p["info"])}
                         for p in procs if (num(p["time"]) or 0) >= 5],
            "tables": [{"name": f"{t['object_schema']}.{t['object_name']}", "reads": num(t["count_read"]),
                        "writes": num(t["count_write"]), "fetches": num(t["count_fetch"]),
                        "time_ms": round((num(t["sum_timer_wait"]) or 0) / ps, 1)} for t in tables],
            "settings": variables,
            "findings": [],
        }
        if variables.get("performance_schema", "ON").upper() != "ON":
            summary["notes"] = ["performance_schema is OFF; statement digests and waits are unavailable"]
        raw = {"global_status": status, "digests": digests, "waits": waits, "table_io": tables,
               "processlist": [dict(p, info=redact_sql(p["info"])) for p in procs]}
        return Snapshot(summary=summary, raw=raw)


def _conn_kwargs(target: Target) -> dict:
    """Accept either a mysql:// URL in dsn or host/port/user/password/database keys."""
    from urllib.parse import unquote, urlparse

    if target.dsn.startswith("mysql://"):
        u = urlparse(target.dsn)
        return {"host": u.hostname or "localhost", "port": u.port or 3306, "user": unquote(u.username or ""),
                "password": unquote(u.password or ""), "database": u.path.lstrip("/") or None}
    raise SystemExit(f"target {target.name!r}: mysql dsn must look like mysql://user:pass@host:3306/db")
