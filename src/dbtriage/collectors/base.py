"""Collector protocol and the normalized summary schema every engine fills in.

summary = {
  "engine": "postgres" | "oracle" | "mysql",
  "target": str, "db": str, "version": str,
  "captured_at": ISO-8601 UTC,
  "window": {"start": ISO, "end": ISO, "mode": "scheduled"|"adhoc"|"incident"},
  "counters_since": ISO | None,      # when cumulative counters were last reset (postgres/mysql)
  "load_profile": {metric: number},  # engine-specific but stable names per engine
  "top_sql": [{"sql_id", "text", "calls", "total_time_ms", "mean_time_ms", "rows",
               "io_blocks_read", "io_blocks_hit", "temp_blocks", "plan_hash"?, "cpu_time_ms"?}],  # cpu: oracle, sqlserver
  "wait_events": [{"class", "event", "count"|"time_ms"}],
  "sessions": [{"state", "wait_class", "wait_event", "duration_s", "text"}],   # long-running / blocked
  "tables": [{"name", "seq_scan", "idx_scan", "live_rows", "dead_rows", "last_vacuum"}],
  "settings": {name: value},
  "findings": [{"id", "finding", "impact_pct"?, "recommendation"?}],  # engine-native (ADDM) if any
}
raw = engine-native payload (e.g. the AWR text report, full pg_stat_statements rows).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from dbtriage.config import Target


@dataclass
class Window:
    start: datetime
    end: datetime
    mode: str = "adhoc"  # scheduled | adhoc | incident

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "mode": self.mode}


@dataclass
class Snapshot:
    summary: dict[str, Any]
    raw: dict[str, Any]
    reports: dict[str, str] = field(default_factory=dict)  # name -> text/html (AWR report, ADDM report...)


class Collector(Protocol):
    target: Target

    def collect(self, window: Window) -> Snapshot: ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def num(v) -> float | int | None:
    """Coerce DB numerics (Decimal, str) to plain JSON numbers."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, 3)
