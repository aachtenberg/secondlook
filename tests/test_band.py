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
         restart_note: set | None = None, startup_inside: set | None = None, late: set | None = None, no_lock: set | None = None,
         db_offset: timedelta | None = None) -> None:
    """Hourly snapshots from `weeks` weeks (and two hours) before MONDAY to MONDAY, cumulative counters or AWR-style
    intervals. `missing` hours are not stored; `reset_at` hours carry a new counters_since (a reset inside that hour);
    `restart_note` and `startup_inside` mark AWR windows that span a restart; `late` hours are captured 59 minutes
    after the hour; `no_lock` hours have no Lock waits; `db_offset` writes the AWR timestamps as naive values on a
    database clock that far from UTC, as Oracle reports them."""
    first = first or MONDAY - timedelta(weeks=weeks, hours=2)
    last = last or MONDAY
    t, total = first, {"commits": 0, "ms": 0}

    def clock(x: datetime) -> str:
        return (x + db_offset).replace(tzinfo=None).isoformat() if db_offset is not None else x.isoformat()

    while t <= last:
        r = rate(t)
        hour_commits = r * 3600
        total["commits"] += hour_commits
        total["ms"] += hour_commits * 2
        if t in (missing or set()):
            t += timedelta(hours=1)
            continue
        captured = t + timedelta(minutes=59) if t in (late or set()) else t
        if kind == "cumulative":
            since = "2026-01-01T00:00:00+00:00" if t not in (reset_at or set()) else (t - timedelta(minutes=30)).isoformat()
            summary = {"engine": engine, "metrics_kind": "cumulative", "captured_at": captured.isoformat(), "counters_since": since,
                       "load_profile": {"xact_commit": total["commits"], "blks_hit": total["commits"] * 100, "blks_read": total["commits"],
                                        "temp_bytes": 0, "deadlocks": 0, "sessions_active": 3},
                       "top_sql": [{"sql_id": "q1", "text": "q1", "calls": total["commits"], "total_time_ms": total["ms"], "rows": 1}],
                       "wait_events": [{"class": "IO", "event": "read", "count": total["commits"]}]
                       + ([] if t in (no_lock or set()) else [{"class": "Lock", "event": "relation", "count": total["commits"] // 10}])}
        else:
            begin = t - timedelta(hours=1)
            awr = {"begin": clock(begin), "end": clock(t), "elapsed_s": 3600, "startup": clock(datetime(2026, 1, 1, tzinfo=timezone.utc))}
            if t in (restart_note or set()):
                awr["note"] = "the window spans an instance restart"
            if t in (startup_inside or set()):
                awr["startup"] = clock(t - timedelta(minutes=20))
            summary = {"engine": engine, "metrics_kind": "interval", "captured_at": captured.isoformat(), "counters_since": clock(begin), "awr": awr,
                       "load_profile": {"elapsed_s": 3600, "xact_commit": hour_commits, "db_time_ms": hour_commits * 2, "sessions_active": 3},
                       "top_sql": [{"sql_id": "q1", "text": "q1", "calls": hour_commits, "total_time_ms": hour_commits * 2, "mean_time_ms": 2}],
                       "wait_events": [{"class": "User I/O", "event": "db file sequential read", "time_ms": hour_commits},
                                       {"class": "Concurrency", "event": "latch", "time_ms": hour_commits // 10}]}
        sid = sid_at(engine, target, captured)
        store.put_json(f"{sid}/summary.json", summary)
        store.put_json(f"{sid}/manifest.json", {"id": sid, "engine": engine, "target": target, "captured_at": captured.isoformat(),
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


def test_a_window_must_cover_the_hour_and_match_its_length(tmp_path):
    """A missing 01:00 folds two hours into last Monday's 02:00 window, and a late collector's 02:59 window touches
    the hour by a minute: neither stands for it, the next snapshot inside the tolerance is tried, and the week
    says why. A clean hour is kept."""
    st = LocalStorage(tmp_path)
    folded, late = mondays(6)[1], mondays(6)[3]
    fill(st, missing={folded - timedelta(hours=1)}, late={late - timedelta(hours=1), late})
    band = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))
    assert band["weeks_used"] == 4 and [s["week"] for s in band["skipped"]] == [2, 4]
    assert [datetime.fromisoformat(w["end"]) for w in band["windows"]] == [mondays(6)[i] for i in (0, 2, 4, 5)]
    two_hours = band["skipped"][0]
    assert two_hours["snapshot"] == sid_at("postgres", "pg", folded)
    assert two_hours["reason"] == "the window 00:00-02:00 is 2.0h long, not the current interval's 1.0h"
    assert [t["snapshot"] for t in two_hours["also_tried"]] == [sid_at("postgres", "pg", folded + timedelta(hours=1))]  # 03:00: the hour after
    touch = band["skipped"][1]
    assert touch["snapshot"] == sid_at("postgres", "pg", late - timedelta(minutes=1))  # 01:59 is nearest to 02:00, but its window starts at 00:00
    assert touch["reason"] == "the window 00:00-01:59 is 2.0h long, not the current interval's 1.0h"
    assert touch["also_tried"] == [{"snapshot": sid_at("postgres", "pg", late + timedelta(minutes=59)),
                                    "reason": "the window 01:59-02:59 does not cover the same hour (01:00-02:00)"},
                                   {"snapshot": sid_at("postgres", "pg", late + timedelta(hours=1)),  # 03:00 is an hour off: still inside the tolerance
                                    "reason": "the window 02:59-03:00 does not cover the same hour (01:00-02:00)"}]
    # the hour is the interval the review scores: a two-hour baseline -> current has no hour-long window to match
    assert usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY), baseline=sid_at("postgres", "pg", MONDAY - timedelta(hours=2))) is None


def test_a_week_whose_summary_cannot_be_read_is_skipped_not_fatal(tmp_path):
    """summary.json gone from one past week, malformed in another: each voids its week with the reason, like a
    reset; the current snapshot's own summary still has to be there."""
    import pytest

    st = LocalStorage(tmp_path)
    fill(st)
    gone, broken = mondays(6)[1], mondays(6)[3]
    (tmp_path / sid_at("postgres", "pg", gone) / "summary.json").unlink()
    st.put_json(f"{sid_at('postgres', 'pg', broken)}/summary.json", {"captured_at": broken.isoformat(), "load_profile": "nope"})
    band = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))
    assert band["weeks_used"] == 4 and [s["week"] for s in band["skipped"]] == [2, 4]
    assert band["skipped"][0]["snapshot"] == sid_at("postgres", "pg", gone) and band["skipped"][0]["reason"].startswith("its summary cannot be read (FileNotFoundError")
    assert band["skipped"][1]["reason"].startswith("its summary cannot be read (")
    (tmp_path / sid_at("postgres", "pg", MONDAY) / "summary.json").unlink()
    with pytest.raises(FileNotFoundError):
        usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))


def test_a_metric_seen_in_only_some_weeks_counts_its_own_weeks(tmp_path):
    """Lock waits in three of the six Mondays: the wait class's band has n 3, its high mark is the max (not a p90
    from the window count), and its sentence says 3 of 6 weeks while a metric in every window still says 6."""
    st = LocalStorage(tmp_path)
    fill(st, no_lock=set(mondays(6)[:3]))
    band = usual_band(st, "postgres", "pg", sid_at("postgres", "pg", MONDAY))
    lock = band["metrics"]["wait_share:Lock"]
    assert band["weeks_used"] == 6 and band["high_is"] == "p90" and band["metrics"]["xact_commit"]["high_is"] == "p90"
    assert lock["n"] == 3 and lock["high_is"] == "max" and lock["high"] == lock["max"] and lock["low"] == lock["min"]
    assert sentence(band, "wait_share:Lock", 0.3, "wait class 'Lock'", percent=True) == \
        "wait class 'Lock' 30% is above every same-hour window in 3 of 6 weeks: median 9.1%, high 9.1%"
    assert "(3 of 6 weeks)" in sentence(band, "wait_share:Lock", 0.09, "wait class 'Lock'", percent=True)
    assert "(6 weeks)" in sentence(band, "xact_commit", 550)


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


def test_awr_windows_are_placed_by_elapsed_s_not_the_database_clock(tmp_path):
    """Oracle reports begin, end, startup and counters_since as naive timestamps on the database's clock. Five hours
    ahead of UTC that used to skip every week (the window looked like the afternoon); behind, it stretched the
    overlap. The week's window is captured_at minus elapsed_s, and the startup is checked on the database's clock."""
    st = LocalStorage(tmp_path)
    startup = mondays(6)[2]
    fill(st, engine="oracle", target="ora", kind="interval", db_offset=timedelta(hours=5), startup_inside={startup})
    band = usual_band(st, "oracle", "ora", sid_at("oracle", "ora", MONDAY))
    assert band["weeks_used"] == 5 and [s["week"] for s in band["skipped"]] == [3]
    # 01:40 UTC is 06:40 on that clock: the reason names the clock, not a UTC offset the naive value never had
    assert band["skipped"][0]["reason"] == "instance startup at 2026-10-19 06:40 on the database's clock inside the window"
    ends = [datetime.fromisoformat(w["end"]) for w in band["windows"]]
    assert ends == [mondays(6)[i] for i in (0, 1, 3, 4, 5)]
    assert all(datetime.fromisoformat(w["start"]) == e - timedelta(hours=1) for w, e in zip(band["windows"], ends))
    behind = LocalStorage(tmp_path / "behind")
    fill(behind, engine="oracle", target="ora", kind="interval", db_offset=timedelta(hours=-5))
    assert usual_band(behind, "oracle", "ora", sid_at("oracle", "ora", MONDAY))["weeks_used"] == 6


def test_startup_without_an_awr_window_is_held_against_utc_and_says_so(tmp_path):
    """Without AWR's begin and end the startup can only be compared with the UTC window the band keeps, which is
    approximate when the startup is a naive database-clock value; an aware startup compares exactly and says nothing."""
    week3 = sid_at("oracle", "ora", mondays(6)[2])
    for name, offset, reason in (
        ("naive", timedelta(0), "instance startup at 2026-10-19 01:40 on the database's clock inside the window "
                                "(approximate: no AWR begin and end, so the database's clock is compared with UTC)"),
        ("aware", None, "instance startup at 2026-10-19T01:40:00+00:00 inside the window"),
    ):
        st = LocalStorage(tmp_path / name)
        fill(st, engine="oracle", target="ora", kind="interval", db_offset=offset, startup_inside={mondays(6)[2]})
        summary = st.get_json(f"{week3}/summary.json")
        del summary["awr"]["begin"], summary["awr"]["end"]
        st.put_json(f"{week3}/summary.json", summary)
        band = usual_band(st, "oracle", "ora", sid_at("oracle", "ora", MONDAY))
        assert [s["week"] for s in band["skipped"]] == [3] and band["skipped"][0]["reason"] == reason


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
    band["metrics"]["xact_commit"]["n"] = 4  # the metric's own count of weeks is the claim, not the band's window count
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
    # a week-long current interval (same-time-last-week baseline on a cumulative engine) has no hour windows to match
    assert compare_ids(st, "postgres", "pg", cur, sid_at("postgres", "pg", MONDAY - timedelta(weeks=1)))["band"] is None
    assert compare_ids(st, "postgres", "pg", cur, prev, band_weeks=0)["band"] is None
