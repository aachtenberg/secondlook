"""PostgreSQL collector: pg_stat_statements, pg_stat_activity, pg_stat_database,
pg_stat_user_tables, a subset of pg_settings. Needs a role with pg_monitor
(or pg_read_all_stats) so query text in pg_stat_statements is visible."""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from dbtriage.collectors.base import Snapshot, Window, num, utcnow
from dbtriage.config import Target
from dbtriage.redact import redact_sql

SETTINGS = (
    "shared_buffers", "work_mem", "maintenance_work_mem", "effective_cache_size",
    "max_connections", "random_page_cost", "autovacuum", "autovacuum_vacuum_scale_factor",
    "checkpoint_timeout", "max_wal_size", "wal_level", "jit", "track_io_timing",
)


class PostgresCollector:
    def __init__(self, target: Target):
        self.target = target

    def collect(self, window: Window) -> Snapshot:
        with psycopg.connect(self.target.dsn, row_factory=dict_row, application_name="dbtriage") as conn:
            conn.read_only = True
            cur = conn.cursor()

            version = cur.execute("select version()").fetchone()["version"]
            dbname = cur.execute("select current_database() as d").fetchone()["d"]

            db = cur.execute(
                """select numbackends, xact_commit, xact_rollback, blks_read, blks_hit,
                          tup_returned, tup_fetched, tup_inserted, tup_updated, tup_deleted,
                          temp_files, temp_bytes, deadlocks, stats_reset
                   from pg_stat_database where datname = current_database()"""
            ).fetchone()

            has_pss = cur.execute(
                "select count(*) as n from pg_extension where extname = 'pg_stat_statements'"
            ).fetchone()["n"] > 0
            statements = []
            if has_pss:
                statements = cur.execute(
                    """select queryid, calls, total_exec_time, mean_exec_time, rows,
                              shared_blks_hit, shared_blks_read, temp_blks_written,
                              wal_bytes, query
                       from pg_stat_statements
                       where dbid = (select oid from pg_database where datname = current_database())
                       order by total_exec_time desc limit %s""",
                    (self.target.top_n,),
                ).fetchall()

            activity = cur.execute(
                """select pid, state, wait_event_type, wait_event, backend_type,
                          extract(epoch from now() - query_start) as duration_s,
                          extract(epoch from now() - xact_start) as xact_s,
                          query
                   from pg_stat_activity
                   where backend_type = 'client backend' and pid <> pg_backend_pid()"""
            ).fetchall()

            tables = cur.execute(
                """select schemaname || '.' || relname as name, seq_scan, seq_tup_read, idx_scan,
                          n_live_tup, n_dead_tup, n_mod_since_analyze, last_autovacuum, last_autoanalyze
                   from pg_stat_user_tables
                   order by seq_tup_read desc nulls last limit %s""",
                (self.target.top_n,),
            ).fetchall()

            settings = {
                r["name"]: r["setting"] + (r["unit"] or "")
                for r in cur.execute(
                    "select name, setting, unit from pg_settings where name = any(%s)", (list(SETTINGS),)
                ).fetchall()
            }

            locks = cur.execute(
                """select count(*) as n from pg_locks where not granted"""
            ).fetchone()["n"]

        # ---- normalize ----
        waits: dict[tuple[str, str], int] = {}
        active = 0
        sessions = []
        for a in activity:
            if a["state"] == "active":
                active += 1
            if a["wait_event_type"]:
                k = (a["wait_event_type"], a["wait_event"] or "")
                waits[k] = waits.get(k, 0) + 1
            dur = num(a["duration_s"]) or 0
            if a["state"] == "active" and dur >= 5 or (a["state"] or "").startswith("idle in transaction") and (num(a["xact_s"]) or 0) >= 30:
                sessions.append({
                    "pid": a["pid"], "state": a["state"], "wait_class": a["wait_event_type"],
                    "wait_event": a["wait_event"], "duration_s": dur, "text": redact_sql(a["query"]),
                })

        hits, reads = num(db["blks_hit"]) or 0, num(db["blks_read"]) or 0
        summary = {
            "engine": "postgres",
            "target": self.target.name,
            "db": dbname,
            "version": version.split(" on ")[0],
            "captured_at": utcnow().isoformat(),
            "window": window.as_dict(),
            "counters_since": db["stats_reset"].isoformat() if db["stats_reset"] else None,
            "load_profile": {
                "sessions_total": num(db["numbackends"]),
                "sessions_active": active,
                "xact_commit": num(db["xact_commit"]),
                "xact_rollback": num(db["xact_rollback"]),
                "blks_hit": hits,
                "blks_read": reads,
                "cache_hit_ratio": round(hits / (hits + reads), 4) if hits + reads else None,
                "tup_returned": num(db["tup_returned"]),
                "tup_fetched": num(db["tup_fetched"]),
                "tup_inserted": num(db["tup_inserted"]),
                "tup_updated": num(db["tup_updated"]),
                "tup_deleted": num(db["tup_deleted"]),
                "temp_files": num(db["temp_files"]),
                "temp_bytes": num(db["temp_bytes"]),
                "deadlocks": num(db["deadlocks"]),
                "locks_waiting": num(locks),
            },
            "top_sql": [
                {
                    "sql_id": str(s["queryid"]),
                    "text": redact_sql(s["query"]),
                    "calls": num(s["calls"]),
                    "total_time_ms": num(s["total_exec_time"]),
                    "mean_time_ms": num(s["mean_exec_time"]),
                    "rows": num(s["rows"]),
                    "io_blocks_hit": num(s["shared_blks_hit"]),
                    "io_blocks_read": num(s["shared_blks_read"]),
                    "temp_blocks": num(s["temp_blks_written"]),
                    "wal_bytes": num(s["wal_bytes"]),
                }
                for s in statements
            ],
            "wait_events": [
                {"class": c, "event": e, "count": n}
                for (c, e), n in sorted(waits.items(), key=lambda kv: -kv[1])
            ],
            "sessions": sessions,
            "tables": [
                {
                    "name": t["name"], "seq_scan": num(t["seq_scan"]), "seq_tup_read": num(t["seq_tup_read"]),
                    "idx_scan": num(t["idx_scan"]), "live_rows": num(t["n_live_tup"]), "dead_rows": num(t["n_dead_tup"]),
                    "mod_since_analyze": num(t["n_mod_since_analyze"]),
                    "last_vacuum": t["last_autovacuum"].isoformat() if t["last_autovacuum"] else None,
                    "last_analyze": t["last_autoanalyze"].isoformat() if t["last_autoanalyze"] else None,
                }
                for t in tables
            ],
            "settings": settings,
            "findings": [],
            "notes": [] if has_pss else ["pg_stat_statements is not installed; top_sql is empty"],
        }
        raw = {
            "pg_stat_database": db,
            "pg_stat_statements": statements,
            "pg_stat_activity": [dict(a, query=redact_sql(a["query"])) for a in activity],
            "pg_stat_user_tables": tables,
        }
        return Snapshot(summary=summary, raw=raw)
