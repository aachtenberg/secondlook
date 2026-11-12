"""One review for the fleet: a scheduled note across every target, not a dashboard.

Since the last digest, which findings opened on which database, which of them share a cause, and which databases
had no significant change. Two links join findings across targets, and they are not equally strong:

- the same deploy: one change-feed description (bet 3, changes.json) seen on two or more targets inside the window,
  with findings opened after it on at least two of them. The deploy id is the same on both, so the sentence
  "After billing@1.4.2, findings opened on ora-prod and pg-prod" is true, and it crosses engines.
- the same statement: the normalized text hash (dbtriage.sqlnorm) of the subject of an open sql finding, equal on
  two or more targets of one engine. Replicas, shards and read copies run the same SQL under different ids.
  Never joined across engines (see sqlnorm).

Everything is read from the store: the per-target ledgers (ledger.py), the snapshots' changes.json and summary.json.
Nothing here touches a database. The digest is delivered as a triage-shaped dict (target and engine "fleet", no
`life`) so the existing channels post it under the whole-run gate, and its last send is kept at `fleet/digest.json`
so a scheduler restart neither resends it nor loses a slot that passed while it was down.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone

from dbtriage import ledger, sqlnorm
from dbtriage.storage import Storage, StorageUnavailable

RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
STATEMENT_KINDS = ("sql_regressed", "sql_new", "sql_volume", "sql_top", "plan_change")  # findings whose subject is a sql_id
DIGEST_KEY = "fleet/digest.json"
SUMMARIES_PER_TARGET = 10  # how many of a target's latest summaries the hash lookup reads before giving up
_SID_TIME = re.compile(r"/(\d{8}T\d{6})Z(?:-\d+)?$")


def _when(iso: str | None) -> datetime | None:
    """An ISO time as an aware datetime (UTC when it carries no offset); None for nothing or garbage."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None
    except (TypeError, ValueError, AttributeError):
        return None
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def _sid_time(sid: str) -> datetime | None:
    """A snapshot's capture time from its id, so the digest does not read a manifest per snapshot."""
    m = _SID_TIME.search(sid)
    return datetime.strptime(m[1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc) if m else None


def parse_since(text: str, now: datetime) -> datetime:
    """'7d', '24h', '90m' before now, or an ISO time (UTC without an offset). Raises ValueError."""
    if m := re.fullmatch(r"\s*(\d+)\s*([dhm])\s*", text or ""):
        n, unit = int(m[1]), m[2]
        return now - timedelta(**{{"d": "days", "h": "hours", "m": "minutes"}[unit]: n})
    t = _when(text)
    if t is None:
        raise ValueError(f"--since takes 7d, 24h, 90m or an ISO time, not {text!r}")
    return t


def _in(t: datetime | None, since: datetime, until: datetime) -> bool:
    return t is not None and since <= t <= until


def _snapshots_in(store: Storage, engine: str, target: str, since: datetime, until: datetime) -> list[str]:
    return [s for s in store.list_snapshots(engine, target) if _in(_sid_time(s), since, until)]


def _deploys(store: Storage, sids: list[str], since: datetime, until: datetime) -> dict[str, datetime]:
    """{description: earliest time} of the deploys inside the window from the snapshots' stored feeds. A deploy sits
    in several snapshots' feeds (the lookback), so it is keyed by its text."""
    out: dict[str, datetime] = {}
    for sid in sids:
        if not store.exists(f"{sid}/changes.json"):
            continue
        try:
            items = store.get_json(f"{sid}/changes.json")
        except (FileNotFoundError, ValueError):
            continue
        for it in items or []:
            if not isinstance(it, dict) or it.get("kind") != "deploy" or it.get("error"):
                continue
            t, text = _when(it.get("time")), str(it.get("description") or "").strip()
            if text and _in(t, since, until) and (text not in out or t < out[text]):
                out[text] = t
    return out


def _hash_of(store: Storage, sids: list[str], sql_id: str, cache: dict) -> str:
    """The text hash of a statement, from the latest summary that lists it. Summaries stored before the hash existed
    get it computed from their redacted text, which normalizes the same."""
    for sid in reversed(sids[-SUMMARIES_PER_TARGET:]):
        rows = cache.get(sid)
        if rows is None:
            try:
                s = store.get_json(f"{sid}/summary.json")
            except (FileNotFoundError, ValueError):
                s = {}
            rows = cache[sid] = {str(r.get("sql_id")): r for r in (s.get("top_sql") or []) if isinstance(r, dict) and r.get("sql_id") is not None}
        if (r := rows.get(sql_id)) is not None:
            return r.get("text_hash") or sqlnorm.text_hash(r.get("text"))
    return ""


def _entry(t: str, e: dict) -> dict:
    return {"target": t, "kind": e.get("kind"), "subject": e.get("subject"), "title": e.get("title") or "", "severity": e.get("severity") or "low",
            "state": e.get("state"), "first_seen_at": e.get("first_seen_at"), "tickets": {k: v.get("key") for k, v in (e.get("tickets") or {}).items() if isinstance(v, dict)}}


def digest(cfg, store: Storage, since: datetime, until: datetime) -> dict:
    """The fleet's week (or day) in one dict, across every configured target.

    targets: per target its engine, the findings opened in the window (first seen inside it, or worse on the latest
    run inside it; the ledger keeps the state of the last run only, so an earlier worsening is not seen), how many
    snapshots the window holds and the deploys in it. Then `deploys`: the cross-target deploy links, `statements`:
    the same-statement links within one engine, `quiet`: targets reviewed in the window with nothing new or worse,
    and `unreviewed`: targets with no snapshot in the window, which is not the same as quiet."""
    targets: dict[str, dict] = {}
    deploy_targets: dict[str, dict[str, datetime]] = {}  # description -> {target: time}
    statements: dict[tuple[str, str], list[dict]] = {}   # (engine, hash) -> open sql findings carrying it
    for name, t in sorted(cfg.targets.items()):
        try:
            led = ledger.load(store, t.engine, name)
            sids = _snapshots_in(store, t.engine, name, since, until)
        except StorageUnavailable as exc:
            print(f"digest: {name}: store unavailable ({exc}); left out", file=sys.stderr)
            continue
        opened = []
        for e in led["entries"]:
            new = _in(_when(e.get("first_seen_at")), since, until)
            worse = e.get("state") == "worse" and _in(_when(e.get("last_seen_at")), since, until)
            if new or worse:
                opened.append(_entry(name, e))
        deploys = _deploys(store, sids, since, until)
        for text, at in deploys.items():
            deploy_targets.setdefault(text, {})[name] = at
        cache: dict = {}
        all_sids = store.list_snapshots(t.engine, name) if led["entries"] else []
        for e in led["entries"]:
            if e.get("kind") in STATEMENT_KINDS and e.get("subject"):
                if h := _hash_of(store, all_sids, str(e["subject"]), cache):
                    statements.setdefault((t.engine, h), []).append(_entry(name, e))
        opened.sort(key=lambda f: (-RANK.get(f["severity"], 0), f["first_seen_at"] or ""))
        targets[name] = {"engine": t.engine, "opened": opened, "snapshots": len(sids),
                         "deploys": sorted(deploys, key=deploys.get)}

    links = []
    for text, seen in sorted(deploy_targets.items(), key=lambda kv: min(kv[1].values())):
        if len(seen) < 2:
            continue
        after = sorted(n for n, at in seen.items() if any(_when(f["first_seen_at"]) and _when(f["first_seen_at"]) >= at for f in targets[n]["opened"]))
        if len(after) >= 2:
            links.append({"deploy": text, "at": min(seen.values()).isoformat(), "targets": after,
                          "quiet": sorted(n for n in seen if n not in after)})
    same = []
    for (engine, h), rows in sorted(statements.items()):
        by_target = {}
        for r in rows:
            if r["target"] not in by_target or RANK.get(r["severity"], 0) > RANK.get(by_target[r["target"]]["severity"], 0):
                by_target[r["target"]] = r
        if len(by_target) >= 2:
            same.append({"engine": engine, "text_hash": h, "findings": [by_target[n] for n in sorted(by_target)]})

    quiet = sorted(n for n, t in targets.items() if not t["opened"] and t["snapshots"])
    unreviewed = sorted(n for n, t in targets.items() if not t["opened"] and not t["snapshots"])
    top = max((f["severity"] for t in targets.values() for f in t["opened"]), key=lambda s: RANK.get(s, 0), default="info")
    return {"since": since.isoformat(), "until": until.isoformat(), "severity": top, "targets": targets,
            "deploys": links, "statements": same, "quiet": quiet, "unreviewed": unreviewed}


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _day(iso: str | None) -> str:
    t = _when(iso)
    return f"{t:%Y-%m-%d}" if t else ""


def title(d: dict) -> str:
    return f"Fleet digest {_day(d['since'])} to {_day(d['until'])}"


def render(d: dict) -> str:
    """The note as markdown: a heading, one line per cross-target cause, the opened findings per target, the quiet
    list. Under a page by construction: it names findings, it does not chart them."""
    out = [f"# {title(d)}", ""]
    n_open = sum(len(t["opened"]) for t in d["targets"].values())
    out.append(f"{n_open} finding{'s' if n_open != 1 else ''} opened on {len([t for t in d['targets'].values() if t['opened']])} of "
               f"{len(d['targets'])} databases; {len(d['quiet'])} had no significant change.")
    out.append("")
    if d["deploys"] or d["statements"]:
        out.append("## Shared causes")
        out.append("")
        for l in d["deploys"]:
            line = f"- After `{l['deploy']}`, findings opened on {_names(l['targets'])}"
            if l["quiet"]:
                line += f" (also deployed to {_names(l['quiet'])}, where nothing opened)"
            out.append(line + ".")
        for s in d["statements"]:
            sevs = {f["severity"] for f in s["findings"]}
            where = (f"{s['findings'][0]['severity']} on {_names([f['target'] for f in s['findings']])}" if len(sevs) == 1
                     else _names([f"{f['severity']} on {f['target']}" for f in s["findings"]]))
            ids = ", ".join(f"`{f['subject']}` on {f['target']}" for f in s["findings"])
            out.append(f"- The same statement is {where} ({s['engine']}, text hash `{s['text_hash']}`: {ids}).")
        out.append("")
    for name, t in d["targets"].items():
        if not t["opened"]:
            continue
        out.append(f"## {name} ({t['engine']})")
        out.append("")
        for f in t["opened"]:
            tick = "".join(f", ticket {k}" for k in f["tickets"].values() if k)
            state = "worse" if f["state"] == "worse" and not _in(_when(f["first_seen_at"]), _when(d["since"]), _when(d["until"])) else "new"
            what = f["title"] or f"{f['kind']} {f['subject']}"
            out.append(f"- **{f['severity']}** {what} ({state} since {_day(f['first_seen_at'])}{tick})")
        if t["deploys"]:
            out.append(f"- deploys in the window: {', '.join(f'`{x}`' for x in t['deploys'])}")
        out.append("")
    if d["quiet"]:
        out.append(f"No significant change: {_names(d['quiet'])}.")
    if d["unreviewed"]:
        out.append(f"No snapshot in the window: {_names(d['unreviewed'])}.")
    return "\n".join(out).rstrip() + "\n"


def as_triage(d: dict) -> dict:
    """The digest in the shape deliver() reads: severity gates the whole run (no `life`), target and engine are
    "fleet" for subjects and metrics, and `findings` is empty so a ticket channel opens one issue for the note."""
    return {"severity": d["severity"], "title": title(d), "target": "fleet", "engine": "fleet", "findings": [],
            "summary": f"fleet digest for {_day(d['since'])} to {_day(d['until'])}", "ts": int(time.time()),
            "since": d["since"], "until": d["until"]}


# ---------------------------------------------------------------- schedule
def schedule_of(delivery_cfg: dict | None) -> tuple[str, list[str] | None]:
    """(cron, channels) from `[delivery] digest`: schedule is "weekly" (Monday 03:00), "daily" (03:00), or cron in the
    server's local time; channels default to the delivery channels. ("", None) when there is no digest."""
    from dbtriage.scheduler import normalize

    d = (delivery_cfg or {}).get("digest")
    if not isinstance(d, dict):
        return "", None
    try:
        cron = normalize(d.get("schedule"))
    except (ValueError, TypeError, AttributeError):
        print(f"digest: schedule {d.get('schedule')!r} is not a schedule; the digest is off", file=sys.stderr)
        return "", None
    ch = d.get("channels")
    return cron, (list(ch) if isinstance(ch, list) and ch else None)


def last_sent(store: Storage) -> datetime | None:
    """When the scheduled digest last went out, from fleet/digest.json; None before the first."""
    try:
        return _when(store.get_json(DIGEST_KEY).get("sent_at"))
    except (FileNotFoundError, ValueError, AttributeError, StorageUnavailable):
        return None


def record_sent(store: Storage, d: dict, delivered: list[str], at: datetime) -> None:
    store.put_json(DIGEST_KEY, {"sent_at": at.isoformat(), "since": d["since"], "until": d["until"], "severity": d["severity"],
                                "delivered": delivered})


def send(cfg, store: Storage, since: datetime, until: datetime, channels: list[str] | None = None) -> tuple[dict, str, list[str]]:
    """Build, render and deliver the digest; returns (digest, markdown, channels that took it)."""
    from dbtriage.deliver import deliver

    d = digest(cfg, store, since, until)
    md = render(d)
    return d, md, deliver(cfg.delivery, as_triage(d), md, channels)


class Digest:
    """What the scheduler calls: the digest's schedule from the current config, when it last went out, and the send
    for one slot. `config()` is called every tick so a changed targets.toml applies without a restart."""

    def __init__(self, config, store: Storage):
        self.config, self.store = config, store

    def schedule(self) -> str:
        return schedule_of(self.config().delivery)[0]

    def last_sent(self) -> datetime | None:
        return last_sent(self.store)

    def fire(self, slot: datetime, now: datetime, reason: str) -> tuple[bool, str]:
        """The digest for one schedule slot: since the last send (or one period before the slot, for the first), until
        now. The store is checked once more so two scheduler processes on one store send one digest, not two."""
        from dbtriage.scheduler import parse

        cfg = self.config()
        cron, channels = schedule_of(cfg.delivery)
        last = self.last_sent()
        if last is not None and last >= slot:
            return False, f"already sent at {last:%Y-%m-%d %H:%M}"
        if last is not None:
            since = last
        else:  # the first digest covers one period before its slot (a week for "weekly")
            since = slot - ((parse(cron).next_after(slot) - slot) if cron else timedelta(days=7))
        d, md, done = send(cfg, self.store, since, now, channels)
        record_sent(self.store, d, done, now)
        print(f"scheduler: {reason}: {title(d)}: {d['severity']}, delivered to {', '.join(done) or 'nothing'}")
        return True, "sent"
