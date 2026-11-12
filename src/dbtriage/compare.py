"""Turn two snapshots into "what changed", engine-agnostically.

Every snapshot summary is reduced to an *interval*: activity over a span of time.
  * cumulative engines (postgres, mysql, oracle without AWR): interval(a, b) = b - a
  * interval engines (oracle AWR): the snapshot already is an interval
Then diff(reference_interval, current_interval) ranks the changes and produces
heuristic findings. The agent reads this output through a tool; it never has to
do the arithmetic itself.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

GAUGES = {"sessions_total", "sessions_active", "sessions_blocked", "cache_hit_ratio", "locks_waiting",
          "elapsed_s", "avg_active_sessions", "page_life_expectancy_s"}  # SQL Server's page life expectancy is a level


CORRELATE_MINUTES = 30  # a change this long before the window still counts as beside it


def _is_gauge(name: str) -> bool:
    return name in GAUGES or name.endswith("_ratio")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _by_id(rows: list[dict], key: str = "sql_id") -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        k = str(r.get(key))
        if k in out:  # same sql_id with several plans (oracle, sql server): merge, remember both plans
            m = out[k]
            for f in ("calls", "total_time_ms", "rows", "io_blocks_hit", "io_blocks_read", "temp_blocks", "cpu_time_ms"):
                if r.get(f) is not None:
                    m[f] = (m.get(f) or 0) + r[f]
            m.setdefault("plans", {m.get("plan_hash")}).add(r.get("plan_hash"))
            if r.get("since") and (not m.get("since") or r["since"] < m["since"]):
                m["since"] = r["since"]  # the oldest plan's start: the statement's counters began then
        else:
            out[k] = dict(r)
    for m in out.values():
        if "plans" in m:
            m["plans"] = sorted(p for p in m["plans"] if p is not None)
    return out


SQL_FIELDS = ("calls", "total_time_ms", "rows", "io_blocks_hit", "io_blocks_read", "temp_blocks", "cpu_time_ms", "rows_examined")


def _by_plan(rows: list[dict]) -> dict[tuple[str, Any], dict]:
    """Statement rows keyed by (sql_id, plan_hash); rows repeating a key are summed."""
    out: dict[tuple[str, Any], dict] = {}
    for r in rows:
        k = (str(r.get("sql_id")), r.get("plan_hash"))
        if k in out:
            m = out[k]
            for f in SQL_FIELDS:
                if r.get(f) is not None:
                    m[f] = (m.get(f) or 0) + r[f]
            if r.get("since") and (not m.get("since") or r["since"] < m["since"]):
                m["since"] = r["since"]
        else:
            out[k] = dict(r)
    return out


def _sql_interval(rows_a: list[dict], rows_b: list[dict], reset: bool, subtract: bool = True) -> dict[str, dict]:
    """Each statement's activity between two snapshots. Each (statement, plan) is differenced on its own, then the
    plans are added up per statement: a plan that appears, or one that leaves the cache, then changes only its own
    share (merging first would subtract one plan's totals from another's). A plan the reference did not have is a
    plan change, reported against the plan that used the most time there.
    subtract=False: rows_b already are the interval's (Query Store), so nothing is subtracted; rows_a only says which
    plans the reference ran."""
    pa, pb = _by_plan(rows_a), _by_plan(rows_b)
    ref_plans: dict[str, dict[Any, float]] = {}
    for (sid, plan), r in pa.items():
        ref_plans.setdefault(sid, {})[plan] = r.get("total_time_ms") or 0
    out: dict[str, dict] = {}
    top: dict[str, float] = {}
    spent_by_plan: dict[str, dict[Any, float]] = {}
    for (sid, plan), row in pb.items():
        prev = None if reset or not subtract else pa.get((sid, plan))
        if prev is not None and prev.get("since") != row.get("since"):
            prev = None  # this plan's counters started over (sql server: it left the cache and came back)
        d = dict(row)
        for f in SQL_FIELDS:
            if row.get(f) is None:
                continue
            pv = (prev or {}).get(f) or 0
            d[f] = round(row[f] - pv, 3) if row[f] >= pv else row[f]
        if not (d.get("calls") or d.get("total_time_ms")):
            continue  # idle in this interval
        spent = d.get("total_time_ms") or 0
        spent_by_plan.setdefault(sid, {})[plan] = spent
        if sid not in out:
            d["_plans"] = {plan}
            out[sid], top[sid] = d, spent
            continue
        m = out[sid]
        for f in SQL_FIELDS:
            if d.get(f) is not None:
                m[f] = round((m.get(f) or 0) + d[f], 3)
        m["_plans"].add(plan)
        if spent > top[sid]:  # the plan that did most of the work names the statement's plan and text
            top[sid] = spent
            m["plan_hash"], m["text"] = plan, d.get("text", m.get("text"))
    for sid, d in out.items():
        plans = sorted(p for p in d.pop("_plans") if p is not None)
        if len(plans) > 1:
            d["plans"] = plans
        before = {p: t for p, t in ref_plans.get(sid, {}).items() if p is not None}
        new = {p: t for p, t in spent_by_plan.get(sid, {}).items() if p is not None and p not in before}
        if not reset and before and new:
            d["plan_changed_from"] = max(before, key=before.get)
            d["plan_changed_to"] = max(new, key=new.get)  # the new plan, which need not be the busiest one
        d["mean_time_ms"] = round(d["total_time_ms"] / d["calls"], 3) if d.get("calls") and d.get("total_time_ms") is not None else None
    return out


# --------------------------------------------------------------------------- intervals
def interval_of(summary: dict) -> dict:
    """An AWR-style snapshot already describes an interval."""
    lp = dict(summary["load_profile"])
    elapsed = lp.get("elapsed_s") or (summary.get("awr") or {}).get("elapsed_s") or 0
    return {
        "snapshot": summary.get("id"),
        "start": summary.get("counters_since"), "end": summary["captured_at"], "elapsed_s": elapsed,
        "load_profile": lp,
        "sql": _by_id(summary.get("top_sql", [])),
        "waits": _wait_shares(summary.get("wait_events", [])),
        "tables": {t["name"]: t for t in summary.get("tables", [])},
        "sessions": summary.get("sessions", []),
        "findings": summary.get("findings", []),
        "settings": summary.get("settings"),
    }


def _span_s(window: dict | None) -> float | None:
    try:
        return (_ts(window["end"]) - _ts(window["start"])).total_seconds() or None
    except (KeyError, TypeError, ValueError):
        return None


def interval_between(a: dict, b: dict) -> dict:
    """Delta of cumulative counters between snapshot a (earlier) and b (later).

    top_sql_kind = "interval" on b (SQL Server's Query Store) means its top SQL already is its window's: it is taken as
    it is, with a's top SQL only as the plans the reference ran, and the rates use the span the statements cover
    (top_sql_window) rather than the time between the snapshots. The load profile and waits are still differenced."""
    elapsed = (_ts(b["captured_at"]) - _ts(a["captured_at"])).total_seconds()
    reset = a.get("counters_since") != b.get("counters_since")
    la, lb = a["load_profile"], b["load_profile"]
    lp: dict[str, Any] = {"elapsed_s": elapsed}
    for k, vb in lb.items():
        va = la.get(k)
        if _is_gauge(k) or vb is None:
            lp[k] = vb
        elif va is None or reset or vb < va:
            reset = reset or (va is not None and vb < va)
            lp[k] = vb  # counters restarted: the later value is the whole interval
        else:
            lp[k] = round(vb - va, 3)
    hits, reads = lp.get("blks_hit") or 0, lp.get("blks_read") or 0
    if hits + reads:
        lp["cache_hit_ratio"] = round(hits / (hits + reads), 4)

    sql_kind = {"sql_kind": "interval", "sql_window": b.get("top_sql_window"),
                "sql_elapsed_s": _span_s(b.get("top_sql_window"))} if b.get("top_sql_kind") == "interval" else {}
    if sql_kind:  # a restart does not reset them, so plan changes are still told against the reference's plans
        sql = _sql_interval(a.get("top_sql", []), b.get("top_sql", []), False, subtract=False)
    else:  # a's top SQL is window figures and b's cumulative (Query Store turned off): nothing to subtract, as after a reset
        sql = _sql_interval(a.get("top_sql", []), b.get("top_sql", []), reset or a.get("top_sql_kind") == "interval")

    wa = {(w["class"], w["event"]): w for w in a.get("wait_events", [])}
    waits = []
    for w in b.get("wait_events", []):
        p = {} if reset else wa.get((w["class"], w["event"]), {})
        d = {"class": w["class"], "event": w["event"]}
        for f in ("count", "time_ms"):
            if w.get(f) is not None:
                d[f] = round(w[f] - (p.get(f) or 0), 3) if w[f] >= (p.get(f) or 0) else w[f]
        waits.append(d)

    ta = {t["name"]: t for t in a.get("tables", [])}
    tables = {}
    for t in b.get("tables", []):
        p = {} if reset else ta.get(t["name"], {})
        d = dict(t)
        for f in ("seq_scan", "seq_tup_read", "idx_scan", "reads", "writes", "fetches", "time_ms"):
            if t.get(f) is not None:
                d[f] = t[f] - (p.get(f) or 0) if t[f] >= (p.get(f) or 0) else t[f]
        tables[t["name"]] = d

    return {
        "snapshot": b.get("id"), "baseline": a.get("id"),
        "start": a["captured_at"], "end": b["captured_at"], "elapsed_s": elapsed, "counters_reset": reset,
        "load_profile": lp, "sql": sql, **sql_kind, "waits": _wait_shares(waits),
        "tables": tables, "sessions": b.get("sessions", []), "findings": b.get("findings", []),
        # both ends: a parameter changed inside this window is the change that sits beside its regression
        "settings": b.get("settings"), "settings_before": a.get("settings"),
    }


def _wait_shares(waits: list[dict]) -> dict:
    """Share of wait time (or count) per class, plus the top events."""
    metric = "time_ms" if any(w.get("time_ms") is not None for w in waits) else "count"
    per_class: dict[str, float] = {}
    for w in waits:
        per_class[w["class"]] = per_class.get(w["class"], 0) + (w.get(metric) or 0)
    total = sum(per_class.values()) or 1
    return {
        "metric": metric,
        "classes": {c: {"value": round(v, 1), "share": round(v / total, 3)} for c, v in
                    sorted(per_class.items(), key=lambda kv: -kv[1])},
        "top_events": sorted(waits, key=lambda w: -(w.get(metric) or 0))[:10],
    }


# --------------------------------------------------------------------------- diff + findings
def _rate(v, elapsed):
    return round(v / elapsed, 3) if v is not None and elapsed else None


def _pct(before, after):
    if before in (None, 0) or after is None:
        return None
    return round((after - before) / abs(before) * 100, 1)


def diff(ref: dict | None, cur: dict, changes: list[dict] | None = None, correlate_minutes: int = CORRELATE_MINUTES,
         band: dict | None = None) -> dict:
    """Compare the current interval with a reference interval (same hour last week,
    the previous run, the pre-deploy snapshot ...). ref may be None: then only the
    current interval is described and findings are 'what is heavy now'.
    `changes` is the snapshot's stored change feed; the ones in or just before the window are evidence.
    `band` is the target's usual band for this hour (dbtriage.band.usual_band): a sentence in the evidence of the
    findings about a banded metric, and out["band"] for the page. The reference stays the narrative baseline."""
    out: dict[str, Any] = {
        "current": {"snapshot": cur.get("snapshot"), "start": cur["start"], "end": cur["end"], "elapsed_s": cur["elapsed_s"],
                    "counters_reset": cur.get("counters_reset", False)},
        "reference": None if ref is None else {"snapshot": ref.get("snapshot"), "start": ref["start"], "end": ref["end"],
                                               "elapsed_s": ref["elapsed_s"]},
        "load_profile": {}, "sql": {"new": [], "gone": [], "changed": [], "top": []},
        "waits": {"current": cur["waits"]["classes"], "reference": ref["waits"]["classes"] if ref else None, "shifts": []},
        "tables": [], "sessions": cur.get("sessions", []), "engine_findings": cur.get("findings", []),
        "settings_changed": [], "changes": [], "band": None, "findings": [],
    }
    for side, iv in (("current", cur), ("reference", ref)):
        if iv and iv.get("sql_window"):  # Query Store: the statements cover their intervals, not the snapshots' span
            out[side]["sql_window"] = iv["sql_window"]
    findings: list[dict] = out["findings"]
    ec, er = cur["elapsed_s"], (ref or {}).get("elapsed_s")
    ecs, ers = cur.get("sql_elapsed_s") or ec, (ref or {}).get("sql_elapsed_s") or er  # what statement rates are over

    # load profile: compare rates so unequal window lengths are fair
    for k, v in cur["load_profile"].items():
        rv = (ref or {}).get("load_profile", {}).get(k)
        if _is_gauge(k) or k == "elapsed_s":
            out["load_profile"][k] = {"reference": rv, "current": v, "pct": _pct(rv, v)}
        else:
            out["load_profile"][k] = {"reference_per_s": _rate(rv, er), "current_per_s": _rate(v, ec),
                                      "reference": rv, "current": v, "pct": _pct(_rate(rv, er), _rate(v, ec))}

    # sql
    total_cur = sum((s.get("total_time_ms") or 0) for s in cur["sql"].values()) or 1
    total_ref = sum((s.get("total_time_ms") or 0) for s in (ref or {}).get("sql", {}).values()) or 1
    for sid, s in sorted(cur["sql"].items(), key=lambda kv: -(kv[1].get("total_time_ms") or 0)):
        share = (s.get("total_time_ms") or 0) / total_cur
        row = {"sql_id": sid, "text": s.get("text"), "share_of_time": round(share, 3),
               "calls": s.get("calls"), "total_time_ms": s.get("total_time_ms"), "mean_time_ms": s.get("mean_time_ms"),
               "cpu_time_ms": s.get("cpu_time_ms"), "rows": s.get("rows"), "io_blocks_read": s.get("io_blocks_read"), "temp_blocks": s.get("temp_blocks"),
               "plan_hash": s.get("plan_hash"), "plans": s.get("plans")}
        if s.get("plan_changed_from"):
            row["plan_changed_from"] = s["plan_changed_from"]
            row["plan_changed_to"] = s.get("plan_changed_to")
        out["sql"]["top"].append(row)
        r = (ref or {}).get("sql", {}).get(sid) if ref else None
        if ref is not None and r is None:
            out["sql"]["new"].append(row)
            if share >= 0.10:
                findings.append(_f(40 + share * 60, "sql_new", f"New heavy SQL {sid} takes {share:.0%} of SQL time",
                                   row["text"], {"sql_id": sid, "share_of_time": row["share_of_time"], "mean_time_ms": row["mean_time_ms"]}))
            continue
        if r is not None:
            ch = dict(row, reference={"calls": r.get("calls"), "total_time_ms": r.get("total_time_ms"),
                                      "mean_time_ms": r.get("mean_time_ms"), "cpu_time_ms": r.get("cpu_time_ms"), "plan_hash": r.get("plan_hash"),
                                      "share_of_time": round((r.get("total_time_ms") or 0) / total_ref, 3)})
            ch["mean_pct"] = _pct(r.get("mean_time_ms"), s.get("mean_time_ms"))
            ch["calls_pct"] = _pct(_rate(r.get("calls"), ers), _rate(s.get("calls"), ecs))
            ch["plan_changed"] = (r.get("plan_hash") is not None and s.get("plan_hash") is not None
                                  and r.get("plan_hash") != s.get("plan_hash")) or bool(s.get("plan_changed_from"))
            out["sql"]["changed"].append(ch)
            if share >= 0.05 and (ch["mean_pct"] or 0) >= 50:
                why = " (execution plan changed)" if ch["plan_changed"] else ""
                findings.append(_f(35 + share * 50 + min(ch["mean_pct"], 300) / 10, "sql_regressed",
                                   f"SQL {sid} is {ch['mean_pct']:.0f}% slower per call{why}, {share:.0%} of SQL time",
                                   row["text"], {"sql_id": sid, "mean_before_ms": r.get("mean_time_ms"),
                                                 "mean_after_ms": s.get("mean_time_ms"), "plan_changed": ch["plan_changed"]}))
            elif share >= 0.10 and (ch["calls_pct"] or 0) >= 100:
                findings.append(_f(30 + share * 40, "sql_volume",
                                   f"SQL {sid} runs {ch['calls_pct']:.0f}% more often, {share:.0%} of SQL time",
                                   row["text"], {"sql_id": sid, "calls_per_s_before": _rate(r.get("calls"), ers),
                                                 "calls_per_s_after": _rate(s.get("calls"), ecs)}))
            elif ch["plan_changed"] and share >= 0.02:
                findings.append(_f(30, "plan_change", f"SQL {sid} changed execution plan", row["text"],
                                   # before: the reference interval's plan; after: the new plan when one appeared
                                   {"sql_id": sid, "plan_before": r.get("plan_hash"),
                                    "plan_after": s.get("plan_changed_to") or s.get("plan_hash")}))
    if ref is not None:
        for sid, r in ref["sql"].items():
            if sid not in cur["sql"] and (r.get("total_time_ms") or 0) / total_ref >= 0.05:
                out["sql"]["gone"].append({"sql_id": sid, "text": r.get("text"),
                                           "reference_share": round(r["total_time_ms"] / total_ref, 3)})
    if ref is None and out["sql"]["top"]:
        top = out["sql"]["top"][0]
        findings.append(_f(20 + top["share_of_time"] * 40, "sql_top",
                           f"SQL {top['sql_id']} dominates with {top['share_of_time']:.0%} of SQL time (no baseline to compare)",
                           top["text"], {"sql_id": top["sql_id"], "mean_time_ms": top["mean_time_ms"]}))

    # waits
    if ref is not None:
        for cls, c in cur["waits"]["classes"].items():
            rs = ref["waits"]["classes"].get(cls, {}).get("share", 0)
            delta = c["share"] - rs
            out["waits"]["shifts"].append({"class": cls, "reference_share": rs, "current_share": c["share"], "delta": round(delta, 3)})
            if delta >= 0.15 and cls.lower() not in ("cpu", "on cpu"):
                top = [e for e in cur["waits"]["top_events"] if e["class"] == cls][:3]
                findings.append(_f(40 + delta * 100, "wait_shift",
                                   f"Wait class '{cls}' rose from {rs:.0%} to {c['share']:.0%} of wait time",
                                   ", ".join(e["event"] for e in top), {"class": cls, "top_events": top}))
        out["waits"]["shifts"].sort(key=lambda s: -abs(s["delta"]))

    # load profile heuristics
    lp = out["load_profile"]

    def cur_v(k):
        return (lp.get(k) or {}).get("current")

    def ref_v(k):
        return (lp.get(k) or {}).get("reference")

    if ref is not None and ref_v("cache_hit_ratio") and cur_v("cache_hit_ratio") is not None \
            and ref_v("cache_hit_ratio") - cur_v("cache_hit_ratio") >= 0.02:
        findings.append(_f(45, "cache_hit_drop", f"Cache hit ratio fell from {ref_v('cache_hit_ratio'):.1%} to {cur_v('cache_hit_ratio'):.1%}",
                           "more physical reads: check new seq scans, plan changes or a larger working set",
                           {"before": ref_v("cache_hit_ratio"), "after": cur_v("cache_hit_ratio")}))
    if (cur_v("deadlocks") or 0) > 0:
        findings.append(_f(60, "deadlocks", f"{cur_v('deadlocks')} deadlock(s) in the window", "", {"deadlocks": cur_v("deadlocks")}))
    for k, label in (("temp_bytes", "temp file bytes"), ("temp_files", "temp files"), ("tmp_disk_tables", "on-disk temp tables"),
                     ("sorts_disk", "disk sorts"), ("row_lock_waits", "row lock waits"), ("hard_parses", "hard parses")):
        p = (lp.get(k) or {}).get("pct")
        if ref is not None and p is not None and p >= 200 and (cur_v(k) or 0) > 0:
            findings.append(_f(35 + min(p, 1000) / 50, "load_" + k, f"{label} up {p:.0f}% per second vs reference", "",
                               {"metric": k, "reference_per_s": lp[k]["reference_per_s"], "current_per_s": lp[k]["current_per_s"]}))
    p = (lp.get("xact_commit") or {}).get("pct")
    if ref is not None and p is not None and p <= -50:
        findings.append(_f(30, "throughput_drop", f"Commit rate down {abs(p):.0f}% vs reference", "app quieter, or the database is stalling",
                           {"reference_per_s": lp["xact_commit"]["reference_per_s"], "current_per_s": lp["xact_commit"]["current_per_s"]}))

    # sessions and tables
    blocked = [s for s in cur.get("sessions", []) if s.get("blocked_by")]
    long = [s for s in cur.get("sessions", []) if (s.get("duration_s") or 0) >= 60]
    if blocked:
        findings.append(_f(65, "blocking", f"{len(blocked)} session(s) blocked right now", "", {"sessions": blocked[:5]}))
    if long:
        findings.append(_f(40, "long_running", f"{len(long)} session(s) active for over a minute", "", {"sessions": long[:5]}))
    if (cur_v("locks_waiting") or 0) > 0:
        findings.append(_f(50, "lock_waits", f"{cur_v('locks_waiting')} lock(s) waiting right now", "", {}))

    for name, t in cur.get("tables", {}).items():
        r = (ref or {}).get("tables", {}).get(name, {}) if ref else {}
        row = {"name": name, **{k: v for k, v in t.items() if k != "name"}}
        if ref is not None and r:
            row["seq_scan_pct"] = _pct(_rate(r.get("seq_scan"), er), _rate(t.get("seq_scan"), ec))
        out["tables"].append(row)
        live, dead = t.get("live_rows") or 0, t.get("dead_rows") or 0
        if live >= 10_000 and dead >= 0.2 * live:
            findings.append(_f(35, "bloat", f"{name}: {dead:,} dead rows vs {live:,} live (vacuum lagging)", "",
                               {"table": name, "dead_rows": dead, "live_rows": live, "last_vacuum": t.get("last_vacuum")}))
        if t.get("stale_stats"):
            findings.append(_f(25, "stale_stats", f"{name}: optimizer statistics are stale", "", {"table": name}))
        if ref is not None and (row.get("seq_scan_pct") or 0) >= 100 and live >= 100_000 and (t.get("seq_scan") or 0) >= 10:
            findings.append(_f(40, "seq_scans", f"{name}: sequential scans up {row['seq_scan_pct']:.0f}% on a {live:,}-row table", "",
                               {"table": name, "seq_scan": t.get("seq_scan"), "idx_scan": t.get("idx_scan")}))

    seen_engine = set()
    for f in cur.get("findings", []):  # engine-native findings (ADDM) get a seat at the table
        kind, message = "engine_" + f.get("type", "finding").lower(), f.get("message") or f.get("finding")
        if (kind, message) in seen_engine:  # one ADDM task per AWR interval repeats the same sentence; say it once
            continue
        seen_engine.add((kind, message))
        impact = f.get("impact_pct") or 0
        findings.append(_f(20 + impact * 0.6, kind, message, f.get("detail", ""), {"engine_finding": f}))

    # settings: the start of the current window against its end (cumulative engines carry both ends), else the
    # reference snapshot against the current one. A change here is low on its own, and it is written into the
    # evidence of every regression in the same window so the correlation travels with the finding.
    before = cur.get("settings_before")
    if before is None and ref is not None:
        before = ref.get("settings")
    changed = settings_changed(before, cur.get("settings"))
    out["settings_changed"] = changed
    for c in changed[:SETTINGS_FINDINGS_CAP]:
        findings.append(_f(20, "setting_changed", f"parameter {c['name']} changed from {_show(c['before'])} to {_show(c['after'])}", "",
                           {"parameter": c["name"], "before": c["before"], "after": c["after"]}))
    if len(changed) > SETTINGS_FINDINGS_CAP:
        rest = [c["name"] for c in changed[SETTINGS_FINDINGS_CAP:]]
        findings.append(_f(20, "setting_changed", f"{len(rest)} more settings changed", ", ".join(rest), {"parameters": rest}))
    if changed:
        names = [c["name"] for c in changed]
        for f in findings:
            if f["kind"] in CORRELATED_KINDS:
                f["evidence"]["settings_changed"] = names

    # the change feed: a deploy inside the window, or just before it, is written into the same findings as the
    # settings change, so the review can say "plan changed; billing@1.4.2 landed 6 minutes earlier" with both ids
    near = changes_near(changes, cur.get("start"), cur["end"], cur.get("elapsed_s"), correlate_minutes)
    out["changes"] = near
    if near:
        brief = [{"time": c["time"], "kind": c["kind"], "description": c["description"]} for c in near]
        for f in findings:
            if f["kind"] in CORRELATED_KINDS or f["kind"] == "sql_new":  # a new statement is what a deploy brings
                f["evidence"]["changes"] = brief

    if band:
        out["band"] = attach_band(band, cur, findings)

    findings.sort(key=lambda f: -f["score"])
    out["severity"] = findings[0]["severity"] if findings else "info"
    return out


BAND_WINDOW_RATIO = 2  # a window this many times longer or shorter than the band's is not the same hour


def attach_band(band: dict, cur: dict, findings: list[dict]) -> dict:
    """The current interval against the usual band: a position per metric, one sentence for the whole interval (total
    SQL time), and the sentence for its own metric written into each throughput, load, wait-shift and regression
    finding. A current window much longer than the band's windows (a week-long interval under same-time-last-week)
    gets no sentences, only the note why: a rate over a week is not a rate over an hour."""
    from dbtriage import band as usual

    info = {k: band.get(k) for k in ("label", "weeks", "weeks_used", "high_is", "window_s", "skipped")}
    ec, bw = cur.get("elapsed_s") or 0, band.get("window_s") or 0
    if not ec or not bw or not 1 / BAND_WINDOW_RATIO <= ec / bw <= BAND_WINDOW_RATIO:
        info.update(applies=False, note=f"the current window is {ec / 3600:.1f}h long and the band is built from "
                                        f"{bw / 3600:.1f}h windows, so they are not compared")
        return info
    now = usual.measures(cur)
    info["applies"] = True
    info["metrics"] = {k: {"current": now[k], "position": usual.position(band, k, now[k]), **m}
                       for k, m in band["metrics"].items() if k in now}
    info["sentence"] = usual.sentence(band, "sql_time_ms", now.get("sql_time_ms"), "total SQL time", " ms/s")
    info["above"] = usual.position(band, "sql_time_ms", now.get("sql_time_ms")) in ("above", "above_all")
    for f in findings:
        k, e = f["kind"], f["evidence"]
        if k == "throughput_drop":
            line = usual.sentence(band, "xact_commit", now.get("xact_commit"), "commits", "/s")
        elif k.startswith("load_"):
            line = usual.sentence(band, k[5:], now.get(k[5:]), k[5:], "/s")
        elif k == "wait_shift" and e.get("class"):
            line = usual.sentence(band, f"wait_share:{e['class']}", now.get(f"wait_share:{e['class']}"),
                                  f"wait class '{e['class']}'", percent=True)
        elif k == "sql_regressed":
            line = info["sentence"]
        else:
            continue
        if line:
            e["band"] = line
    return info


SETTINGS_FINDINGS_CAP = 5  # one finding per changed parameter up to here, then a single "N more" line
CORRELATED_KINDS = ("sql_regressed", "plan_change", "wait_shift")  # the regressions a parameter change can explain
CHANGE_KINDS = ("deploy", "parameter", "job", "failover")  # feed kinds that are evidence; "other" holds notes and feed errors


def changes_near(changes: list[dict] | None, start: str | None, end: str, elapsed_s: float | None, margin_minutes: int) -> list[dict]:
    """Feed items dated inside the window, or up to margin_minutes before it, oldest first. The window's start is the
    interval's start, or elapsed_s before its end when the snapshot does not carry one (an AWR window without a begin)."""
    if not changes:
        return []
    from datetime import timedelta

    t_end = _ts(end)
    t_start = _ts(start) if start else t_end - timedelta(seconds=elapsed_s or 0)
    if t_start.tzinfo is None:
        t_start = t_start.replace(tzinfo=t_end.tzinfo)
    lo = t_start - timedelta(minutes=margin_minutes)
    out = []
    for c in changes:
        if not isinstance(c, dict) or c.get("kind") not in CHANGE_KINDS:
            continue
        try:
            t = _ts(c["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=t_end.tzinfo)
        if lo <= t <= t_end:
            out.append(c)
    return sorted(out, key=lambda c: c["time"])


def settings_changed(before: dict | None, after: dict | None) -> list[dict]:
    """Parameters whose value differs between two snapshots: added, removed or changed, compared as stripped strings
    so 100 and "100" agree. Nothing is excluded on purpose: every key the collectors store under settings (postgres
    pg_settings, mysql global variables, oracle v$parameter, sql server sys.configurations plus database options,
    cpu_count and physical_memory_mb) is configuration, a version or a host size, and any of them changing is worth
    a line. Counters and timestamps live in load_profile, never here."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []

    def norm(v):
        return None if v is None else str(v).strip()

    return [{"name": k, "before": before.get(k), "after": after.get(k)}
            for k in sorted(set(before) | set(after)) if norm(before.get(k)) != norm(after.get(k))]


def _show(v) -> str:
    return "unset" if v is None else str(v)


def _f(score: float, kind: str, title: str, detail: str, evidence: dict) -> dict:
    score = max(0, min(100, round(score)))
    sev = "high" if score >= 70 else "medium" if score >= 40 else "low"
    return {"score": score, "severity": sev, "kind": kind, "title": title, "detail": detail or "", "evidence": evidence}


def compare(current: dict, baseline: dict | None, before_baseline: dict | None = None, changes: list[dict] | None = None,
            correlate_minutes: int = CORRELATE_MINUTES, band: dict | None = None) -> dict:
    """Entry point used by the CLI and the agent tool.

    cumulative engines: current interval = baseline->current, reference = before_baseline->baseline
    interval engines:   current interval = current, reference = baseline
    `changes` is the current snapshot's stored change feed (changes.json), when it has one.
    `band` is the target's usual band for this hour (dbtriage.band.usual_band), when it has enough history.
    """
    if current.get("metrics_kind") == "interval":
        return diff(interval_of(baseline) if baseline else None, interval_of(current), changes, correlate_minutes, band)
    if baseline is None:
        raise ValueError("cumulative engine needs a baseline snapshot to form an interval")
    cur = interval_between(baseline, current)
    ref = interval_between(before_baseline, baseline) if before_baseline else None
    return diff(ref, cur, changes, correlate_minutes, band)
