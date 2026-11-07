from datetime import datetime, timedelta, timezone

import pytest

from dbtriage import scheduler as sch
from dbtriage.config import Config, Target
from dbtriage.settings import Settings


def test_normalize_and_describe():
    assert sch.normalize("every 15m") == "*/15 * * * *" and sch.normalize("every 2h") == "0 */2 * * *" and sch.normalize("hourly") == "0 * * * *"
    assert sch.normalize("every 1m") == "* * * * *" and sch.normalize("  0  3 * * *  ") == "0 3 * * *" and sch.normalize("") == ""
    assert sch.describe("0 * * * *") == "hourly" and sch.describe("every 15m") == "every 15 minutes" and sch.describe("5 4 * * *") == "cron 5 4 * * *"
    assert sch.describe(None) == "not scheduled"
    for bad in ("every 7m", "every 5h", "0 * * *", "60 * * * *", "a b c d e", "*/0 * * * *", "5-3 * * * *"):
        with pytest.raises(ValueError):
            sch.normalize(bad)


def test_next_after():
    t = datetime(2026, 11, 5, 10, 7, 30, tzinfo=timezone.utc)
    assert sch.parse("0 * * * *").next_after(t) == datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc)
    assert sch.parse("*/15 * * * *").next_after(t) == datetime(2026, 11, 5, 10, 15, tzinfo=timezone.utc)
    assert sch.parse("0 3 * * *").next_after(t) == datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
    assert sch.parse("0 8 * * 1-5").next_after(datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)  # Fri -> Mon
    assert sch.parse("30 2 1 * *").next_after(t) == datetime(2026, 12, 1, 2, 30, tzinfo=timezone.utc)
    assert sch.parse("0 0 29 2 *").next_after(t) == datetime(2028, 2, 29, 0, 0, tzinfo=timezone.utc)
    # vixie: day-of-month and day-of-week both restricted = either
    assert sch.parse("0 0 15 * 0").next_after(datetime(2026, 11, 5, tzinfo=timezone.utc)) == datetime(2026, 11, 8, tzinfo=timezone.utc)
    assert sch.parse("0 * * * *").next_after(datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 5, 11, 0, tzinfo=timezone.utc)


def test_scheduler_fires_skips_and_tracks():
    fired = []
    cfg = {"a": {"schedule": "*/15 * * * *", "schedule_window": 30}, "b": {"schedule": "0 * * * *", "schedule_paused": True}, "c": {}}
    busy = {"on": False}

    def fire(name, window, reason):
        fired.append((name, window, reason))
        return (False, "busy") if busy["on"] else (True, "started")

    s = sch.Scheduler(lambda: cfg, fire)
    t0 = datetime(2026, 11, 5, 10, 1, tzinfo=timezone.utc)
    s.tick(t0)
    assert fired == [] and s.state["a"].next == datetime(2026, 11, 5, 10, 15, tzinfo=timezone.utc) and s.state["b"].next is not None
    s.tick(t0 + timedelta(minutes=10))
    assert fired == []
    s.tick(t0 + timedelta(minutes=15))
    assert fired == [("a", 30, "schedule every 15 minutes")] and s.state["a"].last_status == "started"
    assert s.state["a"].next == datetime(2026, 11, 5, 10, 30, tzinfo=timezone.utc)
    busy["on"] = True
    s.tick(t0 + timedelta(minutes=31))  # late tick still fires once, not twice
    assert len(fired) == 2 and s.state["a"].last_status == "skipped: busy" and s.state["a"].next == datetime(2026, 11, 5, 10, 45, tzinfo=timezone.utc)
    s.tick(t0 + timedelta(hours=1))  # b is paused: never fires
    assert all(n != "b" for n, _, _ in fired)
    cfg["a"]["schedule"] = "0 3 * * *"  # a changed schedule is picked up at the next tick
    s.tick(t0 + timedelta(hours=2))
    assert s.state["a"].expr == "0 3 * * *" and s.state["a"].next == datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
    rows = {r["target"]: r for r in s.rows(cfg)}
    assert rows["a"]["label"] == "daily at 03:00" and rows["b"]["paused"] and rows["b"]["next"] is None and rows["c"]["schedule"] == ""
    del cfg["c"]
    s.tick(t0 + timedelta(hours=3))
    assert "c" not in s.state


def test_settings_put_schedule_patches_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("PG_DSN", "postgresql://u:secret@h/db")
    seed = Config(storage_url="x", base_dir=str(tmp_path), targets={
        "demo": Target("demo", "postgres", "postgresql://u:secret@h/db", extra={"dsn_env": "PG_DSN", "links": [{"label": "G", "url": "https://g"}]})})
    st = Settings(tmp_path / "s.json")
    assert st.put_schedule("demo", seed, {"schedule": "every 15m", "schedule_window": "30", "schedule_paused": False}) == []
    assert st.data["targets"]["demo"] == {"schedule": "*/15 * * * *", "schedule_window": 30}  # only what differs from the file
    assert "secret" not in (tmp_path / "s.json").read_text()
    eff = st.apply(seed).targets["demo"]
    assert eff.dsn == "postgresql://u:secret@h/db" and eff.extra["schedule"] == "*/15 * * * *" and eff.extra["links"][0]["label"] == "G"
    form = st.form_of("demo", seed)
    assert form["dsn_env"] == "PG_DSN" and "dsn" not in form and form["schedule_window"] == 30
    assert st.put_schedule("demo", seed, {"schedule": "every 7m"}) == ["schedule: 'every Nm' needs N dividing 60 (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)"]
    assert st.put_schedule("demo", seed, {"schedule": "hourly", "schedule_window": "2"}) == ["schedule_window: 5 to 1440 minutes"]
    assert st.put_schedule("demo", seed, {"schedule": "hourly", "schedule_paused": True}) == [] and st.data["targets"]["demo"]["schedule_paused"] is True
    assert st.put_schedule("demo", seed, {"schedule": ""}) == [] and st.data["targets"]["demo"] == {}  # the file has no schedule either
    # a full edit through put_target keeps validating the schedule too
    assert st.put_target("demo", {"engine": "postgres", "dsn_env": "PG_DSN", "schedule": "0 * * *"}) == ["schedule: a schedule has five fields: minute hour day month weekday (e.g. '0 * * * *'), or 'every 15m'"]
