"""A finding that stays a finding: the per-target ledger of open findings.

Every run writes a fresh review, so the same plan change could page the channel every hour. The ledger
remembers what was open after the last run and gives each of this run's findings a state:

    new         not in the ledger
    still_open  present, same severity as last time
    worse       present, higher severity than last time
    quiet       present, below its own severity last time
    returned    back after being absent (or after being reported gone, within the tombstone window)
    recurring   returned at about the same interval as before: the nightly batch, the Monday job. Quiet unless worse
    gone        absent for `gone_after` times its own period, or `gone_after` runs when it has no period yet
                (default 3). Said once; the entry then waits as a tombstone so a return is a return, not a new finding

Cadence, not runs, decides gone. A finding seen every hourly run has a period of one hour and is gone after three.
A statement that shows up in the 02:00 run each night has a period of a day, so three quiet hours do not clear it
and its ticket is not told "cleared" every morning. The period is the median of the gaps between its returns;
until a finding has returned once, the run count is all there is.

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
from copy import deepcopy
from datetime import datetime
from statistics import median

from dbtriage.findings import identity
from dbtriage.storage import Storage, StorageUnavailable

RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
STATES = ("new", "still_open", "worse", "quiet", "returned", "recurring", "gone")
DEFAULT_GONE_AFTER = 3
TOMBSTONE_DAYS = 7  # how long a gone finding is remembered, so a weekly job that returns is "returned", not "new"
RETURNS_KEPT = 5  # gaps between returns kept per finding; the median of them is its period
RECURRING_TOLERANCE = 0.25  # a return within this fraction of the period counts as the same cadence


def key(engine: str, target: str) -> str:
    return f"{engine}/{target}/findings.json"


def gone_after(delivery_cfg: dict | None) -> int:
    """`[delivery] gone_after_runs`: runs for a finding without a period, multiples of its period once it has one.
    The threshold sits with delivery because flapping is a channel problem."""
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
        return {"entries": [], "tombstones": [], "updated": None, "prior": None, "generation": 0}
    return {"entries": list(data.get("entries") or []), "tombstones": list(data.get("tombstones") or []), "updated": data.get("updated"),
            "prior": data.get("prior"), "generation": int(data.get("generation") or 0)}


def save(store: Storage, engine: str, target: str, ledger: dict) -> None:
    ledger["generation"] = int(ledger.get("generation") or 0) + 1
    store.put_json(key(engine, target), ledger)


def _generation(store: Storage, engine: str, target: str) -> int:
    try:
        return int((store.get_json(key(engine, target)) or {}).get("generation") or 0)
    except (FileNotFoundError, ValueError):
        return 0


def commit(store: Storage, engine: str, target: str, mutate, attempts: int = 3):
    """Read-modify-write the ledger with an optimistic check: `mutate(ledger)` runs on a fresh load, and the save
    goes ahead only when nobody else saved in between (the stored generation is still the one loaded); otherwise
    the whole thing is done again on the new state. Returns what `mutate` returned on the saved attempt.

    The window between the check and the put is not closed (no store here has a conditional put), and the outage
    spool replays whole files, so two processes writing one target through an outage still end last-write-wins.
    `run_request` holds a per-target lock in-process; across processes, run one scheduler per store, and
    `rebuild-ledger` repairs the file from the stored reviews."""
    for attempt in range(attempts):
        ledger = load(store, engine, target)
        gen = ledger["generation"]
        out = mutate(ledger)
        if _generation(store, engine, target) != gen and attempt < attempts - 1:
            print(f"ledger: {engine}/{target} changed under us; applying again", file=sys.stderr)
            continue
        save(store, engine, target, ledger)
        return out


def tracked(f: dict) -> bool:
    """Only a finding with a kind has an identity; reviews stored before kinds existed are not tracked."""
    return bool(f.get("kind"))


def _sev(f: dict) -> str:
    s = f.get("severity")
    return s if s in RANK else "low"


def _title(f: dict) -> str:
    return str(f.get("summary") or f.get("title") or f.get("where") or "")[:160]


def _when(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def _seconds_between(a: str | None, b: str | None) -> float | None:
    ta, tb = _when(a), _when(b)
    if ta is None or tb is None or (ta.tzinfo is None) != (tb.tzinfo is None):
        return None
    return (tb - ta).total_seconds()


def period_of(e: dict) -> float | None:
    """The finding's period in seconds: the median gap between its returns, None until it has returned once."""
    gaps = [g for g in (e.get("returns") or []) if isinstance(g, (int, float)) and g > 0]
    return float(median(gaps)) if gaps else None


def describe_period(seconds: float | None) -> str:
    """"about every 24h" for the page and the markdown; empty without a period."""
    if not seconds:
        return ""
    if seconds >= 36 * 3600:
        return f"about every {round(seconds / 86400)}d"
    if seconds >= 3600:
        return f"about every {round(seconds / 3600)}h"
    return f"about every {max(1, round(seconds / 60))}min"


def _is_gone(e: dict, now: str | None, gone_after: int) -> bool:
    """Gone by cadence when the finding has a period and the clock is known; by run count otherwise."""
    period = period_of(e)
    absent = _seconds_between(e.get("last_seen_at"), now)
    if period and absent is not None:
        return absent > gone_after * period
    return int(e.get("misses") or 0) >= gone_after


def _came_back(e: dict, f: dict, snapshot_id: str, captured_at: str | None) -> None:
    """A finding seen again after being absent: record the gap, and decide returned, recurring or worse."""
    sev = _sev(f)
    was = RANK.get(e.get("severity"), 0)
    gap = _seconds_between(e.get("last_seen_at"), captured_at)
    before = period_of(e)
    if gap and gap > 0:
        e["returns"] = ([*(e.get("returns") or []), gap])[-RETURNS_KEPT:]
    same_cadence = bool(before and gap and abs(gap - before) <= RECURRING_TOLERANCE * before)
    e["state"] = "worse" if RANK[sev] > was else "recurring" if same_cadence else "returned"
    e.update(severity=sev, last_seen=snapshot_id, last_seen_at=captured_at, misses=0, title=_title(f) or e.get("title", ""))
    if RANK[sev] > RANK.get(e.get("peak"), 0):
        e["peak"] = sev


def apply(ledger: dict, snapshot_id: str, captured_at: str | None, rules_findings: list[dict],
          review_findings: list[dict] | None = None, gone_after: int = DEFAULT_GONE_AFTER) -> dict:
    """Advance the ledger (in place) by one run and return what the run should say about its findings.

    Returns {"life": [...parallel to review_findings...], "still_open": [...], "gone": [...]}. `life` items
    carry state, first_seen, first_seen_at, misses and tickets; `still_open` is every open entry that is not
    among the review's findings (so a still-open item never takes one of the review's five slots); `gone`
    is the entries reported gone on this run, which are then dropped from the ledger.
    """
    review_findings = review_findings or []
    if ledger.get("updated") == snapshot_id and ledger.get("prior") is not None:
        # the same snapshot triaged again (a model review after the rules, a re-run): start from where it stood
        # before, or every re-triage would count as another run the open findings were missing from
        ledger["entries"], ledger["tombstones"] = deepcopy(ledger["prior"]["entries"]), deepcopy(ledger["prior"]["tombstones"])
    ledger["prior"] = {"entries": deepcopy(ledger["entries"]), "tombstones": deepcopy(ledger.get("tombstones") or [])}
    entries = {(e["kind"], e["subject"]): e for e in ledger["entries"]}
    tombstones = {(e["kind"], e["subject"]): e for e in ledger.get("tombstones") or []}
    # what is present this run, with the review's finding first where it matched (title and severity come from it).
    # A review finding without a kind was stored before kinds existed; it has no identity, so it is not presence
    # (otherwise every such finding would collapse into one phantom ("other", "") entry on a rebuild).
    present: dict[tuple[str, str], dict] = {}
    for f in [f for f in review_findings if tracked(f)] + list(rules_findings or []):
        present.setdefault(identity(f), f)

    for ident, f in present.items():
        sev = _sev(f)
        e = entries.get(ident)
        if e is None and ident in tombstones:  # reported gone, now back: the same finding, with its ticket and its history
            e = entries[ident] = tombstones.pop(ident)
            e.pop("gone_at", None)
            _came_back(e, f, snapshot_id, captured_at)
            continue
        if e is None:
            entries[ident] = {"kind": ident[0], "subject": ident[1], "title": _title(f),
                              "first_seen": snapshot_id, "first_seen_at": captured_at, "last_seen": snapshot_id, "last_seen_at": captured_at,
                              "severity": sev, "peak": sev, "state": "new", "misses": 0, "returns": [], "tickets": {}}
            continue
        if int(e.get("misses") or 0) > 0:  # absent last run(s), back now
            _came_back(e, f, snapshot_id, captured_at)
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
        if _is_gone(e, captured_at, gone_after):
            e["state"] = "gone"
            gone.append(e)
            del entries[ident]  # reported gone once, on this run
            tombstones[ident] = dict(e, gone_at=captured_at)  # and remembered, so a return is a return
        else:
            e["state"] = "still_open"  # not seen this run, not gone yet: the review still lists it as open

    # tombstones expire after TOMBSTONE_DAYS; without a clock they are kept (a rebuild supplies one)
    if captured_at:
        tombstones = {k: t for k, t in tombstones.items()
                      if (age := _seconds_between(t.get("gone_at"), captured_at)) is None or age <= TOMBSTONE_DAYS * 86400}
    ledger["tombstones"] = sorted(tombstones.values(), key=lambda e: (e.get("gone_at") or "", e["kind"], e["subject"]))

    ledger["entries"] = sorted(entries.values(), key=lambda e: (-RANK.get(e["severity"], 0), e["first_seen"], e["kind"], e["subject"]))
    ledger["updated"] = snapshot_id

    reviewed = set()
    life = []
    for f in review_findings:
        if not tracked(f):  # legacy finding: life stays parallel to the findings, but says nothing and never fires
            life.append({"state": "untracked", "first_seen": None, "first_seen_at": None, "misses": 0, "returns": 0, "period_s": None, "tickets": {}})
            continue
        ident = identity(f)
        reviewed.add(ident)
        e = entries.get(ident)
        life.append(_life_of(e) if e else {"state": "new", "first_seen": snapshot_id, "first_seen_at": captured_at, "misses": 0,
                                           "returns": 0, "period_s": None, "tickets": {}})
    # display rows in the same shape as `life` (period_s, returns as a count), plus what names the finding
    still_open = [dict(e, **_life_of(e)) for e in ledger["entries"] if (e["kind"], e["subject"]) not in reviewed]
    return {"life": life, "still_open": still_open, "gone": [dict(e) for e in gone]}


def _life_of(e: dict) -> dict:
    return {"state": e["state"], "first_seen": e["first_seen"], "first_seen_at": e.get("first_seen_at"),
            "misses": e.get("misses", 0), "returns": len(e.get("returns") or []), "period_s": period_of(e),
            "tickets": dict(e.get("tickets") or {})}


def update(store: Storage, engine: str, target: str, snapshot_id: str, captured_at: str | None, rules_findings: list[dict],
           review_findings: list[dict] | None = None, gone_after: int = DEFAULT_GONE_AFTER) -> dict | None:
    """Load the target's ledger, advance it by this run and store it. See `apply` for the return value.

    None when the snapshot is older than the ledger's last run: re-triaging history must not rewind the ledger,
    so that review carries no life and delivery keeps the whole-run gate. The latest snapshot may be triaged again
    (apply restores the state before it)."""
    def mutate(ledger: dict):
        if ledger.get("updated") and snapshot_id < ledger["updated"]:  # ids sort by capture time within a target
            return None
        return apply(ledger, snapshot_id, captured_at, rules_findings, review_findings, gone_after)

    return commit(store, engine, target, mutate)


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
    if out is None:
        print("ledger: snapshot is older than the ledger's last run; finding life not recorded for it", file=sys.stderr)
        return
    result.update(out)


def record_tickets(store: Storage, result: dict) -> None:
    """After delivery: copy the tickets the senders put on result["life"] onto the ledger entries, and store the
    review again so the ticket is part of the record (and of a rebuild). Nothing happens when no ticket was made."""
    life = result.get("life") or []
    findings = result.get("findings") or []
    new = {identity(f): l["tickets"] for f, l in zip(findings, life) if l.get("tickets") and tracked(f)}
    for e in result.get("still_open") or []:  # an open finding outside the five that fired and got a ticket
        if e.get("tickets") and e.get("kind"):
            new[(e["kind"], e["subject"])] = {**new.get((e["kind"], e["subject"]), {}), **e["tickets"]}
    if not new:
        return
    engine, target = result["engine"], result["target"]

    def mutate(ledger: dict):
        for e in ledger["entries"]:
            if t := new.get((e["kind"], e["subject"])):
                e["tickets"] = {**(e.get("tickets") or {}), **t}

    try:
        commit(store, engine, target, mutate)
    except StorageUnavailable as exc:  # the review itself is spooled; the ticket is still on its stored life for a rebuild
        print(f"ledger: store unavailable, ticket not recorded in the ledger ({exc})", file=sys.stderr)
    if result.get("snapshot") and store.exists(f"{result['snapshot']}/triage.json"):
        store.put_json(f"{result['snapshot']}/triage.json", result)


def rebuild(store: Storage, engine: str, target: str, gone_after: int = DEFAULT_GONE_AFTER) -> dict:
    """Recompute the ledger from every stored review of a target, oldest first, and store it.

    A snapshot counts when it has a compare.json (the run had something to compare against), exactly the runs
    `attach` recorded. Tickets come back from the `life` each triage.json kept, so the rebuilt ledger equals the
    one built run by run.
    """
    ledger = {"entries": [], "tombstones": [], "updated": None, "prior": None, "generation": _generation(store, engine, target)}
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
        tickets = {identity(f): l["tickets"] for f, l in zip(findings, tr.get("life") or []) if l.get("tickets") and tracked(f)}
        for so in tr.get("still_open") or []:
            if so.get("tickets") and so.get("kind"):
                tickets[(so["kind"], so["subject"])] = {**tickets.get((so["kind"], so["subject"]), {}), **so["tickets"]}
        for e in ledger["entries"]:
            if t := tickets.get((e["kind"], e["subject"])):
                e["tickets"] = {**(e.get("tickets") or {}), **t}
    save(store, engine, target, ledger)
    return ledger
