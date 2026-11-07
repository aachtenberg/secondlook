"""The AWR snapshots a capture reports on must come from one instance startup: AWR cannot report across a restart
(ORA-20019), and the lab hit it after a host reboot, failing every capture whose window spanned the restart."""
from datetime import datetime, timedelta

import pytest

from dbtriage.collectors import oracle

A, B = datetime(2026, 11, 6, 9, 0), datetime(2026, 11, 7, 0, 59)  # startup before and after the reboot


def snap(i, startup):
    t = datetime(2026, 11, 7, 0, 0) + timedelta(minutes=10 * i)
    return {"snap_id": i, "begin_interval_time": t, "end_interval_time": t + timedelta(minutes=10), "startup_time": startup}


def picked(monkeypatch, window, latest):
    """_pick_snaps with dba_hist_snapshot answering `window` for the window query and `latest` for the fallback."""
    calls = []

    def rows(cur, sql, **binds):
        calls.append(sql)
        return list(window) if "end_interval_time >= :s" in sql else list(reversed(latest))  # fallback is newest first

    monkeypatch.setattr(oracle, "_rows", rows)
    out = oracle._pick_snaps(None, 1, 1, None, None)
    return out, len(calls)


def test_a_window_that_spans_a_restart_is_reported_from_the_restart_on(monkeypatch):
    (snaps, spans), queries = picked(monkeypatch, [snap(1, A), snap(2, A), snap(3, B), snap(4, B)], [])
    assert [s["snap_id"] for s in snaps] == [3, 4] and spans and queries == 1


def test_one_snapshot_since_the_restart_falls_back_to_the_latest_startup(monkeypatch):
    # in the window: 291 before the restart, 292 after it (the pair the lab failed on); AWR has 292 and 293 since
    (snaps, spans), queries = picked(monkeypatch, [snap(291, A), snap(292, B)], [snap(292, B), snap(293, B)])
    assert [s["snap_id"] for s in snaps] == [292, 293] and spans and queries == 2


def test_fewer_than_two_since_the_restart_is_a_clear_stop(monkeypatch):
    with pytest.raises(SystemExit, match="fewer than two snapshots since the instance last started"):
        picked(monkeypatch, [snap(291, A), snap(292, B)], [snap(292, B)])


def test_one_startup_is_unchanged(monkeypatch):
    (snaps, spans), queries = picked(monkeypatch, [snap(1, A), snap(2, A), snap(3, A)], [])
    assert [s["snap_id"] for s in snaps] == [1, 2, 3] and not spans and queries == 1
