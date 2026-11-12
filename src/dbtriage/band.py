"""The usual band: how this clock hour normally looks on this weekday, from the stored summaries.

A baseline is one snapshot, and one snapshot can itself be odd (last Monday's batch did not run, or ran twice). The
band is the spread of the same wall-clock hour over the past several weeks: a median and a high mark per load-profile
metric, for total SQL time, and for each wait class's share. compare.diff turns it into one sentence per finding
("inside the usual Monday 02:00 band (6 weeks)"); the single comparison snapshot stays the narrative baseline.

Pure arithmetic over summary.json files: no new store, no time-series database.
  * cumulative engines (postgres, mysql, sql server): a week contributes the consecutive pair of snapshots that spans
    the same hour; both must exist, and the interval between them is the window.
  * interval engines (oracle AWR): one snapshot per week, the one nearest the same time within the tolerance.
A past window stands for the hour only when it covers most of it and is about as long as the interval the review
scores (COVERAGE): a missing neighbour that folds two hours into one, or a late snapshot whose window touches the
hour by a minute, is not the same hour. A counter reset or an instance restart inside the window, or a summary that
cannot be read, voids that week. The band says how many weeks it is built from and which were skipped and why, and
it is not written at all below three.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from dbtriage.compare import _is_gauge, interval_between, interval_of

DEFAULT_WEEKS = 6
MIN_WEEKS = 3  # a "usual" needs at least this many windows; below it there is no band
PERCENTILE_FROM = 5  # with this many weeks the high mark is the 90th percentile, else the max
COVERAGE = 0.8  # a past window must cover this share of the hour, and be within this ratio of the current interval's length
MARKS = ("median", "high", "low", "max", "min")  # a metric's band, in the metric's unit; n and high_is sit beside them


def weeks_of(v) -> int:
    """A target's band_weeks: the default when blank, the number otherwise; 0 switches the band off."""
    return DEFAULT_WEEKS if v in (None, "") else int(v)


def weeks_for(cfg, target: str) -> int:
    """A target's band_weeks by name from the config, as every front end should read it; the default for a target
    the config does not know."""
    t = cfg.targets.get(target)
    return weeks_of((t.extra if t else {}).get("band_weeks"))


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


def _candidates(times: dict[str, datetime], at: datetime, tolerance: timedelta, exclude: str) -> list[str]:
    """The snapshots within the tolerance of `at`, nearest first."""
    near = [(abs(t - at), sid) for sid, t in times.items() if sid != exclude and abs(t - at) <= tolerance]
    return [sid for _, sid in sorted(near)]


def _span(a: datetime, b: datetime) -> str:
    return f"{a:%H:%M}-{b:%H:%M}" if b - a <= timedelta(days=1) else f"{a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}"


def _fits(start: datetime, end: datetime, at: datetime, cur_len: timedelta) -> str | None:
    """Why a past window cannot stand for the hour [at - cur_len, at]: it covers too little of it (the neighbouring
    hour, or a late snapshot's window that touches it by minutes), or it is much longer or shorter than the current
    interval (a missing snapshot folds two hours into one). None when it fits."""
    since = at - cur_len
    if min(end, at) - max(start, since) < cur_len * COVERAGE:
        return f"the window {_span(start, end)} does not cover the same hour ({_span(since, at)})"
    length = end - start
    if not cur_len * COVERAGE <= length <= cur_len / COVERAGE:
        return (f"the window {_span(start, end)} is {length.total_seconds() / 3600:.1f}h long, "
                f"not the current interval's {cur_len.total_seconds() / 3600:.1f}h")
    return None


def _restart_inside(summary: dict, start: datetime, end: datetime) -> str | None:
    """Why an interval snapshot's window cannot stand for its hour: a restart inside it. AWR clips the window to the
    time since the restart and notes it; the startup time is checked as well. AWR's begin, end and startup are naive
    timestamps on the database's own clock, so they are compared with each other as they are and the reason names
    that clock, never a UTC offset the value never had. Only without begin and end is the startup held against the
    UTC window given, which is approximate when the startup is naive: the database's clock may be hours from UTC."""
    awr = summary.get("awr") or summary.get("statspack") or {}  # Statspack's window has the same keys and clock
    if awr.get("note"):
        return f"the {'AWR' if summary.get('awr') else 'Statspack'} window spans an instance restart"
    if not awr.get("startup"):
        return None
    try:
        startup = datetime.fromisoformat(awr["startup"])
        t = _ts(awr["startup"], end)
        approximate = ""
        if awr.get("begin") and awr.get("end"):
            start, end = _ts(awr["begin"], end), _ts(awr["end"], end)
        elif startup.tzinfo is None:
            approximate = " (approximate: no AWR begin and end, so the database's clock is compared with UTC)"
    except (TypeError, ValueError):
        return None
    if start < t <= end:
        when = f"{startup:%Y-%m-%d %H:%M} on the database's clock" if startup.tzinfo is None else startup.isoformat()
        return f"instance startup at {when} inside the window{approximate}"
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
    sql_elapsed = iv.get("sql_elapsed_s") or elapsed  # Query Store's statements cover their own intervals
    if sql_elapsed:
        out["sql_time_ms"] = round(sum((s.get("total_time_ms") or 0) for s in (iv.get("sql") or {}).values()) / sql_elapsed, 6)
    for cls, c in ((iv.get("waits") or {}).get("classes") or {}).items():
        out[f"wait_share:{cls}"] = c.get("share", 0)
    return out


def usual_band(store, engine: str, target: str, current_sid: str, weeks: int = DEFAULT_WEEKS,
               hours_tolerance: float = 1, baseline: str | None = None) -> dict | None:
    """The same clock hour and weekday as `current_sid`, over each of the past `weeks` weeks, as a band per metric.

    `baseline` is the snapshot the current one is scored against (compare's baseline -> current interval): on a
    cumulative engine its length is the hour every past window must cover and match; the snapshot before stands in
    when there is none. An interval engine's hour is the snapshot's own window.

    Returns None when fewer than MIN_WEEKS weeks are usable (or the band is switched off), else
    {label, hour, weekday, weeks, weeks_used, high_is, window_s, windows, skipped, metrics}, where each metric has
    {median, high, low, max, min, n, high_is}. The high mark is the 90th percentile from PERCENTILE_FROM weeks of
    that metric on, the max below.
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

    current = store.get_json(f"{current_sid}/summary.json")  # the current snapshot's own summary failing is the review's own failure
    current["id"] = current_sid
    interval_engine = current.get("metrics_kind") == "interval"

    def summary(sid: str) -> dict:
        s = store.get_json(f"{sid}/summary.json")
        s["id"] = sid
        return s

    # the hour being banded: the interval the review scores (the AWR window, or baseline -> current on cumulative counters)
    if interval_engine:
        cur_len = timedelta(seconds=interval_of(current).get("elapsed_s") or 3600)
    else:
        ref = baseline if baseline in times and times[baseline] < now else store.previous(engine, target, current_sid)
        cur_len = now - times[ref] if ref in times else timedelta(hours=1)

    def window_of(w: int, sid: str, at: datetime) -> tuple[dict | None, dict | None, str | None]:
        """One candidate snapshot's window for week w: (window, interval, why it cannot stand for the hour)."""
        try:
            if interval_engine:
                s = summary(sid)
                if (s.get("metrics_kind") or "cumulative") != "interval":  # read before Statspack or AWR was the source
                    return None, None, f"it was read as running totals ({s.get('oracle_source') or 'cumulative'}), not as a window"
                iv = interval_of(s)
                end = times[sid]
                if not iv.get("elapsed_s"):
                    return None, None, "the AWR window has no elapsed_s"
                # on the UTC timeline the band keeps: AWR's own begin is a naive timestamp on the database's clock
                start = end - timedelta(seconds=iv["elapsed_s"])
                window = {"week": w, "snapshot": sid, "start": start.isoformat(), "end": end.isoformat(), "elapsed_s": iv["elapsed_s"]}
                why = _restart_inside(s, start, end)
            else:
                prev = store.previous(engine, target, sid)
                if prev is None:
                    return None, None, "no snapshot before it to form an interval"
                a, b = summary(prev), summary(sid)
                if "interval" in (a.get("metrics_kind"), b.get("metrics_kind")):  # one end was a window, not running totals
                    return None, None, "one end of its interval was read as a window (AWR or Statspack), not as running totals"
                iv = interval_between(a, b)
                start, end = _ts(iv["start"], now), _ts(iv["end"], now)
                window = {"week": w, "snapshot": sid, "baseline": prev, "start": iv["start"], "end": iv["end"], "elapsed_s": iv["elapsed_s"]}
                why = f"counter reset inside the window (counters_since {b.get('counters_since')})" if iv.get("counters_reset") else None
        except (FileNotFoundError, KeyError, TypeError, ValueError, AttributeError) as exc:
            # one old snapshot with a missing or malformed summary voids its week, not the review
            return None, None, f"its summary cannot be read ({type(exc).__name__}: {exc})"
        return window, iv, why or _fits(start, end, at, cur_len)

    windows: list[dict] = []
    skipped: list[dict] = []
    rows: list[dict[str, float]] = []
    for w in range(1, weeks + 1):
        at = now - timedelta(days=7 * w)
        near = _candidates(times, at, tol, current_sid)
        if not near:
            skipped.append({"week": w, "reason": f"no snapshot within ±{hours_tolerance:g}h of {at.isoformat()}"})
            continue
        tried: list[tuple[str, str]] = []
        for sid in near:  # the nearest first; when its window is not the hour, a later one inside the tolerance may be
            window, iv, why = window_of(w, sid, at)
            if why is None:
                windows.append(window)
                rows.append(measures(iv))
                break
            tried.append((sid, why))
        else:
            sid, why = tried[0]
            skip = {"week": w, "snapshot": sid, "reason": why}
            if len(tried) > 1:
                skip["also_tried"] = [{"snapshot": s, "reason": r} for s, r in tried[1:]]
            skipped.append(skip)

    if len(rows) < MIN_WEEKS:
        return None
    n = len(rows)
    high_is = "p90" if n >= PERCENTILE_FROM else "max"
    metrics: dict[str, dict[str, Any]] = {}
    for name in sorted({k for r in rows for k in r}):
        vals = [r[name] for r in rows if name in r]
        if len(vals) < MIN_WEEKS:
            continue  # a metric only some weeks had (an engine upgrade, a wait class that came and went) has no band
        hi = "p90" if len(vals) >= PERCENTILE_FROM else "max"  # the metric's own weeks: seen in 3 of 6, it has a max, not a p90
        metrics[name] = {"median": round(_quantile(vals, 0.5), 4), "n": len(vals), "high_is": hi,
                         "high": round(_quantile(vals, 0.9) if hi == "p90" else max(vals), 4),
                         "low": round(_quantile(vals, 0.1) if hi == "p90" else min(vals), 4),
                         "max": round(max(vals), 4), "min": round(min(vals), 4)}
    label = f"{now:%A %H:%M}" + (" UTC" if now.utcoffset() == timedelta(0) else "")
    return {
        "label": label, "weekday": f"{now:%A}", "hour": f"{now:%H:%M}", "weeks": weeks, "weeks_used": n, "high_is": high_is,
        "window_s": round(_quantile([w["elapsed_s"] for w in windows], 0.5), 1),
        "windows": windows, "skipped": skipped, "metrics": metrics,
    }


# --------------------------------------------------------------------------- sentences
def weeks_phrase(band: dict, n: int | None = None) -> str:
    """"6 weeks", or "4 of 6 weeks" when some were skipped: the count is part of the claim. `n` is one metric's own
    count of weeks, which is lower when it was not in every window (a wait class that came and went)."""
    n, of = band["weeks_used"] if n is None else n, band["weeks"]
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
    weeks = weeks_phrase(band, m.get("n"))  # the metric's own weeks, not the band's: "in 3 of 6 weeks" for a wait class seen in three
    if percent:
        value, m, unit = value * 100, {k: v * 100 if k in MARKS else v for k, v in m.items()}, "%"
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
