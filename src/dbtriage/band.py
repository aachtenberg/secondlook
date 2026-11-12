"""The usual band: how this clock hour normally looks on this weekday, from the stored summaries.

A baseline is one snapshot, and one snapshot can itself be odd (last Monday's batch did not run, or ran twice). The
band is the spread of the same wall-clock hour over the past several weeks: a median and a high mark per load-profile
metric, for total SQL time, and for each wait class's share. compare.diff turns it into one sentence per finding
("inside the usual Monday 02:00 band (6 weeks)"); the single comparison snapshot stays the narrative baseline.

Pure arithmetic over summary.json files: no new store, no time-series database.
  * cumulative engines (postgres, mysql): a week contributes the consecutive pair of snapshots that spans the same
    hour; both must exist, and the interval between them is the window.
  * interval engines (oracle AWR): one snapshot per week, the one nearest the same time within the tolerance.
A counter reset or an instance restart inside a week's window voids that week. The band says how many weeks it is
built from and which were skipped and why, and it is not written at all below three.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from dbtriage.compare import _is_gauge, interval_between, interval_of

DEFAULT_WEEKS = 6
MIN_WEEKS = 3  # a "usual" needs at least this many windows; below it there is no band
PERCENTILE_FROM = 5  # with this many weeks the high mark is the 90th percentile, else the max


def weeks_of(v) -> int:
    """A target's band_weeks: the default when blank, the number otherwise; 0 switches the band off."""
    return DEFAULT_WEEKS if v in (None, "") else int(v)


def _ts(iso: str, like: datetime | None = None) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:  # an old naive timestamp is read in the current snapshot's zone, as baseline.py does
        dt = dt.replace(tzinfo=like.tzinfo if like is not None and like.tzinfo else datetime.now().astimezone().tzinfo)
    return dt


def _quantile(values: list[float], q: float) -> float:
    """Linear interpolation between the sorted values; q in [0, 1]."""
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    pos = q * (len(vs) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(vs) - 1)
    return vs[lo] + (vs[hi] - vs[lo]) * (pos - lo)


def _nearest(times: dict[str, datetime], at: datetime, tolerance: timedelta, exclude: str) -> str | None:
    best = None
    for sid, t in times.items():
        if sid == exclude:
            continue
        off = abs(t - at)
        if off <= tolerance and (best is None or off < best[0]):
            best = (off, sid)
    return best[1] if best else None


def _restart_inside(summary: dict, start: datetime, end: datetime) -> str | None:
    """Why an interval snapshot's window cannot stand for its hour: a restart inside it. AWR clips the window to the
    time since the restart and notes it; SQL Server's counters_since is the instance start."""
    awr = summary.get("awr") or {}
    if awr.get("note"):
        return "the AWR window spans an instance restart"
    for key, label in ((awr.get("startup"), "instance startup"), (summary.get("counters_since"), "counter reset")):
        if not key:
            continue
        try:
            t = _ts(key, end)
        except (TypeError, ValueError):
            continue
        if start < t <= end:
            return f"{label} at {t.isoformat()} inside the window"
    return None


def measures(iv: dict) -> dict[str, float]:
    """One interval as comparable numbers: counters per second, gauges as they are, total SQL time per second, and
    each wait class's share. The same reduction compare.diff applies to the current interval."""
    elapsed = iv.get("elapsed_s") or 0
    out: dict[str, float] = {}
    for k, v in (iv.get("load_profile") or {}).items():
        if v is None or k == "elapsed_s" or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if _is_gauge(k):
            out[k] = float(v)
        elif elapsed:
            out[k] = round(v / elapsed, 6)
    if elapsed:
        out["sql_time_ms"] = round(sum((s.get("total_time_ms") or 0) for s in (iv.get("sql") or {}).values()) / elapsed, 6)
    for cls, c in ((iv.get("waits") or {}).get("classes") or {}).items():
        out[f"wait_share:{cls}"] = c.get("share", 0)
    return out


def usual_band(store, engine: str, target: str, current_sid: str, weeks: int = DEFAULT_WEEKS,
               hours_tolerance: float = 1) -> dict | None:
    """The same clock hour and weekday as `current_sid`, over each of the past `weeks` weeks, as a band per metric.

    Returns None when fewer than MIN_WEEKS weeks are usable (or the band is switched off), else
    {label, hour, weekday, weeks, weeks_used, high_is, window_s, windows, skipped, metrics}, where each metric has
    {median, high, low, max, min, n}. The high mark is the 90th percentile from PERCENTILE_FROM weeks on, the max below.
    """
    if not weeks or weeks < MIN_WEEKS:
        return None
    times_iso = store.captured_at(engine, target)
    if current_sid not in times_iso:
        return None
    now = _ts(times_iso[current_sid])
    times = {}
    for sid, iso in times_iso.items():
        try:
            times[sid] = _ts(iso, now)
        except (TypeError, ValueError):
            continue
    tol = timedelta(hours=hours_tolerance)
    if min(times.values()) > now - timedelta(days=7 * MIN_WEEKS) + tol:
        return None  # not enough history yet: say so without opening a single summary

    current = store.get_json(f"{current_sid}/summary.json")
    current["id"] = current_sid
    interval_engine = current.get("metrics_kind") == "interval"

    def summary(sid: str) -> dict:
        s = store.get_json(f"{sid}/summary.json")
        s["id"] = sid
        return s

    # the hour being banded: the current snapshot's own window (the AWR interval, or the gap to the snapshot before)
    if interval_engine:
        cur_len = timedelta(seconds=interval_of(current).get("elapsed_s") or 3600)
    else:
        prev_sid = store.previous(engine, target, current_sid)
        cur_len = now - times[prev_sid] if prev_sid in times else timedelta(hours=1)

    windows: list[dict] = []
    skipped: list[dict] = []
    rows: list[dict[str, float]] = []
    for w in range(1, weeks + 1):
        at = now - timedelta(days=7 * w)
        sid = _nearest(times, at, tol, current_sid)
        if sid is None:
            skipped.append({"week": w, "reason": f"no snapshot within ±{hours_tolerance:g}h of {at.isoformat()}"})
            continue
        if interval_engine:
            iv = interval_of(summary(sid))
            end = times[sid]
            start = _ts(iv["start"], now) if iv.get("start") else end - timedelta(seconds=iv.get("elapsed_s") or 0)
            window = {"week": w, "snapshot": sid, "start": start.isoformat(), "end": end.isoformat(), "elapsed_s": iv.get("elapsed_s") or 0}
            why = _restart_inside(summary(sid), start, end)
        else:
            prev = store.previous(engine, target, sid)
            if prev is None:
                skipped.append({"week": w, "snapshot": sid, "reason": "no snapshot before it to form an interval"})
                continue
            a, b = summary(prev), summary(sid)
            iv = interval_between(a, b)
            start, end = _ts(iv["start"], now), _ts(iv["end"], now)
            window = {"week": w, "snapshot": sid, "baseline": prev, "start": iv["start"], "end": iv["end"], "elapsed_s": iv["elapsed_s"]}
            why = f"counter reset inside the window (counters_since {b.get('counters_since')})" if iv.get("counters_reset") else None
        if why is None and not (start < at and end > at - cur_len):
            # the nearest snapshot's window is the hour before or after: with hourly snapshots and an hour's tolerance
            # a missing 02:00 would otherwise borrow 01:00's window, which is not the same hour
            why = f"the window {start:%H:%M}-{end:%H:%M} does not cover the same hour ({(at - cur_len):%H:%M}-{at:%H:%M})"
        if why:
            skipped.append({"week": w, "snapshot": sid, "reason": why})
            continue
        windows.append(window)
        rows.append(measures(iv))

    if len(rows) < MIN_WEEKS:
        return None
    n = len(rows)
    high_is = "p90" if n >= PERCENTILE_FROM else "max"
    metrics: dict[str, dict[str, Any]] = {}
    for name in sorted({k for r in rows for k in r}):
        vals = [r[name] for r in rows if name in r]
        if len(vals) < MIN_WEEKS:
            continue  # a metric only some weeks had (an engine upgrade, a wait class that came and went) has no band
        metrics[name] = {"median": round(_quantile(vals, 0.5), 4), "n": len(vals),
                         "high": round(_quantile(vals, 0.9) if high_is == "p90" else max(vals), 4),
                         "low": round(_quantile(vals, 0.1) if high_is == "p90" else min(vals), 4),
                         "max": round(max(vals), 4), "min": round(min(vals), 4)}
    label = f"{now:%A %H:%M}" + (" UTC" if now.utcoffset() == timedelta(0) else "")
    return {
        "label": label, "weekday": f"{now:%A}", "hour": f"{now:%H:%M}", "weeks": weeks, "weeks_used": n, "high_is": high_is,
        "window_s": round(_quantile([w["elapsed_s"] for w in windows], 0.5), 1),
        "windows": windows, "skipped": skipped, "metrics": metrics,
    }


# --------------------------------------------------------------------------- sentences
def weeks_phrase(band: dict) -> str:
    """"6 weeks", or "4 of 6 weeks" when some were skipped: the count is part of the claim."""
    n, of = band["weeks_used"], band["weeks"]
    return f"{n} weeks" if n == of else f"{n} of {of} weeks"


def position(band: dict, metric: str, value) -> str | None:
    """above_all | above | inside | below for one value against the band; None when the metric has no band."""
    m = (band.get("metrics") or {}).get(metric)
    if m is None or value is None or not isinstance(value, (int, float)):
        return None
    if value > m["max"]:
        return "above_all"
    if value > m["high"]:
        return "above"
    if value < m["low"]:
        return "below"
    return "inside"


def sentence(band: dict, metric: str, value, what: str | None = None, unit: str = "", percent: bool = False) -> str | None:
    """One sentence for the evidence: "commits 12.3/s is inside the usual Monday 02:00 band (6 weeks): median 11/s,
    high 14/s". None when the metric is not comparable to the band."""
    pos = position(band, metric, value)
    if pos is None:
        return None
    m = band["metrics"][metric]
    weeks = weeks_phrase(band)
    if percent:
        value, m, unit = value * 100, {k: v * 100 for k, v in m.items()}, "%"
    where = {
        "above_all": f"above every same-hour window in {weeks}",
        "above": f"above the usual {band['label']} band ({weeks})",
        "inside": f"inside the usual {band['label']} band ({weeks})",
        "below": f"below the usual {band['label']} band ({weeks})",
    }[pos]
    return f"{what or metric} {_num(value)}{unit} is {where}: median {_num(m['median'])}{unit}, high {_num(m['high'])}{unit}"


def _num(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if v == 0:
        return "0"
    if abs(v) >= 100:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:.3g}"
    return f"{v:.2g}"
