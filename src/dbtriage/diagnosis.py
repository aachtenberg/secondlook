"""The diagnosis block at the top of a database's page: five sentences from stored data, no model.

A person opening ten databases in a row scans, so the health picture cannot sit behind a button or a model call.
Each line is written by rules from what the store already holds (the latest review and comparison, the ledger of
open findings, the change feed, the schedule), so it is instant, costs nothing, is the same for everyone, and is
still there with the model off. The model's part is optional: the same five lines become the question the "Ask"
button sends, so the prose it writes starts from the facts the page shows.

    health     the latest review's severity and title, how old it is, and whether the interval sat inside the
               usual band for that hour (bet 7) when a band was stored with the comparison
    open       the ledger: how many findings are open, the worst, the oldest, and their tickets
    changed    the most recent deploy or setting change the feed saw, and whether a finding opened after it
    trend      opened and cleared over the last seven days, and rules findings the model left out of the review
    next       the schedule: the next run, paused, or that the last scheduled run failed
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbtriage.findings import rules_dropped

RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
WEEK = timedelta(days=7)
MAX_QUESTION = 1900  # chat.MAX_QUESTION is 2000; leave room for the framing sentence


def _when(iso: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ago(iso: str | None, now: datetime) -> str:
    """"3 min ago", "2 h ago", "4 days ago"; empty when the time is unreadable."""
    t = _when(iso)
    if t is None:
        return ""
    s = max(0, int((now - t).total_seconds()))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 2 * 86400:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} days ago"


def _worst(entries: list[dict]) -> str:
    return max((e.get("severity") or "info" for e in entries), key=lambda s: RANK.get(s, 0), default="info")


def _row(label: str, head: str, sub: str = "", cls: str = "", gap: bool = False, link: tuple[str, str] | None = None,
         head_sev: str | None = None) -> dict:
    """One cell of the block. `head` is the sentence in large type, `sub` the small line under it, `link` (text, href)
    is rendered inside `sub`, `gap` marks a setup gap (no feed, no schedule) that the header counts, and `text` is the
    one-line form the Ask button sends. `head_sev` names a severity word inside `head` to colour."""
    text = head + (("; " + sub) if sub else "")
    return {"label": label, "head": head, "sub": sub, "cls": cls, "gap": gap, "link": link, "head_sev": head_sev,
            "text": text.rstrip(".") + "."}


def lines(*, triage: dict | None, comparison: dict | None, manifest: dict | None, ledger: dict | None, changes: list[dict] | None,
          schedule: dict | None, now: datetime | None = None, ledger_error: str | None = None, target: str = "",
          latest_sid: str | None = None) -> list[dict]:
    """The five cells (see `_row`). Every input may be None: a database with no review yet, no ledger yet, no feed, no
    schedule still gets five honest cells rather than a blank. `now` is for tests; `target` and `latest_sid` build links."""
    now = now or datetime.now(timezone.utc)
    out = []
    edit_href = f"/targets/{target}" if target else "/"
    snap_href = f"/s/{latest_sid}" if latest_sid else None

    # ---- health now
    if not triage:
        out.append(_row("Health", "No review yet", "the first snapshot has not been taken"))
    else:
        sevty = triage.get("severity") or "info"
        when = ago((manifest or {}).get("captured_at") or (comparison or {}).get("captured_at"), now)
        sub = f"reviewed {when}" if when else ""
        if triage.get("source") == "rules" and triage.get("agent_error"):
            sub += (" · " if sub else "") + "the model failed, so this is the rules' review"
        band = (comparison or {}).get("band") or {}
        if band.get("applies", True) and band.get("sentence"):
            sub += (" · " if sub else "") + band["sentence"]
        out.append(_row("Health", triage.get("title") or "no title", sub, cls=sevty))

    # ---- open findings
    if ledger_error:
        out.append(_row("Open", "Ledger unavailable", ledger_error, cls="medium"))
    elif ledger is None:
        out.append(_row("Open", "Nothing tracked yet", "the ledger starts with the first run that has a baseline"))
    else:
        entries = [e for e in ledger.get("entries") or [] if e.get("state") != "gone"]
        if not entries:
            out.append(_row("Open", "No open findings", "", cls="info"))
        else:
            worst = _worst(entries)
            oldest = min(entries, key=lambda e: e.get("first_seen_at") or "")
            since = (oldest.get("first_seen_at") or "")[:10] if _when(oldest.get("first_seen_at")) else ""  # a snapshot id is not a date
            tickets = sorted({t.get("key") for e in entries for t in (e.get("tickets") or {}).values() if isinstance(t, dict) and t.get("key")})
            recurring = sum(1 for e in entries if e.get("state") == "recurring")
            head = f"{len(entries)} finding{'s' if len(entries) != 1 else ''}, worst {worst}"
            bits = []
            if recurring:
                bits.append(f"{recurring} recurring")
            if tickets:
                bits.append("ticket" + ("s " if len(tickets) != 1 else " ") + ", ".join(tickets[:3]) + (" …" if len(tickets) > 3 else ""))
            sub = " · ".join(bits)
            link = (f"oldest since {since}", snap_href) if since and snap_href else None
            if since and not link:
                sub = f"oldest since {since}" + (f" · {sub}" if sub else "")
            out.append(_row("Open", head, sub, cls=worst, link=link, head_sev=worst))

    # ---- what changed
    feed = [c for c in (changes or []) if isinstance(c, dict) and c.get("kind") in ("deploy", "parameter", "job", "failover") and not c.get("error")]
    near = [c for c in ((comparison or {}).get("changes") or []) if isinstance(c, dict)]
    last = max(feed, key=lambda c: c.get("time") or "") if feed else None
    if last:
        after = 0
        if ledger:
            t0 = last.get("time") or ""
            after = sum(1 for e in ledger.get("entries") or [] if (e.get("first_seen_at") or "") > t0 and e.get("state") != "gone")
        head = f"{last.get('kind')} {last.get('description') or ''}".strip()
        sub = ago(last.get("time"), now)
        if near:
            sub += ", inside or just before the last window"
        if ledger:  # without a ledger nothing is known about what opened, so nothing is claimed
            sub += f" · {after} finding{'s' if after != 1 else ''} opened after it" if after else " · no finding opened after it"
        out.append(_row("Changes", head, sub, cls="medium" if after else ""))
    elif (manifest or {}).get("changes_sources"):
        out.append(_row("Changes", "No change in the window", "the feed saw no deploy or setting change"))
    else:
        out.append(_row("Changes", "No change feed", "Regressions can't be tied to a deploy.", cls="medium", gap=True, link=("Set one", edit_href)))

    # ---- trend over seven days
    if ledger is None:
        out.append(_row("Last 7 days", "No history yet"))
    else:
        cut = now - WEEK
        opened = [e for e in ledger.get("entries") or [] if (_when(e.get("first_seen_at")) or cut) > cut]
        cleared = [t for t in ledger.get("tombstones") or [] if (_when(t.get("gone_at")) or cut) > cut]
        net = len(opened) - len(cleared)
        sub = f"net {net:+d}"
        dropped = rules_dropped(triage)
        if dropped:
            sub += f" · the model left {dropped} rules finding{'s' if dropped != 1 else ''} out of the last review"
        out.append(_row("Last 7 days", f"{len(opened)} opened · {len(cleared)} cleared", sub, cls="medium" if dropped else ""))

    # ---- next run
    if not schedule or not schedule.get("schedule"):
        out.append(_row("Next", "No schedule", "Manual or webhook only.", cls="medium", gap=True, link=("Add schedule", "/settings#schedule")))
    elif schedule.get("paused"):
        out.append(_row("Next", "Schedule paused", schedule.get("label") or schedule["schedule"], cls="medium", gap=True, link=("Resume", "/settings#schedule")))
    else:
        nxt = schedule.get("next")
        nxt_s = nxt.strftime("%m-%d %H:%M") if isinstance(nxt, datetime) else str(nxt or "")[5:16]
        head = f"Next run {nxt_s}" if nxt_s else "Scheduled"
        status = str(schedule.get("last_status") or "")
        if status and not status.startswith(("ok", "started", "done")):
            out.append(_row("Next", head, f"{schedule.get('label') or schedule['schedule']} · the last scheduled run did not finish: {status}", cls="high"))
        else:
            out.append(_row("Next", head, schedule.get("label") or schedule["schedule"]))
    return out


def gaps(rows: list[dict]) -> int:
    """How many cells are setup gaps: things the team can fix on the settings pages, not database problems."""
    return sum(1 for r in rows if r.get("gap"))


def synopsis(target: str, rows: list[dict]) -> str:
    """One or two plain sentences for the dock's first message, from the rules: the health, the open findings, and
    the setup gaps. The model's job starts after this."""
    by = {r["label"]: r for r in rows}
    health, open_, week = by.get("Health"), by.get("Open"), by.get("Last 7 days")
    first = f"{target}: {health['head'][0].lower() + health['head'][1:]}" if health else target
    if open_ and open_["head"] not in ("No open findings", "Nothing tracked yet", "Ledger unavailable"):
        first += f", {open_['head'][0].lower() + open_['head'][1:]}"
    elif open_ and open_["head"] == "No open findings":
        first += ", no open findings"
    if week and week["sub"].startswith("net "):
        first += f", {week['sub'].split(' · ')[0]} this week"
    n = gaps(rows)
    second = ""
    if n:
        names = [r["head"][0].lower() + r["head"][1:] for r in rows if r.get("gap")]
        second = f" {n} setup gap{'s' if n != 1 else ''} would make reviews sharper: {' and '.join(names)}."
    return first + "." + second


def suggestions(rows: list[dict], trend: list[dict] | None = None, metric: str | None = None) -> list[str]:
    """Three starter questions for the dock, drawn from the facts: the open finding, the trend's odd point, the gap."""
    out = []
    by = {r["label"]: r for r in rows}
    open_ = by.get("Open")
    if open_ and open_.get("head_sev"):
        out.append(f"What is the open {open_['head_sev']} finding and is it still true?")
    pts = [p for p in (trend or []) if isinstance(p.get("value"), (int, float))]
    if len(pts) >= 3 and metric:
        vals = sorted(p["value"] for p in pts)
        median = vals[len(vals) // 2]
        peak = max(pts, key=lambda p: p["value"])
        if median > 0 and peak["value"] >= 2 * median or (median == 0 and peak["value"] > 0):
            when = peak.get("t") or peak.get("captured_at") or peak.get("at")  # web.trend_points keeps a datetime under "t"
            at = when.strftime("%H:%M") if isinstance(when, datetime) else str(when or "")[11:16]
            out.append(f"Why did {metric.removesuffix('/s')} spike{f' at {at}' if at else ''}?")
    changes = by.get("Changes")
    if changes and not changes.get("gap") and changes["head"].startswith(("deploy", "parameter")):
        out.append(f"Did the {changes['head']} cause anything that is open now?")
    nxt = by.get("Next")
    if nxt and nxt.get("gap"):
        out.append("Suggest a snapshot schedule for this workload")
    if not out:
        out.append("What deserves attention first on this database, and why?")
    return out[:3]


def question(target: str, rows: list[dict]) -> str:
    """The canned question the Ask button sends: the five facts, then what to do with them. Under chat's limit."""
    facts = "\n".join(f"- {r['label']}: {r['text']}" for r in rows)
    text = (f"Summarise the current state of {target} in one short paragraph for the on-call engineer, then say what, "
            f"if anything, deserves attention first and why. Use these facts from the page and the stored snapshot; "
            f"do not invent numbers.\n{facts}")
    return text[:MAX_QUESTION]
