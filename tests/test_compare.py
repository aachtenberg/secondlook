from datetime import datetime, timedelta, timezone

from dbtriage.compare import compare, interval_between

T0 = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)


def snap(minutes, sql, lp=None, reset="2026-11-01T00:00:00+00:00", tables=None, sessions=None):
    return {
        "engine": "postgres", "metrics_kind": "cumulative", "id": f"s{minutes}",
        "captured_at": (T0 + timedelta(minutes=minutes)).isoformat(), "counters_since": reset,
        "load_profile": {"xact_commit": 1000 * minutes, "blks_hit": 100000 * minutes, "blks_read": 1000 * minutes,
                         "deadlocks": 0, "temp_bytes": 0, "sessions_active": 3, **(lp or {})},
        "top_sql": [{"sql_id": k, "text": k, "calls": c, "total_time_ms": t, "rows": 1} for k, (c, t) in sql.items()],
        "wait_events": [{"class": "IO", "event": "DataFileRead", "count": 10 * minutes},
                        {"class": "Lock", "event": "relation", "count": (50 if minutes >= 20 else 1) * minutes}],
        "tables": tables or [], "sessions": sessions or [],
    }


def test_interval_deltas_and_rates():
    a, b = snap(0, {"q1": (100, 1000)}), snap(10, {"q1": (200, 3000)})
    iv = interval_between(a, b)
    assert iv["elapsed_s"] == 600
    assert iv["load_profile"]["xact_commit"] == 10000
    assert iv["sql"]["q1"]["calls"] == 100 and iv["sql"]["q1"]["total_time_ms"] == 2000
    assert iv["sql"]["q1"]["mean_time_ms"] == 20
    assert iv["load_profile"]["sessions_active"] == 3  # gauge, not diffed


def test_counter_reset_uses_later_value():
    a = snap(0, {"q1": (500, 5000)})
    b = snap(10, {"q1": (50, 500)}, reset="2026-11-05T10:05:00+00:00")
    iv = interval_between(a, b)
    assert iv["counters_reset"] is True
    assert iv["sql"]["q1"]["calls"] == 50


def test_regression_detected_against_reference_interval():
    s0 = snap(0, {"q1": (0, 0), "q2": (0, 0)})
    s1 = snap(10, {"q1": (1000, 10000), "q2": (1000, 1000)})           # ref interval: q1 10ms/call
    s2 = snap(20, {"q1": (2000, 60000), "q2": (2000, 2000)})           # cur interval: q1 50ms/call
    out = compare(s2, s1, s0)
    kinds = {f["kind"]: f for f in out["findings"]}
    assert "sql_regressed" in kinds and kinds["sql_regressed"]["evidence"]["sql_id"] == "q1"
    assert kinds["sql_regressed"]["severity"] in ("medium", "high")
    assert any(s["class"] == "Lock" and s["delta"] > 0.15 for s in out["waits"]["shifts"])
    assert "wait_shift" in kinds
    assert out["severity"] in ("medium", "high")


def test_new_sql_and_gone_sql():
    s0, s1 = snap(0, {"q1": (0, 0)}), snap(10, {"q1": (100, 10000)})
    s2 = snap(20, {"q1": (100, 10000), "q9": (10, 50000)})   # q1 stopped, q9 appeared and dominates
    out = compare(s2, s1, s0)
    assert [n["sql_id"] for n in out["sql"]["new"]] == ["q9"]
    assert [g["sql_id"] for g in out["sql"]["gone"]] == ["q1"]
    assert any(f["kind"] == "sql_new" for f in out["findings"])


def test_no_reference_interval_gives_baseline_findings_only():
    out = compare(snap(10, {"q1": (10, 100)}), snap(0, {"q1": (0, 0)}), None)
    assert out["reference"] is None
    assert {f["kind"] for f in out["findings"]} <= {"sql_top"}


def test_blocking_and_bloat_findings():
    tables = [{"name": "public.orders", "live_rows": 100000, "dead_rows": 40000, "seq_scan": 5}]
    sessions = [{"state": "active", "blocked_by": 42, "duration_s": 120, "text": "update orders"}]
    s2 = snap(20, {"q1": (20, 200)}, tables=tables, sessions=sessions)
    out = compare(s2, snap(10, {"q1": (10, 100)}), snap(0, {"q1": (0, 0)}))
    kinds = {f["kind"] for f in out["findings"]}
    assert {"blocking", "bloat", "long_running"} <= kinds
    assert next(f for f in out["findings"] if f["kind"] == "blocking")["severity"] == "medium"


def test_interval_engine_compares_windows_directly():
    def awr(sid, q1_time, elapsed=3600):
        return {"engine": "oracle", "metrics_kind": "interval", "id": sid, "captured_at": T0.isoformat(),
                "counters_since": (T0 - timedelta(seconds=elapsed)).isoformat(),
                "load_profile": {"elapsed_s": elapsed, "db_time_ms": 1000, "xact_commit": 100},
                "top_sql": [{"sql_id": "q1", "text": "q1", "calls": 100, "total_time_ms": q1_time, "mean_time_ms": q1_time / 100, "plan_hash": 1 if q1_time < 5000 else 2},
                            {"sql_id": "q2", "text": "q2", "calls": 100, "total_time_ms": 1000, "mean_time_ms": 10}],
                "wait_events": [{"class": "User I/O", "event": "db file sequential read", "time_ms": 100}],
                "findings": [{"id": "x", "finding": "Top SQL Statements", "type": "PROBLEM", "impact_pct": 60, "message": "SQL statements consuming significant database time were found."}]}
    out = compare(awr("cur", 9000), awr("base", 1000))
    f = {x["kind"]: x for x in out["findings"]}
    assert f["sql_regressed"]["evidence"]["plan_changed"] is True
    assert "engine_problem" in f
    assert out["load_profile"]["xact_commit"]["current_per_s"] == out["load_profile"]["xact_commit"]["reference_per_s"]


def test_setting_change_is_a_finding_and_listed():
    s0, s1 = snap(0, {"q1": (0, 0)}), snap(10, {"q1": (100, 1000)})
    s2 = snap(20, {"q1": (200, 2000)})
    s1["settings"] = {"work_mem": "4MB", "max_connections": 100, "jit": "on"}
    s2["settings"] = {"work_mem": "64MB", "max_connections": "100", "shared_buffers": "1GB"}  # 100 == "100"
    out = compare(s2, s1, s0)
    assert out["settings_changed"] == [{"name": "jit", "before": "on", "after": None},
                                       {"name": "shared_buffers", "before": None, "after": "1GB"},
                                       {"name": "work_mem", "before": "4MB", "after": "64MB"}]
    f = [x for x in out["findings"] if x["kind"] == "setting_changed"]
    assert len(f) == 3 and all(x["severity"] == "low" for x in f)
    wm = next(x for x in f if x["evidence"]["parameter"] == "work_mem")
    assert wm["title"] == "parameter work_mem changed from 4MB to 64MB"
    assert wm["evidence"] == {"parameter": "work_mem", "before": "4MB", "after": "64MB"}


def test_unchanged_settings_produce_nothing():
    s1, s2 = snap(10, {"q1": (100, 1000)}), snap(20, {"q1": (200, 2000)})
    s1["settings"] = {"work_mem": "4MB", "max_connections": 100}
    s2["settings"] = {"work_mem": " 4MB ", "max_connections": "100"}
    out = compare(s2, s1, snap(0, {"q1": (0, 0)}))
    assert out["settings_changed"] == [] and not any(f["kind"] == "setting_changed" for f in out["findings"])
    del s1["settings"]  # no settings on one side (an older snapshot): nothing to say either
    assert compare(s2, s1, None)["settings_changed"] == []


def test_regression_in_same_window_carries_the_settings_change():
    s0 = snap(0, {"q1": (0, 0), "q2": (0, 0)})
    s1 = snap(10, {"q1": (1000, 10000), "q2": (1000, 1000)})
    s2 = snap(20, {"q1": (2000, 60000), "q2": (2000, 2000)})
    s1["settings"], s2["settings"] = {"work_mem": "4MB", "jit": "on"}, {"work_mem": "64MB", "jit": "off"}
    out = compare(s2, s1, s0)
    kinds = {f["kind"]: f for f in out["findings"]}
    assert kinds["sql_regressed"]["evidence"]["settings_changed"] == ["jit", "work_mem"]
    assert kinds["wait_shift"]["evidence"]["settings_changed"] == ["jit", "work_mem"]
    assert "settings_changed" not in kinds["setting_changed"]["evidence"]
    out = compare(s2, s1, s0 | {"settings": {"work_mem": "4MB", "jit": "on"}})  # the change still sits in the current window
    assert {f["kind"]: f for f in out["findings"]}["sql_regressed"]["evidence"]["settings_changed"] == ["jit", "work_mem"]


def test_setting_findings_are_capped_with_a_summary():
    s1, s2 = snap(10, {"q1": (100, 1000)}), snap(20, {"q1": (200, 2000)})
    s1["settings"] = {f"p{i}": i for i in range(8)}
    s2["settings"] = {f"p{i}": i + 1 for i in range(8)}
    out = compare(s2, s1, snap(0, {"q1": (0, 0)}))
    f = [x for x in out["findings"] if x["kind"] == "setting_changed"]
    assert len(out["settings_changed"]) == 8 and len(f) == 6
    summary = next(x for x in f if "parameter" not in x["evidence"])
    assert summary["title"] == "3 more settings changed" and summary["evidence"]["parameters"] == ["p5", "p6", "p7"]
    assert summary["detail"] == "p5, p6, p7"
