"""Statspack as an Oracle source: which source auto picks and why, the deltas between two STATS$ snapshots, the
level and the statements only one snapshot has, statspack.txt, and the taker. Fake cursors stand in for the
database; the snapshot picking is answered the way the SQL would, so the window rules are tested, not the fake."""
from datetime import datetime, timedelta, timezone

import oracledb
import pytest

from dbtriage import dbsnap
from dbtriage.collectors import oracle
from dbtriage.collectors.base import Window
from dbtriage.config import Target

A, B = datetime(2026, 11, 6, 9, 0), datetime(2026, 11, 7, 10, 20)  # instance startups, on the database's clock (UTC here)
START, END = datetime(2026, 11, 7, 10, 5, tzinfo=timezone.utc), datetime(2026, 11, 7, 11, 5, tzinfo=timezone.utc)
ON = {"allow_db_writes": True}
GONE = oracledb.DatabaseError("ORA-00942: table or view does not exist")


def at(hh, mm=0):
    return datetime(2026, 11, 7, hh, mm)


def snap(i, t, startup=A, level=7):
    return {"snap_id": i, "snap_time": t, "startup_time": startup, "snap_level": level}


def cursor_row(sql_id, old_hash, execs, elapsed_us, address=b"\x01", **more):
    return {"old_hash_value": old_hash, "text_subset": sql_id, "address": address, "sql_id": sql_id,
            "executions": execs, "elapsed_time": elapsed_us, "cpu_time": elapsed_us // 2, "buffer_gets": execs * 10,
            "disk_reads": execs // 10, "rows_processed": execs, "direct_writes": 0, **more}


class DB:
    """What the fake database holds, and every statement it was sent."""

    def __init__(self, snaps=(), statspack=True, pack_access="NONE", summary=None, texts=None, plans=(), age=60.0, denied=(), pieces=None):
        self.pieces = pieces or {}  # STATS$SQLTEXT: sql_id -> [(old_hash_value, piece, text)]
        self.snaps, self.statspack, self.pack_access = list(snaps), statspack, pack_access
        self.summary, self.texts, self.plans, self.age, self.denied = summary or {}, texts or {}, list(plans), age, denied
        self.sql, self.called = [], []

    def answer(self, sql: str, b: dict) -> list[dict]:
        if ("perfstat." in sql and not self.statspack) or any(f"perfstat.{t}" in sql for t in self.denied):
            raise GONE  # what Oracle says for a table that is not there and for one without a SELECT grant
        if "stats$snapshot" in sql:
            if "age_min" in sql:
                return [{"age_min": self.age}]
            if "rownum <= 2" in sql:  # the two newest of the latest startup, newest first
                latest = max((s["startup_time"] for s in self.snaps), default=None)
                return sorted((s for s in self.snaps if s["startup_time"] == latest), key=lambda s: -s["snap_id"])[:2]
            s, e = b["s"], b["e"]  # at or before the start, inside, at or after the end
            lo = max((x["snap_time"] for x in self.snaps if x["snap_time"] <= s), default=s)
            hi = min((x["snap_time"] for x in self.snaps if x["snap_time"] >= e), default=e)
            return [x for x in self.snaps if lo <= x["snap_time"] <= hi]
        if "stats$sqltext" in sql:
            ids = {v for k, v in b.items() if k.startswith("q")}
            return [{"sql_id": i, "old_hash_value": h, "piece": n, "sql_text": t}
                    for i, rows in sorted(self.pieces.items()) if i in ids for h, n, t in sorted(rows)]
        if "max(sql_text)" in sql:
            ids = {v for k, v in b.items() if k.startswith("q")}
            return [{"sql_id": i, "sql_text": t} for i, t in self.texts.items() if i in ids]
        if "stats$sql_summary" in sql:
            return self.summary.get(b["snap"], [])
        if "stats$sql_plan_usage" in sql:
            ids, newest = {v for k, v in b.items() if k.startswith("q")}, {}
            for p in sorted(self.plans, key=lambda p: p["snap_id"]):
                if p["sql_id"] in ids and p["snap_id"] <= b["e"]:
                    newest[p["sql_id"]] = p
            return [{"sql_id": i, "plan_hash_value": p["plan_hash_value"]} for i, p in newest.items()]
        if "stats$system_event" in sql:
            return [{"wait_class": "User I/O", "event": "db file sequential read", "total_waits": 1000, "time_waited_micro": 2_000_000}]
        if "stats$sysstat" in sql:
            return [{"name": "user commits", "v": 7200}, {"name": "session logical reads", "v": 100_000}, {"name": "physical reads", "v": 1000}]
        if "stats$sys_time_model" in sql:
            return [{"stat_name": "DB time", "v": 1_800_000_000}, {"stat_name": "DB CPU", "v": 900_000_000}]
        if "dba_hist_snapshot" in sql and "age_min" in sql:
            return [{"age_min": self.age}]
        if "dba_hist_snapshot" in sql:
            return [{"snap_id": 41, "begin_interval_time": at(10), "end_interval_time": at(11), "startup_time": A},
                    {"snap_id": 42, "begin_interval_time": at(11), "end_interval_time": at(12), "startup_time": A}]
        if "awr_report_text" in sql:
            return [{"output": "WORKLOAD REPOSITORY report"}]
        if "from v$instance" in sql:
            return [{"instance_number": 1, "instance_name": "ORCL", "version_full": "19.21.0.0.0", "startup_time": A}]
        if "from v$database" in sql:
            return [{"dbid": 42, "name": "ORCL", "con": "PDB1"}]
        if "from v$parameter" in sql:
            return [{"name": "control_management_pack_access", "value": self.pack_access}]
        if "sys_extract_utc" in sql:
            return [{"off": 0}]
        if "count(*) as n from v$session" in sql:
            return [{"n": 12}]
        return []  # sessions, V$ figures, stale tables, AWR's other views: nothing to say here


class Cur:
    def __init__(self, db):
        self.db, self.description, self.rows = db, [], []

    def setinputsizes(self, **kw):
        pass

    def execute(self, sql, binds=None, **kw):
        flat = " ".join(sql.split()).lower()
        self.db.sql.append(flat)
        rows = self.db.answer(flat, {**(binds or {}), **kw})
        cols = list(rows[0]) if rows else []
        self.description, self.rows = [(c.upper(),) for c in cols], [tuple(r[c] for c in cols) for r in rows]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def callfunc(self, name, typ, keyword_parameters=None):
        self.db.called.append((name, keyword_parameters))
        return 1234


class Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return Cur(self.db)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def tgt(pack=False, **extra):
    return Target("ora", "oracle", "dsn", top_n=10, diagnostics_pack=pack, extra=extra)


def collect(monkeypatch, db, target=None, start=START, end=END):
    monkeypatch.setattr(oracle, "connect", lambda t: Conn(db))
    return oracle.OracleCollector(target or tgt()).collect(Window(start=start, end=end, mode="scheduled"))


def touched_dba_hist(db) -> list[str]:
    return [s for s in db.sql if "dba_hist" in s or "awr_report" in s or "dba_addm" in s]


PAIR = [snap(100, at(9)), snap(101, at(10)), snap(102, at(11))]  # hourly, the window 10:05-11:05


# --------------------------------------------------------------------------- which source, and why
def test_auto_reads_awr_when_the_pack_is_on_and_never_asks_statspack(monkeypatch):
    db = DB(PAIR, pack_access="DIAGNOSTIC+TUNING")
    s = collect(monkeypatch, db, tgt(pack=True)).summary
    assert s["oracle_source"] == "awr" and s["awr"]["end_snap"] == 42 and s["metrics_kind"] == "interval"
    assert s["notes"] == ["Source: AWR snapshots 41-42 (diagnostics_pack = true)."]
    assert not [q for q in db.sql if "perfstat" in q]


def test_auto_reads_statspack_when_a_pair_from_one_startup_bounds_the_window(monkeypatch):
    db = DB(PAIR)
    snap_ = collect(monkeypatch, db)
    s = snap_.summary
    assert s["oracle_source"] == "statspack" and s["metrics_kind"] == "interval"
    assert {k: s["statspack"][k] for k in ("begin_snap", "end_snap", "elapsed_s", "level")} == \
        {"begin_snap": 101, "end_snap": 102, "elapsed_s": 3600, "level": 7}
    assert s["counters_since"] == at(10).isoformat() and "note" not in s["statspack"]
    assert s["notes"][0] == ("Source: Statspack snapshots 101-102 at level 7 (no Diagnostics Pack for this target; "
                             "auto found two snapshots around the window).")
    assert set(snap_.reports) == {"statspack.txt"} and not touched_dba_hist(db)


def test_auto_falls_back_to_v_dollar_when_statspack_cannot_be_read(monkeypatch):
    db = DB(statspack=False)
    s = collect(monkeypatch, db).summary
    assert s["oracle_source"] == "vdollar" and s["metrics_kind"] == "cumulative"
    assert s["notes"] == ["Source: V$ views only, cumulative since the instance started (no Diagnostics Pack for this "
                          "target, and Statspack cannot be read: PERFSTAT.STATS$SNAPSHOT gives ORA-00942: table or view "
                          "does not exist); no AWR, ADDM or ASH."]
    assert not touched_dba_hist(db)


def test_auto_falls_back_with_only_one_snapshot_around_the_window(monkeypatch):
    s = collect(monkeypatch, DB([snap(101, at(10))])).summary
    assert s["oracle_source"] == "vdollar"
    assert "Statspack has 1 snapshot around the window, and needs two from one startup" in s["notes"][0]
    s = collect(monkeypatch, DB([])).summary
    assert s["oracle_source"] == "vdollar" and "Statspack has no snapshots around the window" in s["notes"][0]
    # the newest pair lies before the window: auto does not stretch to it (an explicit statspack does, below)
    s = collect(monkeypatch, DB([snap(98, at(8)), snap(99, at(9))]), start=START + timedelta(hours=1), end=END + timedelta(hours=1)).summary
    assert s["oracle_source"] == "vdollar"


def test_a_restart_between_the_snapshots_is_not_a_pair(monkeypatch):
    s = collect(monkeypatch, DB([snap(101, at(10)), snap(102, at(11), startup=B)])).summary
    assert s["oracle_source"] == "vdollar"
    assert "the Statspack snapshots around the window span an instance restart, with fewer than two after it" in s["notes"][0]
    # two after the restart: reported from the restart on, and said so
    db = DB([snap(101, at(10)), snap(102, at(10, 30), startup=B), snap(103, at(11), startup=B)])
    s = collect(monkeypatch, db).summary
    assert s["oracle_source"] == "statspack" and (s["statspack"]["begin_snap"], s["statspack"]["end_snap"]) == (102, 103)
    assert "spans an instance restart" in s["statspack"]["note"] and any("spans an instance restart" in n for n in s["notes"])
    assert s["statspack"]["startup"] == B.isoformat()


def test_explicit_sources(monkeypatch):
    with pytest.raises(SystemExit, match="oracle_source = statspack, but PERFSTAT.STATS\\$SNAPSHOT cannot be read"):
        collect(monkeypatch, DB(statspack=False), tgt(oracle_source="statspack"))
    # nothing around the window: the newest pair of the latest startup, like AWR, with a note
    s = collect(monkeypatch, DB([snap(98, at(8)), snap(99, at(9))]), tgt(oracle_source="statspack"),
                start=START + timedelta(hours=1), end=END + timedelta(hours=1)).summary
    assert s["oracle_source"] == "statspack" and (s["statspack"]["begin_snap"], s["statspack"]["end_snap"]) == (98, 99)
    assert any(n.startswith("No two Statspack snapshots from one startup lie around the window") for n in s["notes"])
    with pytest.raises(SystemExit, match="Statspack has fewer than two snapshots since the instance last started"):
        collect(monkeypatch, DB([snap(101, at(10)), snap(102, at(11), startup=B)]), tgt(oracle_source="statspack"))
    # statspack wins over the pack when asked for; vdollar reads neither
    s = collect(monkeypatch, DB(PAIR, pack_access="DIAGNOSTIC"), tgt(pack=True, oracle_source="statspack")).summary
    assert s["oracle_source"] == "statspack" and s["notes"][0].endswith("(oracle_source = statspack).")
    db = DB(PAIR, pack_access="DIAGNOSTIC")
    s = collect(monkeypatch, db, tgt(pack=True, oracle_source="vdollar")).summary
    assert s["oracle_source"] == "vdollar" and not touched_dba_hist(db) and not [q for q in db.sql if "perfstat" in q]
    with pytest.raises(SystemExit, match="oracle_source: one of auto, awr, statspack, vdollar"):
        collect(monkeypatch, DB(PAIR), tgt(oracle_source="ash"))


@pytest.mark.parametrize("source", ["auto", "awr", "statspack", "vdollar"])
def test_without_the_pack_dba_hist_is_never_touched(monkeypatch, source):
    """diagnostics_pack = false means no DBA_HIST, whatever oracle_source says and whatever the database allows."""
    db = DB(PAIR, pack_access="DIAGNOSTIC+TUNING")
    s = collect(monkeypatch, db, tgt(oracle_source=source)).summary
    assert not touched_dba_hist(db), touched_dba_hist(db)
    assert s["oracle_source"] == {"auto": "statspack", "awr": "vdollar", "statspack": "statspack", "vdollar": "vdollar"}[source]
    if source == "awr":
        assert "oracle_source = awr, but no Diagnostics Pack for this target" in s["notes"][0]


def test_the_pack_on_but_not_allowed_by_the_database_tries_statspack(monkeypatch):
    db = DB(PAIR, pack_access="NONE")
    s = collect(monkeypatch, db, tgt(pack=True)).summary
    assert s["oracle_source"] == "statspack" and "control_management_pack_access does not include DIAGNOSTIC" in s["notes"][0]
    assert not touched_dba_hist(db)


# --------------------------------------------------------------------------- what is read
SUMMARY = {
    101: [cursor_row("a1", 1, 100, 1_000_000), cursor_row("c3", 3, 500, 2_000_000),     # c3 ages out
          cursor_row("d4", 4, 70, 700_000), cursor_row("e5", 5, 1000, 5_000_000)],
    102: [cursor_row("a1", 1, 400, 4_000_000), cursor_row("b2", 2, 50, 9_000_000),      # b2 is new
          cursor_row("d4", 4, 70, 700_000),                                             # d4 did nothing
          cursor_row("e5", 5, 20, 200_000)],                                            # e5 was reloaded in place
}
TEXTS = {"a1": "select * from app.orders where id = 42", "b2": "select * from app.orders where note = 'secret'",
         "e5": "update app.orders set status = 'paid'"}


def test_deltas_and_rates(monkeypatch):
    from dbtriage import compare

    db = DB(PAIR, summary=SUMMARY, texts=TEXTS)
    s = collect(monkeypatch, db).summary
    top = {q["sql_id"]: q for q in s["top_sql"]}
    assert list(top) == ["b2", "a1", "e5"]  # by elapsed time in the window; d4 had none, c3 is gone
    a = top["a1"]
    assert (a["calls"], a["total_time_ms"], a["mean_time_ms"], a["cpu_time_ms"], a["io_blocks_hit"], a["io_blocks_read"], a["rows"]) == \
        (300, 3000, 10, 1500, 3000, 30, 300)
    assert (top["b2"]["calls"], top["b2"]["total_time_ms"]) == (50, 9000)   # new: all of it
    assert (top["e5"]["calls"], top["e5"]["total_time_ms"]) == (20, 200)    # reloaded: counters started over
    assert top["b2"]["text"] == "select * from app.orders where note = '?'" and top["a1"]["text"].endswith("id = ?")
    lp = s["load_profile"]
    assert lp["elapsed_s"] == 3600 and lp["xact_commit"] == 7200 and lp["db_time_ms"] == 1_800_000 and lp["avg_active_sessions"] == 0.5
    assert lp["cache_hit_ratio"] == 0.99
    assert s["wait_events"] == [{"class": "User I/O", "event": "db file sequential read", "count": 1000, "time_ms": 2000}]
    # the rest of the pipeline reads it as an interval, rates per second over the snapshots' own elapsed time
    out = compare.compare(dict(s, id="oracle/ora/x"), None)
    assert out["current"]["elapsed_s"] == 3600 and out["load_profile"]["xact_commit"]["current_per_s"] == 2


def test_statements_in_only_one_snapshot_are_said(monkeypatch):
    snap_ = collect(monkeypatch, DB(PAIR, summary=SUMMARY, texts=TEXTS))
    notes = snap_.summary["notes"]
    assert "Statements not in the begin snapshot: 1 (new in the window, reloaded, or under Statspack's thresholds then); " \
           "their figures count everything since they were loaded." in notes
    assert "Statements in the begin snapshot but gone by the end: 1 (aged out of the shared pool); their work in the " \
           "window is not counted." in notes
    raw = {r["sql_id"]: r for r in snap_.raw["stats$sql_summary"]}
    assert raw["b2"]["in_begin_snapshot"] is False and raw["a1"]["in_begin_snapshot"] is True
    assert raw["b2"]["sql_text"].endswith("'?'")  # raw keeps no literals either


def test_plan_hash_from_plan_usage(monkeypatch):
    plans = [{"sql_id": "a1", "plan_hash_value": 111, "snap_id": 90}, {"sql_id": "a1", "plan_hash_value": 222, "snap_id": 102},
             {"sql_id": "b2", "plan_hash_value": 333, "snap_id": 101}, {"sql_id": "b2", "plan_hash_value": 444, "snap_id": 103}]
    db = DB(PAIR, summary=SUMMARY, texts=TEXTS, plans=plans)
    top = {q["sql_id"]: q for q in collect(monkeypatch, db).summary["top_sql"]}
    assert (top["a1"]["plan_hash"], top["b2"]["plan_hash"], top["e5"]["plan_hash"]) == (222, 333, None)  # newest up to the end


def test_a_missing_grant_costs_its_part_only(monkeypatch):
    s = collect(monkeypatch, DB(PAIR, summary=SUMMARY, texts=TEXTS, denied=("stats$sql_plan_usage",))).summary
    assert [q["sql_id"] for q in s["top_sql"]] == ["b2", "a1", "e5"] and all(q["plan_hash"] is None for q in s["top_sql"])
    assert "No plan hashes: PERFSTAT.STATS$SQL_PLAN_USAGE gives ORA-00942: table or view does not exist." in s["notes"]
    s = collect(monkeypatch, DB(PAIR, summary=SUMMARY, texts=TEXTS, denied=("stats$sql_summary",))).summary
    assert s["oracle_source"] == "statspack" and s["top_sql"] == [] and s["load_profile"]["xact_commit"] == 7200
    assert "No top SQL: PERFSTAT.STATS$SQL_SUMMARY gives ORA-00942: table or view does not exist." in s["notes"]


def test_levels(monkeypatch):
    for levels in ((0, 0), (7, 0)):  # the lower of the two decides: a statement must be in both
        db = DB([snap(101, at(10), level=levels[0]), snap(102, at(11), level=levels[1])], summary=SUMMARY, texts=TEXTS)
        s = collect(monkeypatch, db).summary
        assert s["top_sql"] == [] and s["statspack"]["level"] == 0
        assert "Statspack level 0: SQL is captured only at level 5 and above, so top SQL is left empty rather than guessed." in s["notes"]
        assert not [q for q in db.sql if "stats$sql_summary" in q] and s["load_profile"]["xact_commit"] == 7200
    db = DB([snap(101, at(10), level=5), snap(102, at(11), level=5)], summary=SUMMARY, texts=TEXTS, plans=[
        {"sql_id": "a1", "plan_hash_value": 222, "snap_id": 102}])
    s = collect(monkeypatch, db).summary
    assert [q["sql_id"] for q in s["top_sql"]] == ["b2", "a1", "e5"] and all(q["plan_hash"] is None for q in s["top_sql"])
    assert "Statspack level 5: plans are captured from level 6, so top SQL has no plan hash." in s["notes"]
    assert not [q for q in db.sql if "stats$sql_plan_usage" in q]


def test_statspack_txt(monkeypatch):
    text = collect(monkeypatch, DB(PAIR, summary=SUMMARY, texts=TEXTS)).reports["statspack.txt"]
    lines = text.splitlines()
    assert lines[0] == "Statspack summary: ORCL/PDB1, instance ORCL, 19.21.0.0.0"
    assert "Snapshots   101 to 102, level 7" in lines
    assert "Interval    2026-11-07T10:00:00 to 2026-11-07T11:00:00 on the database's clock (60.0 min)" in lines
    commits = next(ln for ln in lines if ln.startswith("Commits"))
    assert commits.split()[1:] == ["7,200.0", "2.00"]
    db_time = next(ln for ln in lines if ln.startswith("DB time (s)"))
    assert db_time.split()[3:] == ["1,800.0", "0.50"]
    wait = next(ln for ln in lines if ln.startswith("db file sequential read"))
    assert wait.split()[-6:] == ["User", "I/O", "1,000", "2.0", "2.00", "0.1%"]  # 2 s of 1,800 s DB time
    b2 = lines.index(next(ln for ln in lines if ln.startswith("b2 ")))
    assert lines[b2].split() == ["b2", "-", "50", "9.00", "180.00", "4.50", "500", "5", "50"]
    assert lines[b2 + 1].strip() == "select * from app.orders where note = '?'" and "secret" not in text
    assert "- Statements in the begin snapshot but gone by the end: 1 (aged out of the shared pool); their work in the " \
           "window is not counted." in lines


def test_the_band_reads_a_statspack_window_across_a_restart(tmp_path):
    from dbtriage.band import usual_band
    from dbtriage.storage import LocalStorage
    from tests.test_band import MONDAY, fill, mondays, sid_at

    st = LocalStorage(tmp_path)
    fill(st, engine="oracle", target="ora", kind="interval", restart_note={mondays(6)[0]})
    for sid in st.list_snapshots("oracle", "ora"):  # the same windows, read from Statspack
        s = st.get_json(f"{sid}/summary.json")
        st.put_json(f"{sid}/summary.json", {**{k: v for k, v in s.items() if k != "awr"}, "statspack": s["awr"]})
    band = usual_band(st, "oracle", "ora", sid_at("oracle", "ora", MONDAY))
    assert [s["week"] for s in band["skipped"]] == [1] and band["skipped"][0]["reason"] == "the Statspack window spans an instance restart"


# --------------------------------------------------------------------------- taking snapshots
def win(mode="incident"):
    return Window(start=END - timedelta(hours=1), end=END, mode=mode)


def test_the_taker_runs_snap_only_when_writes_are_allowed(monkeypatch):
    db = DB(PAIR)
    monkeypatch.setattr(oracle, "connect", lambda t: Conn(db))
    t = tgt(create_snapshot="incident")
    assert dbsnap.take(ON, t, win("scheduled")) is None and db.called == []  # not an incident run
    off = dbsnap.take({}, t, win())
    assert off["status"] == "skipped" and db.called == []  # writes off: nothing written
    assert dbsnap.take(ON, t, win()) == {"status": "taken", "detail": "Statspack snapshot 1234 (level 7)"}
    assert db.called == [("perfstat.statspack.snap", {"i_snap_level": 7})]
    db.called.clear()
    assert dbsnap.take(ON, tgt(create_snapshot="always", statspack_level=10), win("adhoc"))["detail"] == "Statspack snapshot 1234 (level 10)"
    assert db.called == [("perfstat.statspack.snap", {"i_snap_level": 10})]


def test_the_taker_respects_the_minimum_age(monkeypatch):
    for age, min_age, want in ((2.0, None, None), (2.0, 1, "Statspack snapshot 1234 (level 7)"), (None, None, "Statspack snapshot 1234 (level 7)"),
                               (30.0, 60, None)):
        db = DB(PAIR, age=age)
        monkeypatch.setattr(oracle, "connect", lambda t, db=db: Conn(db))
        extra = {} if min_age is None else {"min_snapshot_age_min": min_age}
        assert oracle.take_statspack_snapshot(tgt(**extra), win()) == want
        assert db.called == ([] if want is None else [("perfstat.statspack.snap", {"i_snap_level": 7})])
    # through dbsnap, the last database: its newest snapshot is 30 minutes old and 60 are asked for
    assert dbsnap.take(ON, tgt(create_snapshot="always", min_snapshot_age_min=60), win())["status"] == "not needed"


def test_which_taker_and_when_it_cannot(monkeypatch):
    db = DB(PAIR, pack_access="DIAGNOSTIC+TUNING")
    monkeypatch.setattr(oracle, "connect", lambda t: Conn(db))
    assert dbsnap.take(ON, tgt(pack=True, create_snapshot="always"), win())["detail"] == "AWR snapshot 1234"  # auto with the pack
    assert dbsnap.take(ON, tgt(pack=True, create_snapshot="always", oracle_source="statspack"), win())["detail"].startswith("Statspack")
    assert [c[0] for c in db.called] == ["dbms_workload_repository.create_snapshot", "perfstat.statspack.snap"]
    failed = dbsnap.take(ON, tgt(create_snapshot="always", oracle_source="vdollar"), win())
    assert failed["status"] == "failed" and "reads only V$ views" in failed["reason"]
    failed = dbsnap.take(ON, tgt(create_snapshot="always", statspack_level=3), win())
    assert failed["status"] == "failed" and "statspack_level: one of 0, 5, 6, 7, 10" in failed["reason"]
    failed = dbsnap.take(ON, tgt(create_snapshot="always", oracle_source="nope"), win())
    assert failed["status"] == "failed" and "oracle_source: one of" in failed["reason"]
    db.statspack = False
    failed = dbsnap.take(ON, tgt(create_snapshot="always"), win())
    assert failed["status"] == "failed" and "Statspack cannot be read here" in failed["reason"] and "ORA-00942" in failed["reason"]
    assert len(db.called) == 2


def test_a_plugin_named_oracle_still_replaces_both(monkeypatch):
    class EP:
        name, value = "oracle", "my_pkg.snap:take"

        def load(self):
            return lambda t, w: "my own snapshot"

    monkeypatch.setattr(dbsnap, "entry_points", lambda group: [EP()] if group == dbsnap.GROUP else [])
    assert dbsnap.take(ON, tgt(create_snapshot="always"), win()) == {"status": "taken", "detail": "my own snapshot"}


def test_full_text_comes_from_sqltext_in_pieces_then_summary_then_the_subset(monkeypatch):
    """Recent releases leave STATS$SQL_SUMMARY.sql_text empty and keep the text in STATS$SQLTEXT, 64 characters a piece."""
    pieces = {"a1": [(1, 1, " where id = 42"), (1, 0, "update app.orders set n = n + 1")]}  # out of order on purpose
    s = collect(monkeypatch, DB(PAIR, summary=SUMMARY, texts={"b2": "select * from app.orders where note = 'x'"}, pieces=pieces)).summary
    top = {q["sql_id"]: q for q in s["top_sql"]}
    assert top["a1"]["text"] == "update app.orders set n = n + ? where id = ?"  # pieces joined in order, then redacted
    assert top["b2"]["text"] == "select * from app.orders where note = '?'"     # not in SQLTEXT: STATS$SQL_SUMMARY's text
    assert top["e5"]["text"] == "e5 …"                                           # neither: the 31-character subset
    assert any(n.startswith("Statements shown by their first 31 characters only: 1") for n in s["notes"])
    s = collect(monkeypatch, DB(PAIR, summary=SUMMARY, texts=TEXTS, denied=("stats$sqltext",))).summary
    assert any(n.startswith("No full SQL text: PERFSTAT.STATS$SQLTEXT gives ORA-00942") for n in s["notes"])
    assert {q["sql_id"] for q in s["top_sql"]} == {"b2", "a1", "e5"}  # without the grant the statements still stand


def test_statspacks_own_snapshot_call_is_left_out_by_its_full_text(monkeypatch):
    """A snapshot job's call can be longer than the 31-character subset the SQL filter sees."""
    pieces = {"b2": [(1, 0, "begin :retval := perfstat.statspack.snap(i_snap_level => 7); end;")]}
    s = collect(monkeypatch, DB(PAIR, summary=SUMMARY, texts=TEXTS, pieces=pieces)).summary
    assert "b2" not in {q["sql_id"] for q in s["top_sql"]}
