"""One lock per target in this process, shared by the pipeline and the store.

Two runs of one database (two alerts, a webhook beside the schedule) take turns in `run_request`, so the ledger's
read-modify-write and the ticket bookkeeping never interleave; the outage spool holds the same lock while it replays
that database's ledger file, so a flush cannot put an older copy over a run's fresh save.
"""
from __future__ import annotations

import threading

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def target_lock(name: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(name, threading.Lock())
