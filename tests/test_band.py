"""The usual band: six weeks of hourly snapshots for a cumulative and an interval target, built in a LocalStorage,
then the band for one clock hour and what it says about a load."""
from datetime import datetime, timedelta, timezone

from dbtriage.band import position, sentence, usual_band, weeks_of
from dbtriage.storage import LocalStorage

MONDAY = datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc)  # a Monday, 02:00 UTC: the batch hour
# commits per second in the hour ending at Monday 02:00, by weeks before MONDAY: this week sits on the median of the past six
BATCH = {0: 550, 1: 460, 2: 520, 3: 580, 4: 640, 5: 700, 6: 400}


def rate(t: datetime) -> int:
    """Commits per second in the hour ending at t: a wide-ranging batch on Monday 02:00, a quiet 10-12/s otherwise."""
    days = (MONDAY.date() - t.date()).days
    if t.weekday() == 0 and t.hour == 2:
        return BATCH[days // 7]
    return 10 + days % 3


def sid_at(engine: str, target: str, t: datetime) -> str:
    return f"{engine}/{target}/{t:%Y-%m-%d}/{t:%Y%m%dT%H%M%SZ}"


def fill(store: LocalStorage, engine: str = "postgres", target: str = "pg", kind: str = "cumulative", weeks: int = 6,
         first: datetime | None = None, last: datetime | None = None, missing: set | None = None, reset_at: set | None = None,
         restart_note: set | None = None, startup_inside: set | None = None) -> None:
    """Hourly snapshots from `weeks` weeks (and two hours) before MONDAY to MONDAY, cumulative counters or AWR-style
    intervals. `missing` hours are not stored; `reset_at` hours carry a new counters_since (a reset inside that hour);
    `restart_note` and `startup_inside` mark AWR windows that span a restart."""
    first = first or MONDAY - timedelta(weeks=weeks, hours=2)
    last = last or MONDAY
    t, total = first, {"commits": 0, "ms": 0}
    while t <= last:
        r = rate(t)
        hour_commits = r * 3600
        total["commits"] += hour_commits
        total["ms"] += hour_commits * 2
        if t in (missing or set()):
            t += timedelta(hours=1)
            continue
        if kind == "cumulative":
            since = "2026-01-01T00:00:00+00:00" if t not in (reset_at or set()) else (t - timedelta(minutes=30)).isoformat()
            summary = {"engine": engine, "metrics_kind": "cumulative", "captured_at": t.isoformat(), "counters_since": since,
                       "load_profile": {"xact_commit": total["commits"], "blks_hit": total["commits"] * 100, "blks_read": total["commits"],
                                        "temp_bytes": 0, "deadlocks": 0, "sessions_active": 3},
                       "top_sql": [{"sql_id": "q1", "text": "q1", "calls": total["commits"], "total_time_ms": total["ms"], "rows": 1}],
                       "wait_events": [{"class": "IO", "event": "read", "count": total["commits"]},
                                       {"class": "Lock", "event": "relation", "count": total["commits"] // 10}]}
        else:
            begin = t - timedelta(hours=1)
            awr = {"begin": begin.isoformat(), "end": t.isoformat(), "elapsed_s": 3600, "startup": "2026-01-01T00:00:00+00:00"}
            if t in (restart_note or set()):
                awr["note"] = "the window spans an instance restart"
            if t in (startup_inside or set()):
                awr["startup"] = (t - timedelta(minutes=20)).isoformat()
            summary = {"engine": engine, "metrics_kind": "interval", "captured_at": t.isoformat(), "counters_since": begin.isoformat(), "awr": awr,
                       "load_profile": {"elapsed_s": 3600, "xact_commit": hour_commits, "db_time_ms": hour_commits * 2, "sessions_active": 3},
                       "top_sql": [{"sql_id": "q1", "text": "q1", "calls": hour_commits, "total_time_ms": hour_commits * 2, "mean_time_ms": 2}],
                       "wait_events": [{"class": "User I/O", "event": "db file sequential read", "time_ms": hour_commits},
                                       {"class": "Concurrency", "event": "latch", "time_ms": hour_commits // 10}]}
        sid = sid_at(engine, target, t)
        store.put_json(f"{sid}/summary.json", summary)
        store.put_json(f"{sid}/manifest.json", {"id": sid, "engine": engine, "target": target, "captured_at": t.isoformat(),
                                                 "metrics_kind": kind, "reports": []})
        t += timedelta(hours=1)


def mondays(n: int, hour: int = 2) -> list[datetime]:
    return [MONDAY.replace(hour=hour) - timedelta(weeks=w) for w in range(1, n + 1)]


# --------------------------------------------------------------------------- the band itself
def test_band_is_built_from_the_same_hour_only(tmp_path):
    st = LocalStorage(tmp_path)
    fill(st)
    band = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))
    assert band["weeks_used"] == 6 and band["weeks"] == 6 and band["skipped"] == [] and band["high_is"] == "p90"
    assert band["label"] == "Monday 02:00 UTC" and band["window_s"] == 3600
    ends = [datetime.fromisoformat(w["end"]) for w in band["windows"]]
    assert ends == mondays(6)  # one window per past Monday, ending at 02:00, each a consecutive pair
    assert all(datetime.fromisoformat(w["start"]) == e - timedelta(hours=1) for w, e in zip(band["windows"], ends))
    m = band["metrics"]["xact_commit"]
    assert m["n"] == 6 and m["median"] == 550 and m["min"] == 400 and m["max"] == 700
    assert m["low"] == 430 and m["high"] == 670  # 10th and 90th percentile, interpolated
    assert band["metrics"]["sql_time_ms"]["median"] == 1100  # 2 ms of SQL time per commit
    assert band["metrics"]["wait_share:Lock"]["median"] == 0.091  # a share, not a rate
    assert "elapsed_s" not in band["metrics"] and band["metrics"]["sessions_active"]["median"] == 3  # a gauge stays a level


def test_week_with_a_counter_reset_or_no_snapshot_is_skipped_and_named(tmp_path):
    st = LocalStorage(tmp_path)
    reset, gone = mondays(6)[2], mondays(6)[4]
    fill(st, reset_at={reset}, missing={gone})
    band = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))
    assert band["weeks_used"] == 4 and band["high_is"] == "max"  # below five weeks the high mark is the max
    assert [s["week"] for s in band["skipped"]] == [3, 5]
    assert "counter reset inside the window" in band["skipped"][0]["reason"] and band["skipped"][0]["snapshot"] == sid_at("postgres", "pg", reset)
    assert band["skipped"][1]["reason"] == "the window 00:00-01:00 does not cover the same hour (01:00-02:00)"  # the neighbour is not it
    empty = LocalStorage(tmp_path / "sparse")
    fill(empty, missing={gone - timedelta(hours=1), gone, gone + timedelta(hours=1)})
    gaps = usual_band(empty, "postgres", "pg", sid_at("postgres", "pg", MONDAY))["skipped"]
    assert len(gaps) == 1 and gaps[0]["reason"].startswith("no snapshot within ±1h of")
    m = band["metrics"]["xact_commit"]
    assert (m["min"], m["max"], m["median"]) == (400, 640, 490) and m["high"] == 640 and m["low"] == 400  # weeks 1, 2, 4, 6


def test_fewer_than_three_weeks_gives_no_band(tmp_path):
    st = LocalStorage(tmp_path)
    fill(st, weeks=2)
    assert usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY)) is None
    st2 = LocalStorage(tmp_path / "b")
    fill(st2, weeks=6, missing=set(mondays(6)[:4]))  # six weeks of history, but only two usable Monday windows
    assert usual_band(st2, "postgres", "pg", sid_at("postgres", "pg", MONDAY)) is None
    assert usual_band(st2, "postgres", "pg", sid_at("postgres", "pg", MONDAY), weeks=0) is None  # switched off
    assert usual_band(st2, "postgres", "pg", "postgres/pg/nope") is None
    assert weeks_of(None) == 6 and weeks_of("") == 6 and weeks_of(0) == 0 and weeks_of("8") == 8


def test_interval_engine_takes_one_snapshot_per_week_and_skips_restarts(tmp_path):
    st = LocalStorage(tmp_path)
    noted, startup = mondays(6)[0], mondays(6)[3]
    fill(st, engine="oracle", target="ora", kind="interval", restart_note={noted}, startup_inside={startup})
    band = usual_band(st, "oracle", "ora", sid_at("oracle", "ora", MONDAY))
    assert band["weeks_used"] == 4 and [s["week"] for s in band["skipped"]] == [1, 4]
    assert band["skipped"][0]["reason"] == "the AWR window spans an instance restart"
    assert band["skipped"][1]["reason"].startswith("instance startup at ") and "inside the window" in band["skipped"][1]["reason"]
    assert all("baseline" not in w for w in band["windows"])  # the snapshot is the window
    assert band["metrics"]["xact_commit"]["median"] == (520 + 580) / 2 and band["metrics"]["db_time_ms"]["max"] == 1400


# --------------------------------------------------------------------------- what it says
def test_sentences_inside_above_all_above_and_below(tmp_path):
    st = LocalStorage(tmp_path)
    fill(st)
    band = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))
    assert sentence(band, "xact_commit", 550, "commits", "/s") == \
        "commits 550/s is inside the usual Monday 02:00 UTC band (6 weeks): median 550/s, high 670/s"
    assert sentence(band, "xact_commit", 900, "commits", "/s").startswith("commits 900/s is above every same-hour window in 6 weeks")
    assert "is above the usual Monday 02:00 UTC band (6 weeks)" in sentence(band, "xact_commit", 690)  # over p90, under the max
    assert "is below the usual Monday 02:00 UTC band (6 weeks)" in sentence(band, "xact_commit", 300)
    assert sentence(band, "wait_share:Lock", 0.3, "wait class 'Lock'", percent=True) == \
        "wait class 'Lock' 30% is above every same-hour window in 6 weeks: median 9.1%, high 9.1%"
    assert sentence(band, "nothing_like_it", 1) is None and sentence(band, "xact_commit", None) is None
    assert [position(band, "xact_commit", v) for v in (550, 900, 690, 300)] == ["inside", "above_all", "above", "below"]
    band["weeks_used"] = 4
    assert "(4 of 6 weeks)" in sentence(band, "xact_commit", 550) and "in 4 of 6 weeks" in sentence(band, "xact_commit", 900)


def test_monday_batch_reads_differently_at_02_and_14(tmp_path):
    """The batch hour's band is wide and the afternoon's is narrow, so the batch's load is usual at 02:00 and far
    above anything seen at 14:00."""
    st = LocalStorage(tmp_path)
    fill(st, last=MONDAY + timedelta(hours=12))
    at_02 = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))
    at_14 = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY.replace(hour=14)))
    assert at_14["label"] == "Monday 14:00 UTC" and at_14["weeks_used"] == 6
    wide, narrow = at_02["metrics"]["xact_commit"], at_14["metrics"]["xact_commit"]
    assert wide["max"] - wide["min"] == 300 and narrow["max"] - narrow["min"] == 2
    assert position(at_02, "xact_commit", 550) == "inside" and position(at_14, "xact_commit", 550) == "above_all"
    assert position(at_14, "xact_commit", 11) == "inside"


# --------------------------------------------------------------------------- through compare_ids
def test_compare_ids_carries_the_band_when_the_windows_match(tmp_path):
    from dbtriage.cli import compare_ids

    st = LocalStorage(tmp_path)
    fill(st)
    cur, prev = sid_at("postgres", "pg", MONDAY), sid_at("postgres", "pg", MONDAY - timedelta(hours=1))
    out = compare_ids(st, "postgres", "pg", cur, prev, ("previous", "previous snapshot"))
    b = out["band"]
    assert b["applies"] is True and b["weeks_used"] == 6 and b["above"] is False
    assert b["sentence"] == "total SQL time 1,100 ms/s is inside the usual Monday 02:00 UTC band (6 weeks): median 1,100 ms/s, high 1,340 ms/s"
    assert b["metrics"]["xact_commit"]["current"] == 550 and b["metrics"]["xact_commit"]["position"] == "inside"
    # the finding the hour raises against the quiet hour before it says the hour is usual
    drop = {f["kind"]: f for f in out["findings"]}
    assert "throughput_drop" not in drop  # commits rose; the load findings get the sentence when they fire
    # a week-long current interval (same-time-last-week baseline on a cumulative engine) is not compared with hour windows
    week = compare_ids(st, "postgres", "pg", cur, sid_at("postgres", "pg", MONDAY - timedelta(weeks=1)))
    assert week["band"]["applies"] is False and "168.0h long" in week["band"]["note"] and "sentence" not in week["band"]
    assert compare_ids(st, "postgres", "pg", cur, prev, band_weeks=0)["band"] is None
