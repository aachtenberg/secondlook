"""Shared plumbing for keeping dbtriage's own data in PostgreSQL (snapshot store and settings).

Any Postgres-compatible server works: plain PostgreSQL, Aurora/RDS, Cloud SQL, AlloyDB, Azure Database
for PostgreSQL, and wire-compatible engines (CockroachDB, YugabyteDB) since only plain SQL is used:
CREATE TABLE IF NOT EXISTS, INSERT ... ON CONFLICT, bytea and jsonb.

URL: postgres://user:pass@host:5432/db?sslmode=require&schema=dbtriage
  schema   optional; tables are created there (CREATE SCHEMA IF NOT EXISTS is attempted, then search_path set)
  Every other query parameter goes to libpq unchanged. Prefer a password from PGPASSWORD or ~/.pgpass over
  one in the URL; when a URL does carry one it is masked wherever dbtriage shows the location.
psycopg is already a dependency (the Postgres collector), so this needs no extra install.
"""
from __future__ import annotations

import re
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SCHEMES = ("postgres", "postgresql")
_OWN_PARAMS = ("schema",)


def is_pg_url(value: str | None) -> bool:
    return bool(value) and urlparse(str(value)).scheme in SCHEMES


def mask_url(url: str) -> str:
    """postgres://user:secret@host/db -> postgres://user:***@host/db (host and db stay recognisable)."""
    return re.sub(r"(://[^/@:]*:)[^@]*(@)", r"\1***\2", url, count=1)


def split_url(url: str) -> tuple[str, str | None]:
    """(libpq conninfo URL, schema) with dbtriage's own query parameters removed."""
    u = urlparse(url)
    params = parse_qsl(u.query, keep_blank_values=True)
    schema = next((v for k, v in params if k == "schema"), None)
    rest = urlencode([(k, v) for k, v in params if k not in _OWN_PARAMS])
    return urlunparse(u._replace(scheme="postgresql", query=rest)), schema


class PgClient:
    """One autocommit connection shared by the web server's threads behind a lock; reconnects once on a broken
    connection; every call has a hard deadline. Fine for this workload (small, infrequent writes; reads of a few
    hundred rows)."""

    def __init__(self, url: str, ddl: list[str]):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$", split_url(url)[1] or "public"):
            raise SystemExit("postgres url: schema must be a plain identifier")
        self.url, self.ddl = url, ddl
        self.where = mask_url(url)
        self.lock = threading.Lock()
        self.conn = None

    def _connect(self):
        import psycopg

        conninfo, schema = split_url(self.url)
        kw = {} if "connect_timeout" in conninfo else {"connect_timeout": 5}  # an unreachable host must not hang a page
        if "keepalives" not in conninfo:  # notice a dead peer on an idle pooled connection
            kw.update(keepalives=1, keepalives_idle=10, keepalives_interval=3, keepalives_count=3)
        conn = psycopg.connect(conninfo, autocommit=True, application_name="dbtriage", **kw)
        if schema:
            try:
                conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            except psycopg.Error:
                pass  # no CREATE privilege: the schema must already exist
            conn.execute(f'SET search_path TO "{schema}"')
        for stmt in self.ddl:
            conn.execute(stmt)
        return conn

    DEADLINE_S = 5.0  # hard cap per call: a frozen server or a blackholed network must not hang a page

    def run(self, sql: str, params: tuple = (), fetch: str | None = None):
        """fetch: None, 'one' or 'all'. Raises TimeoutError when the database does not answer within DEADLINE_S."""
        if not self.lock.acquire(timeout=self.DEADLINE_S):
            raise TimeoutError(f"postgres {self.where}: busy for {self.DEADLINE_S:.0f}s")
        try:
            box: dict = {}

            def call():
                import psycopg

                for attempt in (1, 2):
                    try:
                        if self.conn is None or self.conn.closed:
                            self.conn = self._connect()
                        cur = self.conn.execute(sql, params)
                        box["out"] = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
                        return
                    except psycopg.OperationalError as exc:
                        self.conn = None
                        if attempt == 2:
                            box["err"] = exc
                    except BaseException as exc:  # noqa: BLE001 - handed back to the caller's thread
                        box["err"] = exc
                        return

            worker = threading.Thread(target=call, daemon=True, name="dbtriage-pg")
            worker.start()
            worker.join(self.DEADLINE_S)
            if worker.is_alive():
                self.conn = None  # abandon the stuck connection; the next call opens a fresh one
                raise TimeoutError(f"postgres {self.where}: no answer within {self.DEADLINE_S:.0f}s")
            if "err" in box:
                raise box["err"]
            return box.get("out")
        finally:
            self.lock.release()

    def close(self):
        with self.lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
