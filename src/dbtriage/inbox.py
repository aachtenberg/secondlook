"""The webhook inbox: deploys and other changes a pipeline POSTs to `dbtriage serve` (/changes).

Each target has one object, <engine>/<target>/changes-inbox.json = {"items": [...], "generation": n}, that
`record` appends to and changes.fetch reads into every snapshot's feed, whatever source the target configured.
A stored item has the feed's shape, {time, kind, description, link, source: "webhook"}; a request is
{target, time?, kind?, description, link?} or a list of those, and `parse` is what the route checks it with.

The object is saved with the store's compare-and-set (put_if) in a retry loop like the ledger, so two pipelines
posting at once each re-read and append again instead of one overwriting the other. Unlike the ledger it needs no
generation rule on the spool: items are only ever appended, never rewritten, so a replayed copy cannot resurrect a
state a later write retracted; the generation is kept so a reader can tell two writes apart. What an outage can
still lose is named in `record`.
"""
from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timedelta, timezone

from dbtriage import changes
from dbtriage.storage import Conflict, Storage

MAX_ITEMS = 500   # a few hundred: more than any window plus lookback can show, small enough to rewrite on every post
ATTEMPTS = 8      # losses of the compare-and-set before giving up; many pipelines may finish within one second
BACKOFF_S = 0.05  # the longest pause after a lost race
KEEP_EXTRA = timedelta(days=1)  # kept past the lookback, so a late capture of yesterday's window still sees its deploys


class InboxContended(RuntimeError):
    """The inbox changed under every attempt; nothing was saved."""


def parse(payload) -> list[tuple[str, dict]]:
    """(target, item) for each change in a request body: one object or a list of them. ValueError says what is wrong
    with the first bad one, so the pipeline's log shows it."""
    objs = payload if isinstance(payload, list) else [payload]
    if not objs:
        raise ValueError("no changes in the request")
    return [parse_item(o) for o in objs]


def parse_item(obj, now: datetime | None = None) -> tuple[str, dict]:
    if not isinstance(obj, dict):
        raise ValueError("each change must be a JSON object {target, time, kind, description, link}")
    target = obj.get("target")
    if not isinstance(target, str) or not target:
        raise ValueError("target is required: the database's name in targets.toml")
    description = str(obj.get("description") or "").strip()
    if not description:
        raise ValueError("description is required, for example \"billing@1.4.2 to production\"")
    kind = str(obj.get("kind") or "deploy").strip().lower()
    if kind not in changes.KINDS and kind not in changes.KIND_ALIASES:
        raise ValueError(f"kind must be one of {', '.join(changes.KINDS)}")
    raw_time = obj.get("time")
    if raw_time in (None, ""):
        at = now or datetime.now(timezone.utc)
    else:
        at = changes.parse_time(raw_time if isinstance(raw_time, str) else None)
        if at is None:
            raise ValueError("time must be ISO 8601, for example 2026-11-11T09:30:00Z (UTC when no offset is given)")
    link = obj.get("link")
    return target, {"time": at.astimezone(timezone.utc).isoformat(), "kind": changes.kind_of(kind), "description": description[:300],
                    "link": str(link)[:500] if link else None, "source": "webhook"}


def record(store: Storage, target, items: list[dict], now: datetime | None = None, attempts: int | None = None) -> list[dict]:
    """Append parsed `items` to the target's inbox and return them. Each write also prunes: an item older than the
    target's lookback plus a day cannot reach any feed (a capture reads the window plus the lookback), and the object
    keeps at most MAX_ITEMS, the newest.

    Through ResilientStorage an outage spools the object like any file (version_of answers None, put_if writes the
    spool). The store treats this file as append-only: a write made from a stale copy, a second append spooled in
    the same outage, and the replay itself are each merged with what is already there, item by item, so a deploy
    posted during an outage is not lost. Only the pruning can differ between copies, and the next write settles it."""
    if not items:
        return []
    k = changes.inbox_key(target.engine, target.name)
    cutoff = (now or datetime.now(timezone.utc)) - changes.lookback(changes.spec_of(target)) - KEEP_EXTRA
    attempts = attempts or ATTEMPTS
    for _ in range(attempts):
        version = store.version_of(k)  # before the read, so a write that lands between the two fails the save
        try:
            data = store.get_json(k) if store.exists(k) else {}
        except ValueError:  # a reader can see a half-written file while another writer holds the lock: read again
            if store.version_of(k) != version:
                continue
            data = {}  # the object itself is unreadable: nothing in it can be kept, and the save replaces it
        old = data.get("items") if isinstance(data, dict) else None
        kept = [it for it in (old or []) + items if isinstance(it, dict) and (changes.parse_time(it.get("time")) or cutoff) >= cutoff]
        kept.sort(key=lambda it: it["time"])
        obj = {"items": kept[-MAX_ITEMS:], "generation": int((data or {}).get("generation") or 0) + 1 if isinstance(data, dict) else 1}
        try:
            store.put_json_if(k, obj, version)
        except Conflict:
            print(f"inbox: {k} changed under us (the store refused the save); appending again", file=sys.stderr)
            time.sleep(random.uniform(0, BACKOFF_S))  # a pause with jitter, or a writer in a tight loop starves the other
            continue
        return items
    raise InboxContended(f"inbox {k}: changed under {attempts} attempts; not saved")
