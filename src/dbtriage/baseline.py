"""Which earlier snapshot a new one is compared against.

Policies:
  previous              the snapshot just before the current one (default)
  same-time-yesterday   the snapshot nearest the same wall-clock time 24h earlier (±tolerance)
  same-time-last-week   the same, 7 days earlier: right for weekly batch cycles
  pinned                a snapshot id fixed per target (a known-good run), until someone re-pins it
An explicit snapshot id always wins. Every policy that finds nothing falls back to `previous`
and says so in the reason, so a review never silently compares against the wrong thing.
"""
from __future__ import annotations

from datetime import datetime, timedelta

POLICIES = ("previous", "same-time-yesterday", "same-time-last-week", "pinned")
LABELS = {"previous": "previous snapshot", "same-time-yesterday": "same time yesterday",
          "same-time-last-week": "same time last week", "pinned": "pinned baseline"}
OFFSETS = {"same-time-yesterday": timedelta(days=1), "same-time-last-week": timedelta(days=7)}


def _ts(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.now().astimezone().tzinfo)


def choose(snapshots: dict[str, str], current: str, policy: str | None = None, pinned: str | None = None,
           explicit: str | None = None, tolerance: timedelta = timedelta(hours=2)) -> tuple[str | None, str, str]:
    """Pick the baseline for `current`.

    snapshots: {snapshot_id: captured_at ISO} for the target (may include `current`).
    Returns (baseline_id or None, effective policy, human-readable reason).
    """
    policy = policy or "previous"
    if policy not in POLICIES:
        raise ValueError(f"baseline policy must be one of {POLICIES}, not {policy!r}")
    earlier = sorted(i for i in snapshots if i < current)
    prev = earlier[-1] if earlier else None

    if explicit:
        if explicit in snapshots and explicit != current:
            return explicit, "explicit", f"requested snapshot {explicit}"
        return prev, "previous", f"requested baseline {explicit} not found; fell back to previous snapshot" if prev else \
            f"requested baseline {explicit} not found and no earlier snapshot"

    if policy == "pinned":
        if pinned and pinned in snapshots and pinned != current:
            return pinned, "pinned", f"pinned baseline {pinned}"
        why = "no pinned baseline configured" if not pinned else f"pinned baseline {pinned} not found"
        return prev, "previous", f"{why}; fell back to previous snapshot" if prev else f"{why}; no earlier snapshot"

    if policy in OFFSETS:
        if current not in snapshots:
            raise ValueError(f"current snapshot {current} needs a captured_at to apply {policy}")
        want = _ts(snapshots[current]) - OFFSETS[policy]
        candidates = [(abs(_ts(snapshots[i]) - want), i) for i in earlier]
        near = [(d, i) for d, i in candidates if d <= tolerance]
        if near:
            d, i = min(near)
            mins = int(d.total_seconds() // 60)
            return i, policy, f"{LABELS[policy]} ({i.rsplit('/', 1)[-1]}, {mins} min off target)"
        return prev, "previous", f"nothing within ±{int(tolerance.total_seconds() // 3600)}h of {LABELS[policy]}; fell back to previous snapshot" \
            if prev else f"nothing within tolerance of {LABELS[policy]} and no earlier snapshot"

    return prev, "previous", "previous snapshot" if prev else "first snapshot for this target"
