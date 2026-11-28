"""What Second Look measured about a finding, so a review can be checked against the data before it is recorded.

A review's evidence is the model's own text, and a model can misread a tool: a running total since the statistics were
reset taken for the window's change, a "from 0" that no tool said. Before a review with findings is recorded,
submit_review hands back a check sheet: for each finding, the numbers Second Look computed for the thing it names (the
window's counts and rates, the running totals at both ends, its share of the database's SQL time) and how busy the
database was, plus any number in the evidence that no tool result shows. The model corrects what disagrees and submits
once more. The measured lines are also stored with each finding as `measured`, so a reader sees them beside the claim
whatever the model did with them.

Pure functions over a compare.diff result and the two summaries: no storage, no model.
"""
from __future__ import annotations

import re

NOT_FOUND = "Second Look has no statement, table, wait class, parameter, load metric or session named {!r} in this snapshot."
RULES = ("Every number in the evidence must match the measured values or a tool result for the same subject and window: a "
         "running total is not a change, and \"from X to Y\" needs both numbers measured. Rewrite evidence that disagrees "
         "and drop a finding the measurements do not support. Then set severity by the finding's impact on this database "
         "at the load above. High only when callers are affected now: a per-call time a request would notice, a large share "
         "of a busy database's time, a resource near its limit. A change that costs this database almost nothing is an "
         "efficiency issue: low, or not a finding. Say in impact which it is.")


def _g(v) -> str:
    """A number as a person reads it: thousands separated, three significant figures below 1000."""
    if v is None:
        return "?"
    if isinstance(v, bool):
        return str(v)
    v = float(v)
    if 0 < abs(v) < 0.001:
        return f"{v:.2g}"
    if v == int(v) and abs(v) >= 1:
        return f"{int(v):,}"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.3g}"


def _calls(n) -> str | None:
    return None if n is None else f"{_g(n)} call{'' if n == 1 else 's'}"


def _ms(v) -> str:
    if v is None:
        return "?"
    return f"{v / 60000:.1f} min" if v >= 60000 else f"{v / 1000:.2f} s" if v >= 1000 else f"{_g(v)} ms"


def _pick(subject: str, names) -> str | None:
    """The name a finding means: exact (any case), else the one unqualified match (the reports print
    _timescaledb_internal._hyper_24_1242_chunk; the model may write _hyper_24_1242_chunk)."""
    s = subject.strip().strip('"`').lower()
    names = list(names)
    for n in names:
        if str(n).lower() == s:
            return n
    tail = [n for n in names if str(n).lower().rpartition(".")[2].strip('"') == s.rpartition(".")[2]]
    return tail[0] if len(tail) == 1 else None


def measured(finding: dict, comparison: dict | None, current: dict, baseline: dict | None) -> list[str]:
    """One to three sentences of what Second Look measured for the finding's subject; empty when it names nothing."""
    subject = str(finding.get("subject") or "").strip()
    if not subject:
        return []
    if comparison is None:  # a cumulative engine's first snapshot: totals only, no window
        return _totals_only(subject, current) or [NOT_FOUND.format(subject)]
    for look in (_sql, _table, _wait, _parameter, _metric, _session):
        if lines := look(subject, comparison, current, baseline):
            return lines
    return [NOT_FOUND.format(subject) + _nearest(subject, comparison, current)]


def _nearest(subject: str, c: dict, current: dict) -> str:
    """" The nearest is ...": a mistyped sql id or table name, said so the reader (and the model) can see it."""
    import difflib

    names = [str(r.get("sql_id")) for r in (c.get("sql") or {}).get("top") or []] + [str(t.get("name")) for t in c.get("tables") or []]
    names += [str(k) for k in ((c.get("waits") or {}).get("current") or {})] + [str(k) for k in current.get("settings") or {}]
    close = difflib.get_close_matches(subject, names, n=1, cutoff=0.85)
    return f" The nearest is {close[0]}." if close else ""


def _sql(subject: str, c: dict, current: dict, baseline: dict | None) -> list[str]:
    sql = c.get("sql") or {}
    top = {str(r.get("sql_id")): r for r in sql.get("top") or []}
    sid = subject if subject in top else _pick(subject, top)
    if sid is None:
        return []
    r = top[sid]
    parts = [_calls(r.get("calls")),
             f"{_ms(r.get('total_time_ms'))} in all" + (f" ({_g(r['ms_per_s'])} ms of SQL per second, {r['share_of_time']:.1%} of the top SQL's time)"
                                                       if r.get("ms_per_s") is not None and r.get("share_of_time") is not None else "")
             if r.get("total_time_ms") is not None else None,
             f"{_ms(r.get('mean_time_ms'))} per call" if r.get("mean_time_ms") is not None else None]
    lines = [f"SQL {sid} in this window: " + ", ".join(p for p in parts if p) + "."]
    ch = next((x for x in sql.get("changed") or [] if str(x.get("sql_id")) == sid), None)
    if ch:
        ref = ch.get("reference") or {}
        was = [_calls(ref.get("calls")),
               f"{_ms(ref.get('mean_time_ms'))} per call" if ref.get("mean_time_ms") is not None else None,
               f"{ref['share_of_time']:.1%} of its top SQL's time" if ref.get("share_of_time") is not None else None]
        change = [f"per call {ch['mean_pct']:+.0f}%" if ch.get("mean_pct") is not None else None,
                  f"calls per second {ch['calls_pct']:+.0f}%" if ch.get("calls_pct") is not None else None,
                  f"{ch['added_ms_per_s']:+.3g} ms of SQL per second" if ch.get("added_ms_per_s") is not None else None,
                  "plan changed" if ch.get("plan_changed") else None]
        line = "Reference window: " + ", ".join(p for p in was if p) + "."
        if any(change):
            line += " Change: " + ", ".join(p for p in change if p) + "."
        lines.append(line)
    elif any(str(x.get("sql_id")) == sid for x in sql.get("new") or []):
        lines.append("Not in the reference window's top SQL.")
    return lines


TABLE_COUNTS = ("seq_scan", "seq_tup_read", "idx_scan", "reads", "writes", "fetches")


def _table(subject: str, c: dict, current: dict, baseline: dict | None) -> list[str]:
    rows = {t.get("name"): t for t in c.get("tables") or []}
    name = _pick(subject, rows)
    if name is None:
        return []
    t, window = rows[name], (c.get("current") or {}).get("elapsed_s")
    counts = [(k, t[k]) for k in TABLE_COUNTS if t.get(k) is not None]
    lines = []
    if counts:
        lines.append(f"{name} in this window: " + ", ".join(f"{k} {_g(v)}" + (f" ({_g(v / window)}/s)" if window and v else "") for k, v in counts) + ".")
    if current.get("metrics_kind") != "interval":  # the summaries hold running totals: show both ends, labelled
        now = next((x for x in current.get("tables") or [] if x.get("name") == name), {})
        then = next((x for x in (baseline or {}).get("tables") or [] if x.get("name") == name), {})
        totals = [f"{k} {_g(then[k])} at the baseline, {_g(now[k])} now" if then.get(k) is not None else f"{k} {_g(now[k])} now"
                  for k, _ in counts if now.get(k) is not None]
        if totals:
            lines.append("Running totals since the statistics were last reset, not changes: " + "; ".join(totals) + ".")
    size = [f"{_g(t['live_rows'])} live rows" if t.get("live_rows") is not None else None,
            f"{_g(t['dead_rows'])} dead" if t.get("dead_rows") is not None else None]
    if any(size):
        lines.append("Size: " + ", ".join(p for p in size if p) + ".")
    return lines


def _wait(subject: str, c: dict, current: dict, baseline: dict | None) -> list[str]:
    w = c.get("waits") or {}
    now = w.get("current") or {}
    cls = _pick(subject, now)
    if cls is None:
        return []
    ref = (w.get("reference") or {}).get(cls)
    line = f"Wait class {cls}: {now[cls].get('share', 0):.0%} of the window's wait time"
    if ref is not None:
        line += f", {ref.get('share', 0):.0%} in the reference window"
    elif w.get("reference") is not None:
        line += ", absent from the reference window"
    return [line + "."]


def _parameter(subject: str, c: dict, current: dict, baseline: dict | None) -> list[str]:
    changed = {s.get("name"): s for s in c.get("settings_changed") or []}
    if name := _pick(subject, changed):
        s = changed[name]
        return [f"Parameter {name} changed in this window: {s.get('before')} to {s.get('after')}."]
    settings = current.get("settings") or {}
    if name := _pick(subject, settings):
        return [f"Parameter {name} = {settings[name]}" + (", unchanged in this window." if c.get("reference") is not None or baseline else ".")]
    return []


def _metric(subject: str, c: dict, current: dict, baseline: dict | None) -> list[str]:
    lp = c.get("load_profile") or {}
    key = _pick(subject, lp)
    if key is None:
        return []
    m = lp[key]
    if "current_per_s" not in m:  # a gauge: a level, not a count
        return [f"{key}: {_g(m.get('current'))} now" + (f", {_g(m.get('reference'))} in the reference window" if m.get("reference") is not None else "") + "."]
    line = f"{key}: {_g(m.get('current'))} in this window ({_g(m.get('current_per_s'))}/s)"
    if m.get("reference") is not None:
        line += f", {_g(m.get('reference'))} in the reference window ({_g(m.get('reference_per_s'))}/s)"
    return [line + "."]


SESSION_KEYS = ("pid", "sid", "session_id", "spid")
SESSION_SHOWN = ("state", "wait_class", "wait_event", "duration_s", "blocked_by", "sql_id")


def _session(subject: str, c: dict, current: dict, baseline: dict | None) -> list[str]:
    for s in c.get("sessions") or []:
        if any(str(s.get(k)) == subject for k in SESSION_KEYS if s.get(k) is not None):
            shown = [f"{k} {s[k]}" for k in SESSION_SHOWN if s.get(k) not in (None, "")]
            return [f"Session {subject} at capture: " + (", ".join(shown) if shown else "no detail") + "."]
    return []


def _totals_only(subject: str, current: dict) -> list[str]:
    for q in current.get("top_sql") or []:
        if str(q.get("sql_id")) == subject:
            return [f"SQL {subject}, running totals since the statistics were last reset (no baseline, so no window): "
                    f"{_g(q.get('calls'))} calls, {_ms(q.get('total_time_ms'))} in all, {_ms(q.get('mean_time_ms'))} per call."]
    tables = {t.get("name"): t for t in current.get("tables") or []}
    if name := _pick(subject, tables):
        t = tables[name]
        counts = ", ".join(f"{k} {_g(t[k])}" for k in TABLE_COUNTS if t.get(k) is not None)
        return [f"{name}, running totals since the statistics were last reset (no baseline, so no window): {counts or 'none'}."]
    return []


# --------------------------------------------------------------------------- numbers no tool showed
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
SCALES = (1, 100, 0.01, 1000, 0.001, 1 / 60000)  # as given; a ratio as a percent; ms as s, s as ms; ms as min


def _numbers(text: str) -> list[tuple[str, float, int]]:
    out = []
    for m in NUMBER.finditer(text or ""):
        s = m.group().replace(",", "")
        try:
            out.append((m.group(), float(s), len(s.partition(".")[2])))
        except ValueError:
            continue
    return out


def pool(texts: list[str]) -> set[float]:
    """Every number in the texts the model read: the tool results of the run and the measured lines."""
    return {abs(v) for t in texts for _, v, _ in _numbers(t)}


def unseen(evidence: list[str], seen: set[float], limit: int = 5) -> list[str]:
    """Numbers cited in the evidence that no tool result shows at the precision cited, after the conversions a person
    makes (a ratio as a percent, milliseconds as seconds or minutes). Small counts are left out: they are everywhere."""
    out: list[str] = []
    rounded: dict[int, set[float]] = {}  # the pool at each precision cited, built once
    for text, v, places in (n for e in evidence for n in _numbers(e)):
        v = abs(v)
        if v <= 10 or text in out:
            continue
        if places not in rounded:
            rounded[places] = {round(y * k, places) for y in seen for k in SCALES}
        if round(v, places) not in rounded[places]:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def sheet(findings: list[dict], facts: list[list[str]], missing: list[list[str]], load_sentence: str = "") -> str:
    """The text submit_review returns the first time: each finding beside what was measured for it, then the rules."""
    out = ["Held, not final yet. Check each finding against what Second Look measured, then call submit_review once more "
           "with the corrected review (the same one if every finding holds). This check runs once."]
    if load_sentence:
        out.append(f"How busy the database was: {load_sentence}")
    for i, (f, lines, nums) in enumerate(zip(findings, facts, missing), 1):
        out.append(f"\nFinding {i} ({f.get('severity')}) {f.get('subject') or f.get('where') or ''}: {f.get('summary', '')}")
        out += [f"  your evidence: {e}" for e in f.get("evidence") or []]
        out += [f"  measured: {line}" for line in lines] or ["  measured: nothing; the finding names no subject"]
        if nums:
            out.append(f"  in your evidence but in no tool result: {', '.join(nums)}. Use the measured value, or say what it was calculated from.")
    out.append("\n" + RULES)
    return "\n".join(out)
