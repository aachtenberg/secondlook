"""Oracle collector.

With the Diagnostics Pack (target.diagnostics_pack = true) it pulls, for the AWR
snapshots that bracket the requested window:
  * the AWR text report (DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_TEXT), stored as a report
  * ADDM findings from DBA_ADDM_FINDINGS for the auto-ADDM task covering the window
  * top SQL from DBA_HIST_SQLSTAT deltas (with plan hash), wait classes from
    DBA_HIST_SYSTEM_EVENT deltas, load profile from DBA_HIST_SYSSTAT / SYS_TIME_MODEL
  * ASH sample counts from DBA_HIST_ACTIVE_SESS_HISTORY
Metrics in that mode are interval values ("metrics_kind": "interval").

Without the pack (Standard Edition or unlicensed) it only reads cumulative V$ views
(V$SQLSTATS, V$SYSTEM_EVENT, V$SYSSTAT, V$SESSION) and never touches DBA_HIST*,
so a licence audit has nothing to find. Those metrics are "cumulative" and are
diffed against the previous snapshot like Postgres/MySQL.

Read-only user: see compose/oracle/init.sql (CREATE SESSION, SELECT_CATALOG_ROLE,
EXECUTE ON DBMS_WORKLOAD_REPOSITORY). Thin-mode python-oracledb, no Instant Client.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import oracledb

from dbtriage.collectors.base import Snapshot, Window, num, utcnow
from dbtriage.config import Target
from dbtriage.redact import mask_report_literals, redact_sql

PARAMS = (
    "sga_target", "pga_aggregate_target", "memory_target", "cpu_count", "processes", "sessions",
    "optimizer_mode", "cursor_sharing", "db_block_size", "compatible", "optimizer_adaptive_plans",
    "control_management_pack_access", "statistics_level",
)
SYSSTATS = (
    "user commits", "user rollbacks", "user calls", "execute count", "parse count (total)",
    "parse count (hard)", "session logical reads", "physical reads", "physical writes",
    "redo size", "sorts (disk)", "table scans (long tables)", "logons cumulative",
)
TIME_MODEL = ("DB time", "DB CPU", "sql execute elapsed time", "parse time elapsed")


def _rows(cur, sql, **binds):
    cur.execute(sql, binds)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _clob(v) -> str:
    if v is None:
        return ""
    return v.read() if hasattr(v, "read") else str(v)


def connect(target: Target):
    """A session for dbtriage: the collector reads on it, and only take_awr_snapshot (opt-in, dbtriage.dbsnap) writes."""
    conn = oracledb.connect(dsn=target.dsn, user=target.extra.get("user") or None,  # blank: the DSN's own
                            password=target.extra.get("password") or None)
    conn.call_timeout = int(target.extra.get("call_timeout_ms", 120_000))
    conn.module = "dbtriage"  # lets the collector filter its own statements out of the report
    return conn


def take_awr_snapshot(target: Target, window: Window) -> str | None:
    """Take an AWR snapshot now, unless the newest is younger than min_snapshot_age_min (default 5). Only called
    through dbtriage.dbsnap, when targets.toml allows writes and this target asks for snapshots."""
    if not target.diagnostics_pack:
        raise RuntimeError("AWR needs the Diagnostics Pack: set diagnostics_pack = true for this target if it is licensed")
    with connect(target) as conn:
        cur = conn.cursor()
        access = _rows(cur, "select value from v$parameter where name = 'control_management_pack_access'")
        if not access or "DIAGNOSTIC" not in (access[0]["value"] or "").upper():
            raise RuntimeError("control_management_pack_access does not include DIAGNOSTIC on this database")
        dbid = _rows(cur, "select dbid from v$database")[0]["dbid"]
        age = _rows(cur, """select (sysdate - cast(max(end_interval_time) as date)) * 1440 as age_min
                            from dba_hist_snapshot where dbid = :dbid""", dbid=dbid)[0]["age_min"]
        if age is not None and float(age) < float(target.extra.get("min_snapshot_age_min", 5)):
            return None
        snap_id = cur.callfunc("dbms_workload_repository.create_snapshot", int)  # commits on its own
        return f"AWR snapshot {snap_id}"


def _same_startup(snaps: list[dict]) -> list[dict]:
    """The snapshots from the most recent instance startup among these, in order. AWR cannot report across a restart
    (ORA-20019), so a window that spans one is reported from the restart on."""
    return [s for s in snaps if s["startup_time"] == snaps[-1]["startup_time"]] if snaps else []


def _pick_snaps(cur, dbid, inst_no, start, end) -> tuple[list[dict], bool]:
    """The AWR snapshots to report on: those in the window, from one instance startup; else the two newest of the
    latest startup, so the run still produces something. Returns them and whether the window spanned a restart."""
    snaps = _rows(cur, """
        select snap_id, begin_interval_time, end_interval_time, startup_time
        from dba_hist_snapshot
        where dbid = :dbid and instance_number = :inst
          and end_interval_time >= :s and begin_interval_time <= :e
        order by snap_id""", dbid=dbid, inst=inst_no, s=start, e=end)
    spans_restart = len({s["startup_time"] for s in snaps}) > 1
    snaps = _same_startup(snaps)
    if len(snaps) < 2:
        snaps = _rows(cur, """
            select * from (select snap_id, begin_interval_time, end_interval_time, startup_time from dba_hist_snapshot
                           where dbid = :dbid and instance_number = :inst
                             and startup_time = (select max(startup_time) from dba_hist_snapshot
                                                 where dbid = :dbid and instance_number = :inst)
                           order by snap_id desc)
            where rownum <= 2""", dbid=dbid, inst=inst_no)[::-1]
    if len(snaps) < 2:
        raise SystemExit("AWR has fewer than two snapshots since the instance last started (AWR cannot report across "
                         "a restart); nothing to report on yet")
    return snaps, spans_restart


class OracleCollector:
    def __init__(self, target: Target):
        self.target = target

    def collect(self, window: Window) -> Snapshot:
        with connect(self.target) as conn:
            cur = conn.cursor()
            inst = _rows(cur, "select instance_number, instance_name, version_full, startup_time from v$instance")[0]
            dbr = _rows(cur, "select dbid, name, sys_context('userenv','con_name') as con from v$database")[0]
            params = {r["name"]: r["value"] for r in _rows(
                cur, "select name, value from v$parameter where name in (%s)" % ",".join(f"'{p}'" for p in PARAMS))}
            sessions = self._sessions(cur)

            pack_ok = "DIAGNOSTIC" in (params.get("control_management_pack_access") or "").upper()
            if self.target.diagnostics_pack and pack_ok:
                summary, raw, reports = self._awr(cur, window, dbr["dbid"], inst["instance_number"])
            else:
                summary, raw, reports = self._vdollar(cur, inst)
                summary["notes"] = [
                    "Diagnostics Pack not enabled for this target; collected V$ views only (no AWR/ADDM/ASH)."
                ]

        summary.update({
            "engine": "oracle",
            "target": self.target.name,
            "db": f"{dbr['name']}/{dbr['con']}",
            "instance": inst["instance_name"],
            "version": inst["version_full"],
            "captured_at": utcnow().isoformat(),
            "window": window.as_dict(),
            "sessions": sessions,
            "settings": params,
        })
        summary["load_profile"]["sessions_active"] = sum(1 for s in sessions if s["state"] == "ACTIVE")
        summary["load_profile"]["sessions_blocked"] = sum(1 for s in sessions if s.get("blocked_by"))
        return Snapshot(summary=summary, raw=raw, reports=reports)

    # ------------------------------------------------------------------ V$ live
    def _sessions(self, cur) -> list[dict]:
        rows = _rows(cur, """
            select s.sid, s.serial#, s.status, s.username, s.wait_class, s.event, s.last_call_et,
                   s.blocking_session, s.sql_id, q.sql_text
            from v$session s left join v$sqlstats q on q.sql_id = s.sql_id
            where s.type = 'USER' and s.username is not null
              and s.sid <> sys_context('userenv','sid')
              and (s.blocking_session is not null
                   or (s.status = 'ACTIVE' and s.wait_class <> 'Idle' and s.last_call_et >= 5))""")
        return [{
            "sid": r["sid"], "state": r["status"], "user": r["username"], "wait_class": r["wait_class"],
            "wait_event": r["event"], "duration_s": num(r["last_call_et"]), "blocked_by": r["blocking_session"],
            "sql_id": r["sql_id"], "text": redact_sql(r["sql_text"]),
        } for r in rows]

    def _vdollar(self, cur, inst):
        sql = _rows(cur, """
            select * from (
              select sql_id, plan_hash_value, executions, elapsed_time, cpu_time, buffer_gets, disk_reads,
                     rows_processed, direct_writes, sql_text
              from v$sqlstats where sql_text not like 'begin dbms_workload_repository%'
              order by elapsed_time desc)
            where rownum <= :n""", n=self.target.top_n)
        events = _rows(cur, """
            select wait_class, event, total_waits, time_waited_micro
            from v$system_event where wait_class <> 'Idle' order by time_waited_micro desc""")
        stats = {r["name"]: num(r["value"]) for r in _rows(
            cur, "select name, value from v$sysstat where name in (%s)" % ",".join(f"'{s}'" for s in SYSSTATS))}
        tm = {r["stat_name"]: num(r["value"]) for r in _rows(
            cur, "select stat_name, value from v$sys_time_model where stat_name in (%s)" % ",".join(f"'{s}'" for s in TIME_MODEL))}
        n_sess = _rows(cur, "select count(*) as n from v$session where type = 'USER'")[0]["n"]
        summary = {
            "metrics_kind": "cumulative",
            "counters_since": inst["startup_time"].isoformat(),
            "load_profile": _load_profile(stats, tm, n_sess),
            "top_sql": [_sql_row(r, "executions", "elapsed_time", "cpu_time", "buffer_gets", "disk_reads",
                                 "rows_processed", "direct_writes") for r in sql],
            "wait_events": [{"class": e["wait_class"], "event": e["event"], "count": num(e["total_waits"]),
                             "time_ms": round((num(e["time_waited_micro"]) or 0) / 1000, 1)} for e in events[:40]],
            "tables": self._stale_tables(cur),
            "findings": [],
        }
        return summary, {"v$sqlstats": sql, "v$system_event": events, "v$sysstat": stats, "v$sys_time_model": tm}, {}

    def _stale_tables(self, cur) -> list[dict]:
        rows = _rows(cur, """
            select * from (
              select owner || '.' || table_name as name, num_rows, stale_stats, last_analyzed
              from dba_tab_statistics
              where owner not in ('SYS','SYSTEM','XDB','MDSYS','CTXSYS','DBSNMP','OUTLN','ORDSYS','WMSYS','AUDSYS')
                and object_type = 'TABLE' and stale_stats = 'YES'
              order by num_rows desc nulls last)
            where rownum <= :n""", n=self.target.top_n)
        return [{"name": r["name"], "live_rows": num(r["num_rows"]), "stale_stats": True,
                 "last_analyze": r["last_analyzed"].isoformat() if r["last_analyzed"] else None} for r in rows]

    # ------------------------------------------------------------------ AWR
    def _awr(self, cur, window: Window, dbid, inst_no):
        reports: dict[str, str] = {}
        # AWR timestamps are naive, in the database server's clock; shift the UTC window to that clock. Compare two
        # DATEs: subtracting sys_extract_utc() from systimestamp would convert through the *session* time zone, which
        # the driver takes from the client machine, and shift the window by the client's UTC offset.
        off_s = _rows(cur, "select round((sysdate - cast(sys_extract_utc(systimestamp) as date)) * 86400) as off from dual")[0]["off"]
        offset = timedelta(seconds=float(off_s or 0))
        local = lambda dt: (dt.astimezone(timezone.utc) + offset).replace(tzinfo=None)  # noqa: E731

        # The driver binds a datetime as DATE, which has no fractions of a second: a window ending at :16.9 would be
        # compared as :16.0 and drop a snapshot that began at :16.6. Bind both bounds as TIMESTAMP.
        cur.setinputsizes(s=oracledb.DB_TYPE_TIMESTAMP, e=oracledb.DB_TYPE_TIMESTAMP)
        snaps, spans_restart = _pick_snaps(cur, dbid, inst_no, local(window.start), local(window.end))
        b, e = snaps[0]["snap_id"], snaps[-1]["snap_id"]
        begin_t, end_t = snaps[0]["begin_interval_time"], snaps[-1]["end_interval_time"]
        elapsed_s = (end_t - begin_t).total_seconds()

        cur.execute("select output from table(dbms_workload_repository.awr_report_text(:d, :i, :b, :e))",
                    d=dbid, i=inst_no, b=b, e=e)
        reports["awr.txt"] = "\n".join(r[0] or "" for r in cur.fetchall())

        sql = _rows(cur, """
            select * from (
              select s.sql_id, s.plan_hash_value,
                     sum(s.executions_delta) as executions, sum(s.elapsed_time_delta) as elapsed_time,
                     sum(s.cpu_time_delta) as cpu_time, sum(s.buffer_gets_delta) as buffer_gets,
                     sum(s.disk_reads_delta) as disk_reads, sum(s.rows_processed_delta) as rows_processed,
                     sum(s.direct_writes_delta) as direct_writes,
                     max(dbms_lob.substr(t.sql_text, 2000, 1)) as sql_text
              from dba_hist_sqlstat s join dba_hist_sqltext t on t.dbid = s.dbid and t.sql_id = s.sql_id
              where s.dbid = :dbid and s.instance_number = :inst and s.snap_id > :b and s.snap_id <= :e
                and s.parsing_schema_name <> 'SYS' and nvl(s.module, '-') not like 'dbtriage%'
                and t.sql_text not like 'begin dbms_workload_repository%'
              group by s.sql_id, s.plan_hash_value
              order by elapsed_time desc)
            where rownum <= :n""", dbid=dbid, inst=inst_no, b=b, e=e, n=self.target.top_n)

        events = _rows(cur, """
            select e.wait_class, e.event_name as event,
                   sum(e.total_waits - nvl(p.total_waits, 0)) as total_waits,
                   sum(e.time_waited_micro - nvl(p.time_waited_micro, 0)) as time_waited_micro
            from dba_hist_system_event e
            left join dba_hist_system_event p
              on p.dbid = e.dbid and p.instance_number = e.instance_number and p.event_id = e.event_id and p.snap_id = :b
            where e.dbid = :dbid and e.instance_number = :inst and e.snap_id = :e and e.wait_class <> 'Idle'
            group by e.wait_class, e.event_name order by time_waited_micro desc""", dbid=dbid, inst=inst_no, b=b, e=e)

        stats = {r["stat_name"]: num(r["v"]) for r in _rows(cur, """
            select e.stat_name, e.value - nvl(p.value, 0) as v
            from dba_hist_sysstat e left join dba_hist_sysstat p
              on p.dbid = e.dbid and p.instance_number = e.instance_number and p.stat_id = e.stat_id and p.snap_id = :b
            where e.dbid = :dbid and e.instance_number = :inst and e.snap_id = :e
              and e.stat_name in (%s)""" % ",".join(f"'{s}'" for s in SYSSTATS), dbid=dbid, inst=inst_no, b=b, e=e)}
        tm = {r["stat_name"]: num(r["v"]) for r in _rows(cur, """
            select e.stat_name, e.value - nvl(p.value, 0) as v
            from dba_hist_sys_time_model e left join dba_hist_sys_time_model p
              on p.dbid = e.dbid and p.instance_number = e.instance_number and p.stat_id = e.stat_id and p.snap_id = :b
            where e.dbid = :dbid and e.instance_number = :inst and e.snap_id = :e
              and e.stat_name in (%s)""" % ",".join(f"'{s}'" for s in TIME_MODEL), dbid=dbid, inst=inst_no, b=b, e=e)}

        ash = _rows(cur, """
            select * from (
              select nvl(wait_class, 'CPU') as wait_class, nvl(event, 'ON CPU') as event, sql_id, count(*) as samples
              from dba_hist_active_sess_history
              where dbid = :dbid and instance_number = :inst and snap_id > :b and snap_id <= :e
              group by wait_class, event, sql_id order by samples desc)
            where rownum <= 50""", dbid=dbid, inst=inst_no, b=b, e=e)

        addm = _rows(cur, """
            select f.task_name, t.owner, f.finding_name, f.type, f.message, f.more_info, f.perc_active_sess
            from dba_addm_findings f join dba_addm_tasks t on t.task_id = f.task_id
            where t.dbid = :dbid and t.begin_snap_id >= :b and t.end_snap_id <= :e and f.filtered = 'N'
            order by f.perc_active_sess desc nulls last""", dbid=dbid, b=b, e=e)
        if addm:  # auto-ADDM tasks belong to SYS: read them through DBMS_ADVISOR with the owner (needs ADVISOR)
            try:
                cur.execute("""select dbms_advisor.get_task_report(task_name => :t, type => 'TEXT', level => 'ALL',
                                      section => 'ALL', owner_name => :o) from dual""",
                            t=addm[0]["task_name"], o=addm[0]["owner"])
                reports["addm.txt"] = _clob(cur.fetchone()[0])
            except oracledb.DatabaseError as exc:  # ADVISOR privilege missing: findings table still works
                reports["addm.txt"] = f"(report unavailable: {exc})"

        n_sess = _rows(cur, "select count(*) as n from v$session where type = 'USER'")[0]["n"]
        summary = {
            "metrics_kind": "interval",
            "awr": {"begin_snap": b, "end_snap": e, "begin": begin_t.isoformat(), "end": end_t.isoformat(),
                    "elapsed_s": elapsed_s, "startup": snaps[-1]["startup_time"].isoformat(),
                    **({"note": "the window spans an instance restart; AWR cannot report across one, so this "
                                "covers the time since the restart"} if spans_restart else {})},
            "counters_since": begin_t.isoformat(),
            "load_profile": _load_profile(stats, tm, n_sess, elapsed_s),
            "top_sql": [_sql_row(r, "executions", "elapsed_time", "cpu_time", "buffer_gets", "disk_reads",
                                 "rows_processed", "direct_writes") for r in sql],
            "wait_events": [{"class": r["wait_class"], "event": r["event"], "count": num(r["total_waits"]),
                             "time_ms": round((num(r["time_waited_micro"]) or 0) / 1000, 1)} for r in events[:40]],
            "ash": [{"class": r["wait_class"], "event": r["event"], "sql_id": r["sql_id"], "samples": num(r["samples"])}
                    for r in ash],
            "tables": self._stale_tables(cur),
            "findings": [{"id": f"{r['task_name']}:{r['finding_name']}", "finding": r["finding_name"],
                          "type": r["type"], "impact_pct": num(r["perc_active_sess"]),
                          "message": _clob(r["message"]), "detail": _clob(r["more_info"])} for r in addm],
        }
        sql = [dict(r, sql_text=redact_sql(_clob(r.get("sql_text")))) if "sql_text" in r else r for r in sql]  # raw keeps no literals either
        raw = {"snapshots": snaps, "dba_hist_sqlstat": sql, "dba_hist_system_event": events,
               "dba_hist_sysstat": stats, "dba_hist_sys_time_model": tm, "ash": ash, "addm": addm}
        if self.target.extra.get("mask_report_literals", True):
            reports = {name: mask_report_literals(text) for name, text in reports.items()}
        return summary, raw, reports


def _sql_row(r, ex, el, cpu, gets, reads, rows, dw) -> dict:
    execs = num(r[ex]) or 0
    el_ms = (num(r[el]) or 0) / 1000
    return {
        "sql_id": r["sql_id"], "plan_hash": num(r["plan_hash_value"]), "text": redact_sql(_clob(r["sql_text"])),
        "calls": execs, "total_time_ms": round(el_ms, 3), "mean_time_ms": round(el_ms / execs, 3) if execs else None,
        "cpu_time_ms": round((num(r[cpu]) or 0) / 1000, 3), "rows": num(r[rows]),
        "io_blocks_hit": num(r[gets]), "io_blocks_read": num(r[reads]), "temp_blocks": num(r[dw]),
    }


def _load_profile(stats: dict, tm: dict, n_sess, elapsed_s: float | None = None) -> dict:
    lp = {
        "sessions_total": num(n_sess),
        "db_time_ms": round((tm.get("DB time") or 0) / 1000, 1),
        "db_cpu_ms": round((tm.get("DB CPU") or 0) / 1000, 1),
        "sql_exec_ms": round((tm.get("sql execute elapsed time") or 0) / 1000, 1),
        "parse_ms": round((tm.get("parse time elapsed") or 0) / 1000, 1),
        "xact_commit": stats.get("user commits"),
        "xact_rollback": stats.get("user rollbacks"),
        "user_calls": stats.get("user calls"),
        "executions": stats.get("execute count"),
        "parses": stats.get("parse count (total)"),
        "hard_parses": stats.get("parse count (hard)"),
        "blks_hit": stats.get("session logical reads"),
        "blks_read": stats.get("physical reads"),
        "blks_written": stats.get("physical writes"),
        "redo_bytes": stats.get("redo size"),
        "sorts_disk": stats.get("sorts (disk)"),
        "long_table_scans": stats.get("table scans (long tables)"),
        "logons": stats.get("logons cumulative"),
    }
    lr, pr = lp["blks_hit"] or 0, lp["blks_read"] or 0
    lp["cache_hit_ratio"] = round(1 - pr / lr, 4) if lr else None
    if elapsed_s:
        lp["elapsed_s"] = elapsed_s
        lp["avg_active_sessions"] = round(lp["db_time_ms"] / 1000 / elapsed_s, 2)
    return lp
