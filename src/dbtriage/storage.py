"""Snapshot storage: local filesystem (or an NFS/SMB mount), S3-compatible object store, or Azure Blob.

Layout: <engine>/<target>/<YYYY-MM-DD>/<YYYYMMDDTHHMMSSZ>/{manifest,raw,summary,triage}.json
A snapshot id is the directory part: "postgres/demo/2026-11-05/20261105T101500Z".
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse


class Storage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]: ...

    # --- helpers shared by both backends ---
    def put_json(self, key: str, obj) -> None:
        self.put(key, json.dumps(obj, indent=1, sort_keys=True, default=str).encode())

    def get_json(self, key: str):
        return json.loads(self.get(key))

    def put_text(self, key: str, text: str) -> None:
        self.put(key, text.encode())

    def list_snapshots(self, engine: str, target: str) -> list[str]:
        """Snapshot ids for a target, oldest first."""
        prefix = f"{engine}/{target}/"
        ids = {k.rsplit("/", 1)[0] for k in self.list_keys(prefix) if k.endswith("/manifest.json")}
        return sorted(ids)

    def latest(self, engine: str, target: str) -> str | None:
        ids = self.list_snapshots(engine, target)
        return ids[-1] if ids else None

    def previous(self, engine: str, target: str, snapshot_id: str) -> str | None:
        ids = self.list_snapshots(engine, target)
        earlier = [i for i in ids if i < snapshot_id]
        return earlier[-1] if earlier else None

    def captured_at(self, engine: str, target: str) -> dict[str, str]:
        """{snapshot_id: captured_at ISO} for a target (reads each manifest; fine for the few hundred we keep)."""
        out = {}
        for sid in self.list_snapshots(engine, target):
            try:
                out[sid] = self.get_json(f"{sid}/manifest.json")["captured_at"]
            except (KeyError, FileNotFoundError, ValueError):
                continue
        return out

    def pick_baseline(self, engine: str, target: str, snapshot_id: str, policy: str | None = None,
                      pinned: str | None = None, explicit: str | None = None) -> tuple[str | None, str, str]:
        """Baseline for a snapshot according to the policy (see dbtriage.baseline.choose)."""
        from dbtriage.baseline import choose

        if not policy and not pinned and not explicit:
            prev = self.previous(engine, target, snapshot_id)
            return prev, "previous", "previous snapshot" if prev else "first snapshot for this target"
        return choose(self.captured_at(engine, target), snapshot_id, policy, pinned, explicit)


_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")


def key_ok(key: str, prefix: bool = False) -> bool:
    """A storage key is '/'-separated segments of ASCII letters, digits, '.', '_' and '-', none of them '.' or '..' and
    none ending in '.'. An allowlist, so nothing a filesystem treats specially gets through: no absolute path, no
    backslash, ':' (C:/x, file:stream), control character or space, and no '.. ' or 'x.', which Windows strips to
    '..' and 'x'. Every key dbtriage writes fits. A prefix may end with '/' and may be empty (everything)."""
    if not isinstance(key, str):
        return False
    if prefix:
        if key == "":
            return True
        key = key[:-1] if key.endswith("/") else key
    from dbtriage.config import windows_reserved

    return key != "" and all(_SEGMENT.fullmatch(part) and part not in (".", "..") and not part.endswith(".")
                             and not windows_reserved(part) for part in key.split("/"))



class LocalStorage(Storage):
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str, prefix: bool = False) -> Path:
        """The file for a key. Keys come from snapshot ids and file names that requests and models supply, so one
        that could leave the store ('..', an absolute path) is treated as missing rather than followed."""
        if not key_ok(key, prefix):
            raise FileNotFoundError(f"no such key in the store: {key!r}")
        return self.root / key

    def put(self, key: str, data: bytes) -> None:
        if not key_ok(key):
            raise ValueError(f"not a valid storage key: {key!r}")
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def exists(self, key: str) -> bool:
        try:
            return self._p(key).exists()
        except FileNotFoundError:
            return False

    def list_keys(self, prefix: str) -> list[str]:
        try:
            base = self._p(prefix, prefix=True)
        except FileNotFoundError:
            return []
        if not base.exists():
            return []
        keys = (p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file())  # '/' on Windows too
        return sorted(k for k in keys if key_ok(k))  # only what get() will read: a stray file never breaks a listing


class S3Storage(Storage):
    """Works with AWS S3, MinIO, Ceph RGW, etc. Endpoint comes from AWS_ENDPOINT_URL."""

    def __init__(self, bucket: str, prefix: str = ""):
        import boto3  # imported lazily so local mode needs no AWS deps at runtime

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        from botocore.config import Config as BotoConfig

        # short timeouts and few retries: an unreachable store should cost seconds, then the spool takes over
        self.s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
                               config=BotoConfig(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2, "mode": "standard"}))

    def _k(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        try:
            self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data)
        except self.s3.exceptions.NoSuchBucket:
            self.s3.create_bucket(Bucket=self.bucket)
            self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=data)

    def get(self, key: str) -> bytes:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read()
        except self.s3.exceptions.ClientError as exc:  # a missing key is FileNotFoundError, as in every other store
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                raise FileNotFoundError(key) from None
            raise

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except self.s3.exceptions.ClientError:
            return False

    def list_keys(self, prefix: str) -> list[str]:
        full = self._k(prefix)
        strip = len(self.prefix) + 1 if self.prefix else 0
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][strip:])
        return sorted(keys)


class AzureBlobStorage(Storage):
    """Azure Blob Storage: az://<container>/<prefix>. Auth, in order: AZURE_STORAGE_CONNECTION_STRING;
    else AZURE_STORAGE_ACCOUNT with AZURE_STORAGE_SAS_TOKEN or, failing that, DefaultAzureCredential
    (managed identity, workload identity, az login). AZURE_STORAGE_ENDPOINT overrides the account URL
    (Azurite: http://127.0.0.1:10000/devstoreaccount1). No credential is ever printed."""

    def __init__(self, container: str, prefix: str = "", client=None):
        self.container_name = container
        self.prefix = prefix.strip("/")
        self.container = client if client is not None else self._connect().get_container_client(container)

    @staticmethod
    def _connect():
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            raise SystemExit("Azure Blob storage needs the azure extra: install dbtriage[azure] (azure-storage-blob, azure-identity)")
        conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        timeouts = {"connection_timeout": 5, "read_timeout": 15, "retry_total": 2}  # seconds, then the spool takes over
        if conn:
            return BlobServiceClient.from_connection_string(conn, **timeouts)
        account = os.environ.get("AZURE_STORAGE_ACCOUNT")
        url = os.environ.get("AZURE_STORAGE_ENDPOINT") or (f"https://{account}.blob.core.windows.net" if account else None)
        if not url:
            raise SystemExit("Azure Blob storage: set AZURE_STORAGE_CONNECTION_STRING, or AZURE_STORAGE_ACCOUNT (plus AZURE_STORAGE_SAS_TOKEN or a managed identity / az login)")
        sas = os.environ.get("AZURE_STORAGE_SAS_TOKEN")
        if sas:
            return BlobServiceClient(account_url=url, credential=sas, **timeouts)
        from azure.identity import DefaultAzureCredential

        return BlobServiceClient(account_url=url, credential=DefaultAzureCredential(), **timeouts)

    def _k(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self.container.upload_blob(self._k(key), data, overwrite=True)
        except ResourceNotFoundError:
            self.container.create_container()
            self.container.upload_blob(self._k(key), data, overwrite=True)

    def get(self, key: str) -> bytes:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.container.download_blob(self._k(key)).readall()
        except ResourceNotFoundError:
            raise FileNotFoundError(key)

    def exists(self, key: str) -> bool:
        return self.container.get_blob_client(self._k(key)).exists()

    def list_keys(self, prefix: str) -> list[str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        try:
            return sorted(b.name[strip:] for b in self.container.list_blobs(name_starts_with=self._k(prefix)))
        except Exception as exc:  # container not created yet: nothing stored
            if type(exc).__name__ == "ResourceNotFoundError":
                return []
            raise


class StorageUnavailable(RuntimeError):
    """The remote store cannot be reached right now (as opposed to: the key does not exist)."""


_UNAVAILABLE = {"OperationalError", "InterfaceError", "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                "ConnectionClosedError", "ServiceRequestError", "ServiceResponseError", "ConnectionError", "TimeoutError"}


def is_unavailable(exc: BaseException) -> bool:
    """Connectivity failures from any backend's client library, matched by class name so no SDK is imported."""
    if isinstance(exc, StorageUnavailable):
        return True
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return any(c.__name__ in _UNAVAILABLE for c in type(exc).__mro__)


class PostgresStorage(Storage):
    """Objects in one table of a Postgres-compatible database. JSON objects go into a jsonb column (queryable:
    trends, SQL history, search), everything else (AWR/ADDM text, triage.md) into bytea. The key column uses the
    C collation so prefix listing is an index range scan."""

    TABLE = "dbtriage_objects"

    def __init__(self, url: str):
        from dbtriage.pgstore import PgClient

        self.client = PgClient(url, [
            f"CREATE TABLE IF NOT EXISTS {self.TABLE} (key text COLLATE \"C\" PRIMARY KEY, doc jsonb, data bytea,"
            " updated_at timestamptz NOT NULL DEFAULT now(), CHECK ((doc IS NULL) <> (data IS NULL)))",
        ])
        self.where = self.client.where

    def _run(self, sql: str, params: tuple = (), fetch: str | None = None):
        try:
            return self.client.run(sql, params, fetch)
        except Exception as exc:  # noqa: BLE001
            if is_unavailable(exc):
                raise StorageUnavailable(f"postgres store {self.where}: {type(exc).__name__}") from exc
            raise

    def put(self, key: str, data: bytes) -> None:
        doc = None
        if key.endswith(".json"):
            try:
                doc = json.dumps(json.loads(data))
                if "\\u0000" in doc:  # jsonb cannot hold NUL characters
                    doc = None
            except (ValueError, UnicodeDecodeError):
                doc = None
        self._run(f"INSERT INTO {self.TABLE} (key, doc, data) VALUES (%s, %s::jsonb, %s) "
                  "ON CONFLICT (key) DO UPDATE SET doc = EXCLUDED.doc, data = EXCLUDED.data, updated_at = now()",
                  (key, doc, None if doc is not None else data))

    def get(self, key: str) -> bytes:
        row = self._run(f"SELECT doc::text, data FROM {self.TABLE} WHERE key = %s", (key,), "one")
        if row is None:
            raise FileNotFoundError(key)
        return row[0].encode() if row[0] is not None else bytes(row[1])

    def exists(self, key: str) -> bool:
        return self._run(f"SELECT 1 FROM {self.TABLE} WHERE key = %s", (key,), "one") is not None

    def list_keys(self, prefix: str) -> list[str]:
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return [r[0] for r in self._run(f"SELECT key FROM {self.TABLE} WHERE key LIKE %s ORDER BY key", (like,), "all")]

    def list_snapshots(self, engine: str, target: str) -> list[str]:
        like = f"{engine}/{target}/".replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%/manifest.json"
        rows = self._run(f"SELECT key FROM {self.TABLE} WHERE key LIKE %s ORDER BY key", (like,), "all")
        return [r[0].rsplit("/", 1)[0] for r in rows]


class ResilientStorage(Storage):
    """Wraps a remote store (Postgres, S3, Azure Blob) so its outage degrades the app instead of breaking it.

    Writes that cannot reach the store go to a local spool directory and are pushed on the next successful write,
    on each scheduler tick, or by `dbtriage flush-spool`. Reads merge the spool with whatever the store returns; a
    read of a key only the store holds raises StorageUnavailable, which the web view turns into a 503 page. `degraded`
    holds the last connectivity error (None while the store answers) for banners and /health."""

    RETRY_S = 30.0  # after a failure, skip the store for this long so pages do not wait on every call

    def __init__(self, primary: Storage, spool: LocalStorage):
        self.primary, self.spool = primary, spool
        self.where = getattr(primary, "where", type(primary).__name__)
        self.degraded: str | None = None
        self._down_until = 0.0
        import threading

        self._flush_lock = threading.Lock()

    def _down(self) -> bool:
        import time

        return time.monotonic() < self._down_until

    def _call(self, fn, *args):
        import time

        if self._down():
            raise StorageUnavailable(self.degraded or "store unavailable")
        try:
            out = fn(*args)
        except Exception as exc:  # noqa: BLE001
            if is_unavailable(exc):
                self.degraded = str(exc) if isinstance(exc, StorageUnavailable) else f"{type(exc).__name__}: {str(exc)[:200]}"
                self._down_until = time.monotonic() + self.RETRY_S
                raise StorageUnavailable(self.degraded) from exc
            raise
        if self.degraded:
            self.degraded = None
        return out

    def probe(self) -> bool:
        """Health check: once the retry window has passed, ask the store one cheap question so the degraded flag
        clears (and the spool drains) without waiting for a page view or a write. Returns True when the store answers."""
        if self._down():
            return False
        try:
            self._call(self.primary.exists, ".dbtriage-probe")
        except StorageUnavailable:
            return False
        self.flush()
        return True

    def spooled(self) -> list[str]:
        return [k for k in self.spool.list_keys("") if not k.rsplit("/", 1)[-1].startswith(".")]

    def put(self, key: str, data: bytes) -> None:
        try:
            self._call(self.primary.put, key, data)
        except StorageUnavailable:
            self.spool.put(key, data)
            return
        if self.spool.exists(key):  # an older spooled copy must not overwrite this one later
            self.spool._p(key).unlink(missing_ok=True)
        self.flush()

    def get(self, key: str) -> bytes:
        if self.spool.exists(key):
            return self.spool.get(key)
        return self._call(self.primary.get, key)

    def exists(self, key: str) -> bool:
        if self.spool.exists(key):
            return True
        try:
            return self._call(self.primary.exists, key)
        except StorageUnavailable:
            return False

    def list_keys(self, prefix: str) -> list[str]:
        local = self.spool.list_keys(prefix)
        try:
            remote = self._call(self.primary.list_keys, prefix)
        except StorageUnavailable:
            remote = []
        return sorted(set(local) | set(remote))

    def flush(self) -> tuple[int, int]:
        """Push spooled objects to the store; returns (pushed, still spooled). Never raises on an outage."""
        keys = self.spooled()
        if not keys or self._down() or not self._flush_lock.acquire(blocking=False):
            return 0, len(keys)
        pushed = 0
        try:
            # manifests last: a snapshot only becomes visible in the store once all its files are there
            for key in sorted(keys, key=lambda k: (k.endswith("/manifest.json"), k)):
                try:
                    self._call(self.primary.put, key, self.spool.get(key))
                except StorageUnavailable:
                    break
                self.spool._p(key).unlink(missing_ok=True)
                pushed += 1
            for d in sorted({self.spool._p(k).parent for k in keys}, key=lambda p: -len(p.parts)):
                while d != self.spool.root and d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
        finally:
            self._flush_lock.release()
        return pushed, len(keys) - pushed


def open_storage(url: str, spool_dir: str | os.PathLike | None = None) -> Storage:
    """file:// stays as it is; a remote store (postgres://, s3://, az://) is wrapped in ResilientStorage with a local
    spool (DBTRIAGE_SPOOL, else spool_dir, else ./spool) so it can be down without taking the app with it."""
    u = urlparse(url)
    if u.scheme in ("", "file"):
        return LocalStorage((u.netloc or "") + u.path)
    if u.scheme in ("postgres", "postgresql"):
        primary: Storage = PostgresStorage(url)
    elif u.scheme == "s3":
        primary = S3Storage(u.netloc, u.path)
    elif u.scheme in ("az", "azure"):
        primary = AzureBlobStorage(u.netloc, u.path)
    else:
        raise SystemExit(f"unsupported storage url: {url}")
    if os.environ.get("DBTRIAGE_NO_SPOOL"):
        return primary
    return ResilientStorage(primary, LocalStorage(os.environ.get("DBTRIAGE_SPOOL") or spool_dir or "spool"))


def describe(store: Storage) -> str:
    """Where a store lives, safe to show (passwords masked)."""
    if isinstance(store, LocalStorage):
        return f"file://{store.root}"
    return getattr(store, "where", type(store).__name__)
