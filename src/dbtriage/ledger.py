"""A finding that stays a finding: the per-target ledger of open findings.

Every run writes a fresh review, so the same plan change could page the channel every hour. The ledger
remembers what was open after the last run and gives each of this run's findings a state:

    new         not in the ledger
    still_open  present, same severity as last time
    worse       present, higher severity than last time
    quiet       present, below its own severity last time
    gone        absent from the rules findings for `gone_after` consecutive runs (default 3, so the channel
                does not flap when a finding is just under a threshold for one run)

Identity is `findings.identity` (kind, subject), the same join used for the agreement marks. Presence comes
from the rules findings (compare.json "findings"); a finding only the model named is tracked by its own
appearances, since nothing else sees it. The review's matched finding supplies the title and severity.

The ledger lives at `<engine>/<target>/findings.json`, next to the snapshot days, and is read and written
through the same `Storage` interface as everything else: on a remote store an outage sends the put to the
spool and the next flush replays it, like any snapshot file. It is derived data: `rebuild()` recomputes it
from the stored triage.json and compare.json files, so losing it is a rebuild, not a loss.
"""
from __future__ import annotations

import sys

from dbtriage.findings import identity
from dbtriage.storage import Storage, StorageUnavailable

RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
STATES = ("new", "still_open", "worse", "quiet", "gone")
DEFAULT_GONE_AFTER = 3


def key(engine: str, target: str) -> str:
    return f"{engine}/{target}/findings.json"


def gone_after(delivery_cfg: dict | None) -> int:
    """`[delivery] gone_after_runs`: the threshold sits with delivery because flapping is a channel problem."""
    try:
        n = int((delivery_cfg or {}).get("gone_after_runs") or DEFAULT_GONE_AFTER)
    except (TypeError, ValueError):
        n = DEFAULT_GONE_AFTER
    return max(1, n)


def load(store: Storage, engine: str, target: str) -> dict:
    """The stored ledger, or an empty one when none exists yet. A store outage (StorageUnavailable) propagates:
    an empty ledger written in its place would replay over the real one when the spool flushes."""
    try:
        data = store.get_json(key(engine, target))
    except (FileNotFoundError, ValueError):
        return {"entries": [], "updated": None}
    return {"entries": list(data.get("entries") or []), "updated": data.get("updated")}


def save(store: Storage, engine: str, target: str, ledger: dict) -> None:
    store.put_json(key(engine, target), ledger)


def _sev(f: dict) -> str:
    s = f.get("severity")
    return s if s in RANK else "low"


def _title(f: dict) -> str:
    return str(f.get("summary") or f.get("title") or f.get("where") or "")[:160]


def apply(ledger: dict, snapshot_id: str, captured_at: str | None, rules_findings: list[dict],
          review_findings: list[dict] | None = None, gone_after: int = DEFAULT_GONE_AFTER) -> dict:
    """Advance the ledger (in place) by one run and return what the run should say about its findings.

    Returns {"life": [...parallel to review_findings...], "still_open": [...], "gone": [...]}. `life` items
    carry state, first_seen, first_seen_at, misses and tickets; `still_open` is every open entry that is not
    among the review's findings (so a still-open item never takes one of the review's five slots); `gone`
    is the entries reported gone on this run, which are then dropped from the ledger.
    """
    review_findings = review_findings or []
    entries = {(e["kind"], e["subject"]): e for e in ledger["entries"]}
    # what is present this run, with the review's finding first where it matched (title and severity come from it)
    present: dict[tuple[str, str], dict] = {}
    for f in list(review_findings) + list(rules_findings or []):
        present.setdefault(identity(f), f)

    for ident, f in present.items():
        sev = _sev(f)
        e = entries.get(ident)
        if e is None:
            entries[ident] = {"kind": ident[0], "subject": ident[1], "title": _title(f),
                              "first_seen": snapshot_id, "first_seen_at": captured_at, "last_seen": snapshot_id, "last_seen_at": captured_at,
                              "severity": sev, "peak": sev, "state": "new", "misses": 0, "tickets": {}}
            continue
        was = RANK.get(e.get("severity"), 0)
        e["state"] = "worse" if RANK[sev] > was else "quiet" if RANK[sev] < was else "still_open"
        e.update(severity=sev, last_seen=snapshot_id, last_seen_at=captured_at, misses=0, title=_title(f) or e.get("title", ""))
        if RANK[sev] > RANK.get(e.get("peak"), 0):
            e["peak"] = sev

    gone = []
    for ident, e in list(entries.items()):
        if ident in present:
            continue
        e["misses"] = int(e.get("misses") or 0) + 1
        if e["misses"] >= gone_after:
            e["state"] = "gone"
            gone.append(e)
            del entries[ident]  # reported gone once, on this run; a return later is a new finding
        else:
            e["state"] = "still_open"  # not seen this run, not gone yet: the review still lists it as open

    ledger["entries"] = sorted(entries.values(), key=lambda e: (-RANK.get(e["severity"], 0), e["first_seen"], e["kind"], e["subject"]))
    ledger["updated"] = snapshot_id

    reviewed = set()
    life = []
    for f in review_findings:
        ident = identity(f)
        reviewed.add(ident)
        e = entries.get(ident)
        life.append(_life_of(e) if e else {"state": "new", "first_seen": snapshot_id, "first_seen_at": captured_at, "misses": 0, "tickets": {}})
    still_open = [dict(e) for e in ledger["entries"] if (e["kind"], e["subject"]) not in reviewed]
    return {"life": life, "still_open": still_open, "gone": [dict(e) for e in gone]}


def _life_of(e: dict) -> dict:
    return {"state": e["state"], "first_seen": e["first_seen"], "first_seen_at": e.get("first_seen_at"),
            "misses": e.get("misses", 0), "tickets": dict(e.get("tickets") or {})}


def update(store: Storage, engine: str, target: str, snapshot_id: str, captured_at: str | None, rules_findings: list[dict],
           review_findings: list[dict] | None = None, gone_after: int = DEFAULT_GONE_AFTER) -> dict:
    """Load the target's ledger, advance it by this run and store it. See `apply` for the return value."""
    ledger = load(store, engine, target)
    out = apply(ledger, snapshot_id, captured_at, rules_findings, review_findings, gone_after)
    save(store, engine, target, ledger)
    return out


def attach(result: dict, store: Storage, cfg, comparison: dict | None, manifest: dict) -> None:
    """The cli hook: put life, still_open and gone on a run's result. A first snapshot has nothing to compare, so
    nothing is recorded; the ledger starts with the first run that has rules findings to be present or absent from."""
    if comparison is None:
        return
    try:
        out = update(store, manifest["engine"], manifest["target"], result["snapshot"], manifest.get("captured_at"),
                     comparison.get("findings") or [], result.get("findings") or [], gone_after(getattr(cfg, "delivery", None)))
    except StorageUnavailable as exc:  # no history to compare with: this review has no life, and delivery keeps the run gate
        print(f"ledger: store unavailable, finding life skipped for this run ({exc})", file=sys.stderr)
        return
    result.update(out)


def record_tickets(store: Storage, result: dict) -> None:
    """After delivery: copy the tickets the senders put on result["life"] onto the ledger entries, and store the
    review again so the ticket is part of the record (and of a rebuild). Nothing happens when no ticket was made."""
    life = result.get("life") or []
    findings = result.get("findings") or []
    new = {identity(f): l["tickets"] for f, l in zip(findings, life) if l.get("tickets")}
    if not new:
        return
    engine, target = result["engine"], result["target"]
    try:
        ledger = load(store, engine, target)
    except StorageUnavailable as exc:  # the review itself is spooled; the ticket is still on its stored life for a rebuild
        print(f"ledger: store unavailable, ticket not recorded in the ledger ({exc})", file=sys.stderr)
        ledger = None
    for e in (ledger or {"entries": []})["entries"]:
        if t := new.get((e["kind"], e["subject"])):
            e["tickets"] = {**(e.get("tickets") or {}), **t}
    if ledger is not None:
        save(store, engine, target, ledger)
    if result.get("snapshot") and store.exists(f"{result['snapshot']}/triage.json"):
        store.put_json(f"{result['snapshot']}/triage.json", result)


def rebuild(store: Storage, engine: str, target: str, gone_after: int = DEFAULT_GONE_AFTER) -> dict:
    """Recompute the ledger from every stored review of a target, oldest first, and store it.

    A snapshot counts when it has a compare.json (the run had something to compare against), exactly the runs
    `attach` recorded. Tickets come back from the `life` each triage.json kept, so the rebuilt ledger equals the
    one built run by run.
    """
    ledger = {"entries": [], "updated": None}
    for sid in store.list_snapshots(engine, target):
        if not store.exists(f"{sid}/compare.json") or not store.exists(f"{sid}/triage.json"):
            continue
        tr = store.get_json(f"{sid}/triage.json")
        rules = store.get_json(f"{sid}/compare.json").get("findings") or []
        try:
            captured = store.get_json(f"{sid}/manifest.json").get("captured_at")
        except (FileNotFoundError, ValueError):
            captured = None
        findings = tr.get("findings") or []
        apply(ledger, sid, captured, rules, findings, gone_after)
        tickets = {identity(f): l["tickets"] for f, l in zip(findings, tr.get("life") or []) if l.get("tickets")}
        for e in ledger["entries"]:
            if t := tickets.get((e["kind"], e["subject"])):
                e["tickets"] = {**(e.get("tickets") or {}), **t}
    save(store, engine, target, ledger)
    return ledger
