"""Engine plugins. Each collector returns the same normalized summary shape so the
comparison and the agent work the same way for Oracle, Postgres, MySQL, ..."""
from __future__ import annotations

from dbtriage.collectors.base import Collector, Snapshot
from dbtriage.config import Target


def get_collector(target: Target) -> Collector:
    if target.engine == "postgres":
        from dbtriage.collectors.postgres import PostgresCollector

        return PostgresCollector(target)
    if target.engine == "oracle":
        from dbtriage.collectors.oracle import OracleCollector

        return OracleCollector(target)
    if target.engine == "mysql":
        from dbtriage.collectors.mysql import MySQLCollector

        return MySQLCollector(target)
    if target.engine == "sqlserver":
        try:
            from dbtriage.collectors.sqlserver import SQLServerCollector
        except ModuleNotFoundError as exc:  # the driver is an optional extra
            raise SystemExit(f"target {target.name!r}: SQL Server needs its driver: uv sync --extra sqlserver ({exc})") from None

        return SQLServerCollector(target)
    raise SystemExit(f"target {target.name!r}: unsupported engine {target.engine!r}")


__all__ = ["Collector", "Snapshot", "get_collector"]
