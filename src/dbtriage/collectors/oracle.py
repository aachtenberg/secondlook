"""Oracle collector.

oracle_source picks what it reads, per target: auto (the default), awr, statspack or vdollar.

With the Diagnostics Pack (target.diagnostics_pack = true) it pulls, for the AWR
snapshots that bracket the requested window:
  * the AWR text report (DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_TEXT), stored as a report
  * ADDM findings from DBA_ADDM_FINDINGS for the auto-ADDM task covering the window
  * top SQL from DBA_HIST_SQLSTAT deltas (with plan hash), wait classes from
    DBA_HIST_SYSTEM_EVENT deltas, load profile from DBA_HIST_SYSSTAT / SYS_TIME_MODEL
  * ASH sample counts from DBA_HIST_ACTIVE_SESS_HISTORY
Metrics in that mode are interval values ("metrics_kind": "interval").

Without the pack, Statspack (installed by the DBA with spcreate.sql) gives the same shape from the PERFSTAT.STATS$
tables: two snapshots around the window from one startup, deltas of STATS$SQL_SUMMARY, STATS$SYSTEM_EVENT,
STATS$SYSSTAT and STATS$SYS_TIME_MODEL, plan hashes from STATS$SQL_PLAN_USAGE, and a short statspack.txt built from
those numbers (spreport.sql is SQL*Plus only). Also "interval". auto reads it when the pack is off and such a pair
exists.

Otherwise it only reads cumulative V$ views (V$SQLSTATS, V$SYSTEM_EVENT, V$SYSSTAT, V$SESSION). Without the pack it
never touches DBA_HIST*, whatever the source, so a licence audit has nothing to find. V$ metrics are "cumulative" and
are diffed against the previous snapshot like Postgres/MySQL.

Read-only user: see compose/oracle/init.sql (CREATE SESSION, SELECT_CATALOG_ROLE,
EXECUTE ON DBMS_WORKLOAD_REPOSITORY), and for Statspack SELECT on the STATS$ tables it reads
(docs/configuration.md). Thin-mode python-oracledb, no Instant Client.
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
SOURCES = ("auto", "awr", "statspack", "vdollar")
STATSPACK_LEVELS = (0, 5, 6, 7, 10)  # what STATSPACK.SNAP takes: SQL from 5, plan usage from 6, segment statistics from 7
SQL_STATS = ("executions", "elapsed_time", "cpu_time", "buffer_gets", "disk_reads", "rows_processed", "direct_writes")


def _rows(cur, sql, **binds):
    cur.execute(sql, binds)
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _clob(v) -> str:
    if v is None:
        return ""
    return v.read() if hasattr(v, "read") else str(v)


def _first_line(exc: Exception) -> str:
    return (str(exc).splitlines() or [""])[0][:200]


def source_of(target: Target) -> str:
    """oracle_source as one of SOURCES, blank meaning auto. ValueError otherwise (a snapshot taker must not exit)."""
    v = target.extra.get("oracle_source") or "auto"
    if v not in SOURCES:
        raise ValueError(f"oracle_source: one of {', '.join(SOURCES)}")
    return v


def statspack_level(target: Target) -> int:
    """The level dbtriage's own Statspack snapshots are taken at: statspack_level, else 7."""
    v = target.extra.get("statspack_level")
    level = 7 if v in (None, "") else int(v)
    if level not in STATSPACK_LEVELS:
        raise ValueError(f"statspack_level: one of {', '.join(map(str, STATSPACK_LEVELS))}")
    return level


def connect(target: Target):
    """A session for dbtriage: the collector reads on it, and only the snapshot takers (opt-in, dbtriage.dbsnap) write."""
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


def take_statspack_snapshot(target: Target, window: Window) -> str | None:
    """Take a Statspack snapshot now (PERFSTAT.STATSPACK.SNAP at statspack_level), unless the newest is younger than
    min_snapshot_age_min (default 5). Only called through dbtriage.dbsnap, when targets.toml allows writes and this
    target asks for snapshots. Needs EXECUTE on PERFSTAT.STATSPACK; installing Statspack is the DBA's step."""
    level = statspack_level(target)
    with connect(target) as conn:
        cur = conn.cursor()
        dbid = _rows(cur, "select dbid from v$database")[0]["dbid"]
        inst = _rows(cur, "select instance_number from v$instance")[0]["instance_number"]
        try:
            age = _rows(cur, """select (sysdate - max(snap_time)) * 1440 as age_min from perfstat.stats$snapshot
                                where dbid = :dbid and instance_number = :inst""", dbid=dbid, inst=inst)[0]["age_min"]
        except oracledb.DatabaseError as exc:
            raise RuntimeError(f"Statspack cannot be read here (PERFSTAT.STATS$SNAPSHOT: {_first_line(exc)}); "
                               "the DBA installs it with spcreate.sql") from exc
        if age is not None and float(age) < float(target.extra.get("min_snapshot_age_min", 5)):
            return None
        # the function form of SNAP returns the new snap_id, and commits on its own
        snap_id = cur.callfunc("perfstat.statspack.snap", int, keyword_parameters={"i_snap_level": level})
        return f"Statspack snapshot {snap_id} (level {level})"


def takes_statspack(target: Target) -> bool:
    """Whether this target's own snapshots are Statspack's: asked for, or auto without the pack (with it, AWR)."""
    src = source_of(target)
    if src == "vdollar":
        raise RuntimeError("oracle_source = vdollar reads only V$ views, so there is no snapshot to take")
    return src == "statspack" or (src == "auto" and not target.diagnostics_pack)


def _db_clock(cur):
    """UTC datetime -> the database server's own clock, naive, which is how AWR and Statspack store their times."""
    # Compare two DATEs: subtracting sys_extract_utc() from systimestamp would convert through the *session* time zone,
    # which the driver takes from the client machine, and shift the window by the client's UTC offset.
    off_s = _rows(cur, "select round((sysdate - cast(sys_extract_utc(systimestamp) as date)) * 86400) as off from dual")[0]["off"]
    offset = timedelta(seconds=float(off_s or 0))
    return lambda dt: (dt.astimezone(timezone.utc) + offset).replace(tzinfo=None)


def _same_startup(snaps: list[dict]) -> list[dict]:
    """The snapshots from the most recent instance startup among these, in order. AWR cannot report across a restart
    (ORA-20019) and Statspack's counters start over at one, so a window that spans one is reported from the restart on."""
    return [s for s in snaps if s["startup_time"] == snaps[-1]["startup_time"]] if snaps else []


# Per source: the snapshots around the window, and the two newest of the latest startup. AWR snapshots are intervals,
# picked by overlap; Statspack's are points in time, so the one at or before the window's start and the one at or
# after its end bound it, with any inside it between.
SNAP_QUERIES = {
    "awr": ("""
        select snap_id, begin_interval_time, end_interval_time, startup_time
        from dba_hist_snapshot
        where dbid = :dbid and instance_number = :inst
          and end_interval_time >= :s and begin_interval_time <= :e
        order by snap_id""", """
            select * from (select snap_id, begin_interval_time, end_interval_time, startup_time from dba_hist_snapshot
                           where dbid = :dbid and instance_number = :inst
                             and startup_time = (select max(startup_time) from dba_hist_snapshot
                                                 where dbid = :dbid and instance_number = :inst)
                           order by snap_id desc)
            where rownum <= 2"""),
    "statspack": ("""
        select snap_id, snap_time, startup_time, snap_level
        from perfstat.stats$snapshot
        where dbid = :dbid and instance_number = :inst
          and snap_time >= nvl((select max(snap_time) from perfstat.stats$snapshot
                                where dbid = :dbid and instance_number = :inst and snap_time <= :s), :s)
          and snap_time <= nvl((select min(snap_time) from perfstat.stats$snapshot
                                where dbid = :dbid and instance_number = :inst and snap_time >= :e), :e)
        order by snap_id""", """
            select * from (select snap_id, snap_time, startup_time, snap_level from perfstat.stats$snapshot
                           where dbid = :dbid and instance_number = :inst
                             and startup_time = (select max(startup_time) from perfstat.stats$snapshot
                                                 where dbid = :dbid and instance_number = :inst)
                           order by snap_id desc)
            where rownum <= 2"""),
}
ONE_STARTUP = {"awr": "AWR cannot report across a restart", "statspack": "its counters start over at a restart"}


def _pick_snaps(cur, dbid, inst_no, start, end, source: str = "awr", latest: bool = True) -> tuple[list[dict], bool]:
    """The snapshots to report on: those around the window, from one instance startup; else, with `latest`, the two
    newest of the latest startup, so the run still produces something. Returns them and whether the window spanned a
    restart. Without `latest` (auto probing Statspack) fewer than two come back as they are, for the caller to judge."""
    around, newest = SNAP_QUERIES[source]
    snaps = _rows(cur, around, dbid=dbid, inst=inst_no, s=start, e=end)
    spans_restart = len({s["startup_time"] for s in snaps}) > 1
    snaps = _same_startup(snaps)
    if len(snaps) < 2 and latest:
        snaps = _rows(cur, newest, dbid=dbid, inst=inst_no)[::-1]
        if len(snaps) < 2:
            name = "AWR" if source == "awr" else "Statspack"
            raise SystemExit(f"{name} has fewer than two snapshots since the instance last started ({ONE_STARTUP[source]}); "
                             "nothing to report on yet")
    return snaps, spans_restart


def _level(snap: dict) -> int:
    return 5 if snap.get("snap_level") is None else int(snap["snap_level"])  # 5 is Statspack's own default


class OracleCollector:
    def __init__(self, target: Target):
        self.target = target

    def collect(self, window: Window) -> Snapshot:
        try:
            source_of(self.target)
        except ValueError as exc:
            raise SystemExit(f"target {self.target.name!r}: {exc}") from exc
        with connect(self.target) as conn:
            cur = conn.cursor()
            inst = _rows(cur, "select instance_number, instance_name, version_full, startup_time from v$instance")[0]
            dbr = _rows(cur, "select dbid, name, sys_context('userenv','con_name') as con from v$database")[0]
            params = {r["name"]: r["value"] for r in _rows(
                cur, "select name, value from v$parameter where name in (%s)" % ",".join(f"'{p}'" for p in PARAMS))}
            sessions = self._sessions(cur)

            pack_ok = "DIAGNOSTIC" in (params.get("control_management_pack_access") or "").upper()
            source, why, picked = self._choose(conn, window, dbr["dbid"], inst["instance_number"], pack_ok)
            if source == "awr":
                summary, raw, reports = self._awr(cur, window, dbr["dbid"], inst["instance_number"])
                summary["notes"] = [f"Source: AWR snapshots {summary['awr']['begin_snap']}-{summary['awr']['end_snap']} ({why})."]
            elif source == "statspack":
                summary, raw, reports = self._statspack(cur, window, dbr, inst, why, *picked)
            else:
                summary, raw, reports = self._vdollar(cur, inst)
                summary["notes"] = [f"Source: V$ views only, cumulative since the instance started ({why}); no AWR, ADDM or ASH."]

        summary.update({
            "engine": "oracle",
            "oracle_source": source,
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

    def _choose(self, conn, window: Window, dbid, inst_no, pack_ok: bool):
        """(source, why, picked) for this run. auto reads AWR when the pack is on and the database allows it, then
        Statspack when two of its snapshots from one startup are around the window, then V$. picked is Statspack's
        (snapshots, spans_restart, clock). DBA_HIST is never read unless diagnostics_pack is true."""
        want, pack = source_of(self.target), self.target.diagnostics_pack
        if want == "vdollar":
            return "vdollar", "oracle_source = vdollar", None
        if want != "statspack" and pack and pack_ok:
            return "awr", "oracle_source = awr" if want == "awr" else "diagnostics_pack = true", None
        no_pack = "no Diagnostics Pack for this target" if not pack else "control_management_pack_access does not include DIAGNOSTIC"
        if want == "awr":
            return "vdollar", f"oracle_source = awr, but {no_pack}", None
        cur = conn.cursor()  # its own cursor: a probe that fails leaves nothing behind on the one collection reads with
        local = _db_clock(cur)
        # bind the bounds as TIMESTAMP: the driver's DATE would drop fractions of a second (see _awr)
        cur.setinputsizes(s=oracledb.DB_TYPE_TIMESTAMP, e=oracledb.DB_TYPE_TIMESTAMP)
        try:
            snaps, spans = _pick_snaps(cur, dbid, inst_no, local(window.start), local(window.end), "statspack",
                                       latest=want == "statspack")
        except oracledb.DatabaseError as exc:  # ORA-00942: not installed, or no SELECT on it
            if want == "statspack":
                raise SystemExit(f"oracle_source = statspack, but PERFSTAT.STATS$SNAPSHOT cannot be read ({_first_line(exc)}): "
                                 "the DBA installs Statspack with spcreate.sql and grants SELECT on its tables "
                                 "(docs/configuration.md)") from exc
            return "vdollar", f"{no_pack}, and Statspack cannot be read: PERFSTAT.STATS$SNAPSHOT gives {_first_line(exc)}", None
        if len(snaps) < 2:  # auto only: with oracle_source = statspack, _pick_snaps took the newest pair or stopped
            found = ("the Statspack snapshots around the window span an instance restart, with fewer than two after it"
                     if spans else f"Statspack has {len(snaps) or 'no'} snapshot{'' if len(snaps) == 1 else 's'} "
                                   "around the window, and needs two from one startup")
            return "vdollar", f"{no_pack}, and {found}", None
        why = "oracle_source = statspack" if want == "statspack" else f"{no_pack}; auto found two snapshots around the window"
        return "statspack", why, (snaps, spans, local)

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
            "top_sql": [_sql_row(r, *SQL_STATS) for r in sql],
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
        # AWR timestamps are naive, in the database server's clock; shift the UTC window to that clock.
        local = _db_clock(cur)

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
            "top_sql": [_sql_row(r, *SQL_STATS) for r in sql],
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

    # ------------------------------------------------------------------ Statspack
    def _statspack(self, cur, window: Window, dbr, inst, why: str, snaps: list[dict], spans_restart: bool, local):
        first, last = snaps[0], snaps[-1]
        b, e = first["snap_id"], last["snap_id"]
        begin_t, end_t = first["snap_time"], last["snap_time"]
        elapsed_s = (end_t - begin_t).total_seconds()
        level = min(_level(first), _level(last))  # a statement or plan is in both only if both snapshots captured it
        binds = {"dbid": dbr["dbid"], "inst": inst["instance_number"], "b": b, "e": e}
        notes = [f"Source: Statspack snapshots {b}-{e} at level {level} ({why})."]
        if spans_restart:
            notes.append("The window spans an instance restart; Statspack's counters start over at one, so this covers "
                         "the time since the restart.")
        if end_t < local(window.start) or begin_t > local(window.end):
            notes.append(f"No two Statspack snapshots from one startup lie around the window, so this covers the newest "
                         f"two, {begin_t:%Y-%m-%d %H:%M} to {end_t:%Y-%m-%d %H:%M} on the database's clock.")
        sql, sql_notes = self._statspack_sql(cur, binds, level)
        notes += sql_notes

        events = _rows(cur, """
            select nvl(n.wait_class, 'Other') as wait_class, e.event,
                   e.total_waits - nvl(p.total_waits, 0) as total_waits,
                   e.time_waited_micro - nvl(p.time_waited_micro, 0) as time_waited_micro
            from perfstat.stats$system_event e
            left join perfstat.stats$system_event p
              on p.snap_id = :b and p.dbid = e.dbid and p.instance_number = e.instance_number and p.event = e.event
            left join (select name, max(wait_class) as wait_class from v$event_name group by name) n on n.name = e.event
            where e.snap_id = :e and e.dbid = :dbid and e.instance_number = :inst and nvl(n.wait_class, 'Other') <> 'Idle'
            order by time_waited_micro desc""", **binds)
        stats = {r["name"]: num(r["v"]) for r in _rows(cur, """
            select e.name, e.value - nvl(p.value, 0) as v
            from perfstat.stats$sysstat e left join perfstat.stats$sysstat p
              on p.snap_id = :b and p.dbid = e.dbid and p.instance_number = e.instance_number and p.name = e.name
            where e.snap_id = :e and e.dbid = :dbid and e.instance_number = :inst
              and e.name in (%s)""" % ",".join(f"'{s}'" for s in SYSSTATS), **binds)}
        # STATS$SYS_TIME_MODEL keeps only stat_id; the names come from the instance, where they are fixed
        tm = {r["stat_name"]: num(r["v"]) for r in _rows(cur, """
            select n.stat_name, e.value - nvl(p.value, 0) as v
            from perfstat.stats$sys_time_model e left join perfstat.stats$sys_time_model p
              on p.snap_id = :b and p.dbid = e.dbid and p.instance_number = e.instance_number and p.stat_id = e.stat_id
            join (select distinct stat_id, stat_name from v$sys_time_model) n on n.stat_id = e.stat_id
            where e.snap_id = :e and e.dbid = :dbid and e.instance_number = :inst
              and n.stat_name in (%s)""" % ",".join(f"'{s}'" for s in TIME_MODEL), **binds)}
        n_sess = _rows(cur, "select count(*) as n from v$session where type = 'USER'")[0]["n"]

        info = {"begin_snap": b, "end_snap": e, "begin": begin_t.isoformat(), "end": end_t.isoformat(),
                "elapsed_s": elapsed_s, "startup": last["startup_time"].isoformat(), "level": level,
                **({"note": "the window spans an instance restart; Statspack's counters start over at one, so this "
                            "covers the time since the restart"} if spans_restart else {})}
        load = _load_profile(stats, tm, n_sess, elapsed_s)
        top = [_sql_row(r, *SQL_STATS) for r in sql]
        waits = [{"class": r["wait_class"], "event": r["event"], "count": num(r["total_waits"]),
                  "time_ms": round((num(r["time_waited_micro"]) or 0) / 1000, 1)} for r in events[:40]]
        summary = {
            "metrics_kind": "interval",
            "statspack": info,
            "counters_since": begin_t.isoformat(),
            "load_profile": load,
            "top_sql": top,
            "wait_events": waits,
            "tables": self._stale_tables(cur),
            "findings": [],
            "notes": notes,
        }
        reports = {"statspack.txt": _statspack_text(f"{dbr['name']}/{dbr['con']}", inst, info, load, waits, top, notes)}
        if self.target.extra.get("mask_report_literals", True):
            reports = {name: mask_report_literals(text) for name, text in reports.items()}
        raw = {"snapshots": snaps, "stats$sql_summary": [dict(r, sql_text=redact_sql(_clob(r.get("sql_text")))) for r in sql],
               "stats$system_event": events, "stats$sysstat": stats, "stats$sys_time_model": tm}
        return summary, raw, reports

    def _statspack_sql(self, cur, binds: dict, level: int) -> tuple[list[dict], list[str]]:
        """Top SQL for the interval: each cursor in STATS$SQL_SUMMARY at the end snapshot less the same cursor at the
        begin one, summed per sql_id. Statspack keeps cumulative figures for the statements over its thresholds."""
        if level < 5:
            return [], [f"Statspack level {level}: SQL is captured only at level 5 and above, so top SQL is left empty "
                        "rather than guessed."]
        q = f"""select old_hash_value, text_subset, address, sql_id, {", ".join(SQL_STATS)}
                from perfstat.stats$sql_summary
                where snap_id = :snap and dbid = :dbid and instance_number = :inst
                  and nvl(module, '-') not like 'dbtriage%'
                  and lower(nvl(sql_text, text_subset)) not like '%statspack.snap%'"""  # sql_text is empty on recent releases
        where = {"dbid": binds["dbid"], "inst": binds["inst"]}

        def key(r):  # one cursor: a reload puts it at another address, and its counters start over
            return r["old_hash_value"], r["text_subset"], r["address"]

        try:
            before = {key(r): r for r in _rows(cur, q, snap=binds["b"], **where)}
            after = _rows(cur, q, snap=binds["e"], **where)
        except oracledb.DatabaseError as exc:  # no SELECT on it: the waits and load profile still stand
            return [], [f"No top SQL: PERFSTAT.STATS$SQL_SUMMARY gives {_first_line(exc)}."]
        by_id: dict = {}
        for r in after:
            p = before.pop(key(r), None)
            d = by_id.setdefault(r["sql_id"], {"sql_id": r["sql_id"], "in_begin_snapshot": False, "text_subset": r["text_subset"],
                                               **dict.fromkeys(SQL_STATS, 0)})
            d["in_begin_snapshot"] = d["in_begin_snapshot"] or p is not None
            for c in SQL_STATS:
                now, then = num(r[c]) or 0, (num(p[c]) or 0) if p is not None else 0
                d[c] += now - then if now >= then else now  # lower than before: reloaded in place, all of it is new
        active = [d for d in by_id.values() if d["executions"] or d["elapsed_time"]]
        sql = sorted(active, key=lambda d: -d["elapsed_time"])[:self.target.top_n]
        notes = []
        if new := sum(1 for d in active if not d["in_begin_snapshot"]):
            notes.append(f"Statements not in the begin snapshot: {new} (new in the window, reloaded, or under Statspack's "
                         "thresholds then); their figures count everything since they were loaded.")
        if before:
            notes.append(f"Statements in the begin snapshot but gone by the end: {len(before)} (aged out of the shared "
                         "pool); their work in the window is not counted.")
        if not sql:
            return sql, notes
        ids = {f"q{i}": d["sql_id"] for i, d in enumerate(sql)}
        marks = ", ".join(f":{k}" for k in ids)
        texts = self._statspack_texts(cur, marks, ids, notes)
        if missing := {k: v for k, v in ids.items() if v not in texts}:  # older releases keep it in SQL_SUMMARY itself
            more = ", ".join(f":{k}" for k in missing)
            texts.update({r["sql_id"]: r["sql_text"] for r in _rows(cur, f"""
                select sql_id, max(sql_text) as sql_text from perfstat.stats$sql_summary
                where snap_id = :e and dbid = :dbid and instance_number = :inst and sql_id in ({more})
                group by sql_id""", e=binds["e"], **where, **missing) if r["sql_text"]})
        if short := [d for d in sql if d["sql_id"] not in texts]:  # the first 31 characters are all Statspack kept here
            for d in short:
                texts[d["sql_id"]] = (d.get("text_subset") or "") + " …"
            notes.append(f"Statements shown by their first 31 characters only: {len(short)} (no full text in STATS$SQLTEXT "
                         "or STATS$SQL_SUMMARY).")
        plans: dict = {}
        if level >= 6:
            try:  # the newest plan recorded for each statement up to the end snapshot
                plans = {r["sql_id"]: r["plan_hash_value"] for r in _rows(cur, f"""
                    select sql_id, plan_hash_value from (
                      select sql_id, plan_hash_value,
                             row_number() over (partition by sql_id order by snap_id desc, last_active_time desc nulls last) as rn
                      from perfstat.stats$sql_plan_usage
                      where dbid = :dbid and instance_number = :inst and snap_id <= :e and sql_id in ({marks}))
                    where rn = 1""", e=binds["e"], **where, **ids)}
            except oracledb.DatabaseError as exc:  # no SELECT on it: top SQL still stands, without plans
                notes.append(f"No plan hashes: PERFSTAT.STATS$SQL_PLAN_USAGE gives {_first_line(exc)}.")
        else:
            notes.append(f"Statspack level {level}: plans are captured from level 6, so top SQL has no plan hash.")
        for d in sql:
            d["sql_text"], d["plan_hash_value"] = texts.get(d["sql_id"]), plans.get(d["sql_id"])
            d.pop("text_subset", None)
        # Statspack's own snapshot call: the SQL filter above sees only 31 characters, which may stop before ".snap"
        sql = [d for d in sql if "statspack.snap" not in (d["sql_text"] or "").lower()]
        return sql, notes

    @staticmethod
    def _statspack_texts(cur, marks: str, ids: dict, notes: list[str]) -> dict[str, str]:
        """Full statement text from STATS$SQLTEXT, which keeps it once per statement in 64-character pieces (recent
        releases leave STATS$SQL_SUMMARY.sql_text empty). Without SELECT on it, the caller falls back and says so."""
        try:
            rows = _rows(cur, f"""select sql_id, old_hash_value, piece, sql_text from perfstat.stats$sqltext
                                  where sql_id in ({marks}) order by sql_id, old_hash_value, piece""", **ids)
        except oracledb.DatabaseError as exc:
            notes.append(f"No full SQL text: PERFSTAT.STATS$SQLTEXT gives {_first_line(exc)}.")
            return {}
        pieces: dict = {}
        for r in rows:  # one statement can have several old_hash_values (versions); the first is enough for its text
            pieces.setdefault(r["sql_id"], {}).setdefault(r["old_hash_value"], []).append(r["sql_text"] or "")
        return {sql_id: "".join(next(iter(by_hash.values()))) for sql_id, by_hash in pieces.items()}


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


# (label, load_profile key, divisor) for statspack.txt
LOAD_LINES = (
    ("DB time (s)", "db_time_ms", 1000), ("DB CPU (s)", "db_cpu_ms", 1000), ("User calls", "user_calls", 1),
    ("Executions", "executions", 1), ("Parses", "parses", 1), ("Hard parses", "hard_parses", 1),
    ("Commits", "xact_commit", 1), ("Rollbacks", "xact_rollback", 1), ("Logical reads (blocks)", "blks_hit", 1),
    ("Physical reads (blocks)", "blks_read", 1), ("Physical writes (blocks)", "blks_written", 1),
    ("Redo size (bytes)", "redo_bytes", 1), ("Sorts to disk", "sorts_disk", 1),
    ("Long table scans", "long_table_scans", 1), ("Logons", "logons", 1),
)


def _statspack_text(db: str, inst: dict, info: dict, lp: dict, waits: list[dict], top: list[dict], notes: list[str]) -> str:
    """A short plain-text report from the numbers read, for the reviewer and the snapshot page: spreport.sql, Statspack's
    own report, runs only in SQL*Plus. SQL text is the redacted text the summary keeps."""
    el = info["elapsed_s"] or 0
    L = [f"Statspack summary: {db}, instance {inst['instance_name']}, {inst['version_full']}",
         "Built by Second Look from the PERFSTAT.STATS$ tables; spreport.sql gives Statspack's full report in SQL*Plus.",
         "",
         f"Snapshots   {info['begin_snap']} to {info['end_snap']}, level {info['level']}",
         f"Interval    {info['begin']} to {info['end']} on the database's clock ({el / 60:.1f} min)",
         f"Started     {info['startup']}",
         "", "Load profile", f"{'':<26}{'Total':>18}{'Per second':>14}"]
    for label, key, div in LOAD_LINES:
        if lp.get(key) is None:
            continue
        v = lp[key] / div
        L.append(f"{label:<26}{v:>18,.1f}{(v / el if el else 0):>14,.2f}")
    if lp.get("avg_active_sessions") is not None:
        L.append(f"{'Average active sessions':<26}{lp['avg_active_sessions']:>18,.2f}")
    if lp.get("cache_hit_ratio") is not None:
        L.append(f"{'Buffer cache hit ratio':<26}{lp['cache_hit_ratio'] * 100:>17,.2f}%")

    db_time_s = (lp.get("db_time_ms") or 0) / 1000
    L += ["", "Top wait events (idle left out)",
          f"{'Event':<40}{'Class':<16}{'Waits':>12}{'Time (s)':>12}{'Avg (ms)':>10}{'% DB time':>11}"]
    for w in waits[:10]:
        n, t_ms = w["count"] or 0, w["time_ms"] or 0
        L.append(f"{str(w['event'])[:39]:<40}{str(w['class'])[:15]:<16}{n:>12,}{t_ms / 1000:>12,.1f}"
                 f"{(t_ms / n if n else 0):>10,.2f}{(100 * t_ms / 1000 / db_time_s if db_time_s else 0):>10,.1f}%")
    if not waits:
        L.append("(none)")

    L += ["", "Top SQL by elapsed time",
          f"{'SQL id':<15}{'Plan hash':>12}{'Execs':>10}{'Elapsed (s)':>13}{'Per exec (ms)':>15}{'CPU (s)':>10}"
          f"{'Gets':>14}{'Reads':>12}{'Rows':>12}"]
    for q in top[:10]:
        L.append(f"{str(q['sql_id']):<15}{str(q['plan_hash'] or '-'):>12}{q['calls']:>10,}{q['total_time_ms'] / 1000:>13,.2f}"
                 f"{(q['mean_time_ms'] or 0):>15,.2f}{q['cpu_time_ms'] / 1000:>10,.2f}"
                 f"{(q['io_blocks_hit'] or 0):>14,}{(q['io_blocks_read'] or 0):>12,}{(q['rows'] or 0):>12,}")
        L.append(f"    {' '.join((q['text'] or '').split())[:110]}")
    if not top:
        L.append("(none)")
    if notes:
        L += ["", "Notes", *(f"- {n}" for n in notes)]
    return "\n".join(L) + "\n"
