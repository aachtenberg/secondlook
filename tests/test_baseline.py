from datetime import timedelta

import pytest

from dbtriage.baseline import POLICIES, choose

T = "postgres/pg"


def snaps(*hours_ago, now="2026-11-12T15:00:00+00:00"):
    """Snapshots at `now` minus N hours (floats allowed), ids sortable by time."""
    from datetime import datetime
    base = datetime.fromisoformat(now)
    out = {}
    for h in hours_ago:
        t = base - timedelta(hours=h)
        out[f"{T}/{t:%Y-%m-%d}/{t:%Y%m%dT%H%M%SZ}"] = t.isoformat()
    return out


def sid(h, now="2026-11-12T15:00:00+00:00"):
    return next(iter(snaps(h, now=now)))


def test_previous_and_first():
    s = snaps(0, 1, 2)
    assert choose(s, sid(0)) == (sid(1), "previous", "previous snapshot")
    assert choose(s, sid(2)) == (None, "previous", "first snapshot for this target")
    assert choose(s, sid(0), "previous") == (sid(1), "previous", "previous snapshot")


def test_same_time_yesterday_and_last_week():
    s = snaps(0, 1, 23.5, 24.75, 48, 24 * 7 + 1.5, 24 * 7 + 3)
    b, pol, why = choose(s, sid(0), "same-time-yesterday")
    assert b == sid(23.5) and pol == "same-time-yesterday" and "30 min off target" in why
    b, pol, why = choose(s, sid(0), "same-time-last-week")
    assert b == sid(24 * 7 + 1.5) and pol == "same-time-last-week" and "90 min off target" in why
    # nothing within ±2h: fall back to previous and say so
    s2 = snaps(0, 1, 30)
    assert choose(s2, sid(0), "same-time-yesterday") == (sid(1), "previous", "nothing within ±2h of same time yesterday; fell back to previous snapshot")
    assert choose(s2, sid(0), "same-time-last-week", tolerance=timedelta(hours=1)) == (sid(1), "previous", "nothing within ±1h of same time last week; fell back to previous snapshot")
    # only snapshot at all
    s3 = snaps(0)
    assert choose(s3, sid(0), "same-time-yesterday")[0] is None and "no earlier snapshot" in choose(s3, sid(0), "same-time-yesterday")[2]
    # later snapshots are never baselines even if they are close to the target time
    s4 = {**snaps(0, 1), **snaps(-23)}  # one captured in the future (replayed data)
    assert choose(s4, sid(0), "same-time-yesterday")[1] == "previous"


def test_pinned_and_explicit():
    s = snaps(0, 1, 5)
    assert choose(s, sid(0), "pinned", pinned=sid(5)) == (sid(5), "pinned", f"pinned baseline {sid(5)}")
    assert choose(s, sid(0), "pinned") == (sid(1), "previous", "no pinned baseline configured; fell back to previous snapshot")
    assert choose(s, sid(0), "pinned", pinned="postgres/pg/gone") == (sid(1), "previous", "pinned baseline postgres/pg/gone not found; fell back to previous snapshot")
    assert choose(s, sid(0), "pinned", pinned=sid(0))[1] == "previous"  # pinning yourself is ignored
    assert choose(s, sid(0), "same-time-last-week", explicit=sid(5)) == (sid(5), "explicit", f"requested snapshot {sid(5)}")
    assert choose(s, sid(0), explicit="nope") == (sid(1), "previous", "requested baseline nope not found; fell back to previous snapshot")
    assert choose(snaps(0), sid(0), explicit="nope")[0] is None


def test_bad_policy_and_naive_timestamps():
    with pytest.raises(ValueError):
        choose(snaps(0), sid(0), "random")
    assert set(POLICIES) == {"previous", "same-time-yesterday", "same-time-last-week", "pinned"}
    s = {k: v.replace("+00:00", "") for k, v in snaps(0, 24).items()}  # naive ISO (Oracle collectors)
    assert choose(s, sid(0), "same-time-yesterday")[1] == "same-time-yesterday"
