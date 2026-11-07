"""Snapshots taken on the database itself: the one thing dbtriage can write, and only when you ask for it.

dbtriage only reads by default. Teams that want it to take the database's own snapshot before collecting (on Oracle,
an AWR snapshot, so the review's window ends now instead of at the last hourly snapshot) turn on two switches, and
both are needed:

    [collect]
    allow_db_writes = true            # targets.toml only: the Settings page and its API cannot change it

    [targets.prod]
    create_snapshot = "always"        # every run; "incident" for incident runs only (true means this too)

The first is the owner of targets.toml saying dbtriage may write at all; the second picks the databases. With
either off, nothing is ever written, and a database that asks for snapshots while writes are off gets a note in
each snapshot saying why none was taken.

Built in: oracle (AWR; needs diagnostics_pack and EXECUTE on DBMS_WORKLOAD_REPOSITORY, which reading the AWR
report already needs). Other engines, or another method such as Statspack or pg_profile's take_sample(), plug in
as a package entry point in the "dbtriage.db_snapshot" group, named after the engine:

    [project.entry-points."dbtriage.db_snapshot"]
    postgres = "my_pkg.snap:take"

A taker is a function (target, window) -> str | None. It opens its own connection (collection always stays on a
read-only session), takes the snapshot, and returns what it did, or None when none was needed (one was taken a
moment ago). It raises when it cannot: the run carries on without it and the snapshot records why.
"""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Callable

from dbtriage.collectors.base import Window
from dbtriage.config import Target

GROUP = "dbtriage.db_snapshot"
COLLECT_FIELDS = ("allow_db_writes",)  # the [collect] section of targets.toml
MODES = ("off", "incident", "always")
Taker = Callable[[Target, Window], "str | None"]


def mode_of(value) -> str:
    """create_snapshot as one of MODES: true (the old switch) means incident runs only."""
    if value is True:
        return "incident"
    if value in (None, False, "", "off", "false"):
        return "off"
    if value in MODES:
        return value
    raise ValueError(f"create_snapshot: one of {', '.join(MODES)}")


def writes_allowed(collect: dict | None) -> bool:
    return (collect or {}).get("allow_db_writes") is True  # a real boolean in targets.toml, nothing else


def _oracle(target: Target, window: Window) -> str | None:
    from dbtriage.collectors.oracle import take_awr_snapshot

    return take_awr_snapshot(target, window)


BUILTIN: dict[str, Taker] = {"oracle": _oracle}


def takers() -> dict[str, Taker]:
    """Built-in takers, then any installed plugins (a plugin for an engine replaces the built-in one)."""
    found = dict(BUILTIN)
    for ep in entry_points(group=GROUP):
        found[ep.name] = _lazy(ep)
    return found


def _lazy(ep) -> Taker:
    """Import a plugin only when a run needs it, so a broken plugin fails that run's snapshot, not every import."""
    def call(target: Target, window: Window):
        return ep.load()(target, window)

    call.__qualname__ = f"plugin {ep.value}"
    return call


def take(collect: dict | None, target: Target, window: Window) -> dict | None:
    """Take the database's own snapshot if this run should, before collecting. Returns what happened, for the
    snapshot's manifest, or None when the target does not ask for one. Never raises: a snapshot that could not be
    taken must not lose the run."""
    try:
        mode = mode_of(target.extra.get("create_snapshot"))
    except ValueError as exc:
        return {"status": "skipped", "reason": str(exc)}
    if mode == "off" or (mode == "incident" and window.mode != "incident"):
        return None
    if not writes_allowed(collect):
        return {"status": "skipped", "reason": "allow_db_writes is off in targets.toml ([collect]), so dbtriage only reads"}
    found = takers().get(target.engine)
    if found is None:
        return {"status": "skipped", "reason": f"nothing takes snapshots on {target.engine}; a plugin can (entry point group {GROUP})"}
    try:
        did = found(target, window)
    except Exception as exc:  # noqa: BLE001 - the run goes on with what the database already has
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:300]}
    return {"status": "taken", "detail": str(did)} if did else {"status": "not needed", "detail": "a recent snapshot exists"}
