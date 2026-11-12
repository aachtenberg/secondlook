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


def lines(*, triage: dict | None, comparison: dict | None, manifest: dict | None, ledger: dict | None, changes: list[dict] | None,
          schedule: dict | None, now: datetime | None = None, ledger_error: str | None = None) -> list[dict]:
    """The five lines, each {"label", "text", "cls"}; `cls` is a severity for colouring or "" for a neutral line.

    Every input may be None: a database with no review yet, no ledger yet, no feed, no schedule still gets five
    honest sentences rather than a blank. `now` is for tests."""
    now = now or datetime.now(timezone.utc)
    out = []

    # ---- health now
    if not triage:
        out.append({"label": "Health", "text": "No review yet: the first snapshot has not been taken.", "cls": ""})
    else:
        sevty = triage.get("severity") or "info"
        when = ago((manifest or {}).get("captured_at") or (comparison or {}).get("captured_at"), now)
        head = f"{sevty.capitalize()}: {triage.get('title') or 'no title'}"
        if when:
            head += f", reviewed {when}"
        if triage.get("source") == "rules" and triage.get("agent_error"):
            head += ". The model failed, so this is the rules' review"
        band = (comparison or {}).get("band") or {}
        if band.get("applies", True) and band.get("sentence"):
            head += ". " + band["sentence"][0].upper() + band["sentence"][1:]
        out.append({"label": "Health", "text": head + ".", "cls": sevty})

    # ---- open findings
    if ledger_error:
        out.append({"label": "Open", "text": f"The ledger could not be read: {ledger_error}.", "cls": ""})
    elif ledger is None:
        out.append({"label": "Open", "text": "No findings tracked yet: the ledger starts with the first run that has a baseline.", "cls": ""})
    else:
        entries = [e for e in ledger.get("entries") or [] if e.get("state") != "gone"]
        if not entries:
            out.append({"label": "Open", "text": "No open findings.", "cls": "info"})
        else:
            worst = _worst(entries)
            oldest = min(entries, key=lambda e: e.get("first_seen_at") or "")
            since = (oldest.get("first_seen_at") or oldest.get("first_seen") or "")[:10]
            tickets = sorted({t.get("key") for e in entries for t in (e.get("tickets") or {}).values() if isinstance(t, dict) and t.get("key")})
            recurring = sum(1 for e in entries if e.get("state") == "recurring")
            text = f"{len(entries)} open finding{'s' if len(entries) != 1 else ''}, worst {worst}" + (f", oldest since {since}" if since else "")
            if recurring:
                text += f", {recurring} recurring"
            if tickets:
                text += "; ticket" + ("s " if len(tickets) != 1 else " ") + ", ".join(tickets[:3]) + (" …" if len(tickets) > 3 else "")
            out.append({"label": "Open", "text": text + ".", "cls": worst})

    # ---- what changed
    feed = [c for c in (changes or []) if isinstance(c, dict) and c.get("kind") in ("deploy", "parameter", "job", "failover") and not c.get("error")]
    near = [c for c in ((comparison or {}).get("changes") or []) if isinstance(c, dict)]
    last = max(feed, key=lambda c: c.get("time") or "") if feed else None
    if last:
        after = 0
        if ledger:
            t0 = last.get("time") or ""
            after = sum(1 for e in ledger.get("entries") or [] if (e.get("first_seen_at") or "") > t0 and e.get("state") != "gone")
        text = f"Last change: {last.get('kind')} {last.get('description') or ''} {ago(last.get('time'), now)}".strip()
        if near:
            text += ", inside or just before the last window"
        text += f"; {after} finding{'s' if after != 1 else ''} opened after it." if after else "; no finding opened after it."
        out.append({"label": "Changed", "text": text, "cls": "medium" if after else ""})
    elif (manifest or {}).get("changes_sources"):
        out.append({"label": "Changed", "text": "No deploy or setting change in the feed for the last window.", "cls": ""})
    else:
        out.append({"label": "Changed", "text": "No change feed configured; a regression cannot be tied to a deploy. Set one on the database's edit page.", "cls": ""})

    # ---- trend over seven days
    if ledger is None:
        out.append({"label": "Week", "text": "No history yet.", "cls": ""})
    else:
        cut = now - WEEK
        opened = [e for e in ledger.get("entries") or [] if (_when(e.get("first_seen_at")) or cut) > cut]
        cleared = [t for t in ledger.get("tombstones") or [] if (_when(t.get("gone_at")) or cut) > cut]
        text = f"{len(opened)} opened and {len(cleared)} cleared in the last 7 days"
        dropped = rules_dropped(triage)
        if dropped:
            text += f"; the model left {dropped} rules finding{'s' if dropped != 1 else ''} out of the last review"
        out.append({"label": "Week", "text": text + ".", "cls": "medium" if dropped else ""})

    # ---- next run
    if not schedule or not schedule.get("schedule"):
        out.append({"label": "Next", "text": "No schedule: snapshots are taken by hand or by webhook.", "cls": ""})
    elif schedule.get("paused"):
        out.append({"label": "Next", "text": f"Schedule paused ({schedule.get('label') or schedule['schedule']}).", "cls": "medium"})
    else:
        nxt = schedule.get("next")
        nxt_s = nxt.strftime("%m-%d %H:%M") if isinstance(nxt, datetime) else str(nxt or "")[5:16]
        text = f"Next run {nxt_s}" if nxt_s else "Scheduled"
        text += f" ({schedule.get('label') or schedule['schedule']})"
        status = str(schedule.get("last_status") or "")
        if status and not status.startswith(("ok", "started", "done")):
            text += f"; the last scheduled run did not finish: {status}"
            out.append({"label": "Next", "text": text + ".", "cls": "high"})
        else:
            out.append({"label": "Next", "text": text + ".", "cls": ""})
    return out


def question(target: str, rows: list[dict]) -> str:
    """The canned question the Ask button sends: the five facts, then what to do with them. Under chat's limit."""
    facts = "\n".join(f"- {r['label']}: {r['text']}" for r in rows)
    text = (f"Summarise the current state of {target} in one short paragraph for the on-call engineer, then say what, "
            f"if anything, deserves attention first and why. Use these facts from the page and the stored snapshot; "
            f"do not invent numbers.\n{facts}")
    return text[:MAX_QUESTION]
