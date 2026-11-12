"""The compare-and-set write every store offers (`version_of`, `put_if`, `Conflict`): the ledger's save goes through
it so two processes writing one database's ledger cannot overwrite each other. The Postgres case runs live when
DBTRIAGE_TEST_PG_URL is set (tests/test_pg_resilience.py)."""
import hashlib
import threading

import pytest

from dbtriage import storage as st
from dbtriage.storage import Conflict, LocalStorage, ResilientStorage, Storage
from tests.test_pg_resilience import Flaky, pg_url  # noqa: F401  (pg_url: the live-Postgres fixture)
from tests.test_storage_azure import FakeContainer, azure_core  # noqa: F401  (azure_core: fakes azure.core)

K = "postgres/demo/findings.json"


# ---------------------------------------------------------------- local disk
def test_local_put_if_matching_version_writes(tmp_path):
    s = LocalStorage(tmp_path)
    assert s.version_of(K) is None
    s.put_if(K, b"one", None)
    v1 = s.version_of(K)
    assert s.get(K) == b"one" and v1 is not None
    s.put_if(K, b"two", v1)
    assert s.get(K) == b"two" and s.version_of(K) != v1
    assert s.list_keys("postgres/") == [K]  # the lock file beside it is not an object


def test_local_put_if_refuses_a_stale_version_and_an_unexpected_file(tmp_path):
    s = LocalStorage(tmp_path)
    s.put(K, b"one")
    v1 = s.version_of(K)
    s.put(K, b"two")  # somebody else
    with pytest.raises(Conflict):
        s.put_if(K, b"mine", v1)
    with pytest.raises(Conflict):  # "must not exist" on a file that does
        s.put_if(K, b"mine", None)
    with pytest.raises(Conflict):  # a version for a file that is not there
        s.put_if("postgres/demo/other.json", b"mine", v1)
    assert s.get(K) == b"two"
    with pytest.raises(ValueError):
        s.put_if("../outside", b"x", None)


def test_local_put_if_two_threads_exactly_one_wins(tmp_path):
    s = LocalStorage(tmp_path)
    s.put(K, b"start")
    v = s.version_of(K)
    go, results = threading.Barrier(2), []

    def writer(i):
        go.wait(5)
        try:
            s.put_if(K, f"writer {i}".encode(), v)
            results.append("won")
        except Conflict:
            results.append("lost")

    threads = [threading.Thread(target=writer, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == ["lost", "won"] and s.get(K).startswith(b"writer ")


# ---------------------------------------------------------------- base class fallback
class Minimal(Storage):
    """A backend that implements only the four primitives, so it gets the base-class conditional write."""

    def __init__(self):
        self.d = {}

    def put(self, key, data):
        self.d[key] = data

    def get(self, key):
        if key not in self.d:
            raise FileNotFoundError(key)
        return self.d[key]

    def exists(self, key):
        return key in self.d

    def list_keys(self, prefix):
        return sorted(k for k in self.d if k.startswith(prefix))


def test_base_class_fallback_is_read_compare_write():
    s = Minimal()
    assert s.version_of(K) is None
    s.put_if(K, b"one", None)
    v = s.version_of(K)
    assert v == hashlib.sha256(b"one").hexdigest()  # a content hash; only equality means anything
    s.put_if(K, b"two", v)
    with pytest.raises(Conflict):
        s.put_if(K, b"three", v)
    with pytest.raises(Conflict):
        s.put_if(K, b"three", None)
    assert s.get(K) == b"two"
    s.put_json_if(K, {"a": 1}, s.version_of(K))
    assert s.get_json(K) == {"a": 1}


# ---------------------------------------------------------------- resilient wrapper
def test_resilient_delegates_and_spools_on_outage(tmp_path):
    ResilientStorage.RETRY_S = 0.0
    remote = Flaky(tmp_path / "remote")
    s = ResilientStorage(remote, LocalStorage(tmp_path / "spool"))
    one, two = b'{"generation": 1}', b'{"generation": 2}'
    s.put_if(K, one, None)
    v = s.version_of(K)
    assert remote.get(K) == one and v == remote.version_of(K)
    with pytest.raises(Conflict):  # the store's refusal comes straight through, not into the spool
        s.put_if(K, two, None)
    assert s.spooled() == []
    remote.up = False
    assert s.version_of(K) is None  # unreachable: no version to compare with
    s.put_if(K, two, v)  # spooled like any put; the generation decides the replay
    assert s.spooled() == [K] and s.get(K) == two and (tmp_path / "remote" / K).read_bytes() == one
    remote.up = True
    assert s.flush() == (1, 0) and remote.get(K) == two


# ---------------------------------------------------------------- S3
class FakeS3:
    """Dict-backed stand-in for the boto3 client; honours If-Match / If-None-Match like S3 does."""
    import botocore.exceptions as exceptions

    def __init__(self, implements=True):
        self.objects: dict[str, bytes] = {}
        self.implements = implements

    @staticmethod
    def _etag(data):
        return '"' + hashlib.md5(data).hexdigest() + '"'

    def _err(self, code, op):
        return self.exceptions.ClientError({"Error": {"Code": code, "Message": code}}, op)

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None):
        if (IfMatch or IfNoneMatch) and not self.implements:
            raise self._err("NotImplemented", "PutObject")
        if IfNoneMatch == "*" and Key in self.objects:
            raise self._err("PreconditionFailed", "PutObject")
        if IfMatch is not None and (Key not in self.objects or self._etag(self.objects[Key]) != IfMatch):
            raise self._err("PreconditionFailed", "PutObject")
        self.objects[Key] = Body
        return {"ETag": self._etag(Body)}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._err("404", "HeadObject")
        return {"ETag": self._etag(self.objects[Key])}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._err("NoSuchKey", "GetObject")
        return {"Body": type("B", (), {"read": lambda self_, d=self.objects[Key]: d})()}


def s3_store(fake):
    s = st.S3Storage.__new__(st.S3Storage)
    s.bucket, s.prefix, s.s3, s._own_client = "b", "p", fake, False
    return s


def test_s3_put_if_uses_the_etag(monkeypatch):
    monkeypatch.setattr(st.S3Storage, "_conditional", True)
    fake = FakeS3()
    s = s3_store(fake)
    assert s.version_of(K) is None
    s.put_if(K, b"one", None)
    v = s.version_of(K)
    assert v == fake._etag(b"one") and fake.objects == {"p/" + K: b"one"}
    s.put_if(K, b"two", v)
    with pytest.raises(Conflict):
        s.put_if(K, b"three", v)
    with pytest.raises(Conflict):
        s.put_if(K, b"three", None)
    assert s.get(K) == b"two" and st.S3Storage._conditional


def test_s3_endpoint_without_conditional_puts_falls_back_once(monkeypatch, capsys):
    monkeypatch.setattr(st.S3Storage, "_conditional", True)
    fake = FakeS3(implements=False)
    s = s3_store(fake)
    s.put_if(K, b"one", None)  # NotImplemented -> read-compare-write, remembered for the process
    assert fake.objects["p/" + K] == b"one" and not st.S3Storage._conditional
    assert capsys.readouterr().err.count("does not implement conditional puts") == 1
    v = s.version_of(K)
    s.put_if(K, b"two", v)
    with pytest.raises(Conflict):  # still refuses a stale version, by comparing before the put
        s.put_if(K, b"three", v)
    assert s.get(K) == b"two" and capsys.readouterr().err == ""  # said once


def test_s3_other_errors_are_not_conflicts(monkeypatch):
    monkeypatch.setattr(st.S3Storage, "_conditional", True)
    fake = FakeS3()

    def denied(**kw):
        raise fake._err("AccessDenied", "PutObject")

    fake.put_object = denied
    with pytest.raises(FakeS3.exceptions.ClientError):
        s3_store(fake).put_if(K, b"x", None)
    assert st.S3Storage._conditional  # not mistaken for an endpoint without the feature


# ---------------------------------------------------------------- Azure
def test_azure_put_if_uses_the_etag(azure_core):
    c = FakeContainer()
    s = st.AzureBlobStorage("snaps", "/lab/", client=c)
    assert s.version_of(K) is None
    s.put_if(K, b"one", None)
    v = s.version_of(K)
    assert v and c.blobs == {"lab/" + K: b"one"}
    s.put_if(K, b"two", v)
    with pytest.raises(Conflict):
        s.put_if(K, b"three", v)
    with pytest.raises(Conflict):
        s.put_if(K, b"three", None)
    with pytest.raises(Conflict):  # a version for a blob that is not there
        s.put_if("postgres/demo/other.json", b"x", v)
    assert s.get(K) == b"two"


def test_azure_create_makes_the_container(azure_core):
    c = FakeContainer(exists=False)
    s = st.AzureBlobStorage("snaps", "/lab/", client=c)
    s.put_if(K, b"one", None)
    assert c.created and c.blobs == {"lab/" + K: b"one"}


# ---------------------------------------------------------------- Postgres, live
def test_postgres_put_if_live(pg_url):
    s = st.PostgresStorage(pg_url)
    assert s.version_of(K) is None
    s.put_if(K, b'{"generation": 1}', None)
    v = s.version_of(K)
    assert v == "1" and s.get_json(K) == {"generation": 1}
    s.put_if(K, b'{"generation": 2}', v)
    assert s.version_of(K) == "2"
    with pytest.raises(Conflict):
        s.put_if(K, b'{"generation": 9}', v)
    with pytest.raises(Conflict):
        s.put_if(K, b'{"generation": 9}', None)
    s.put(K, b'{"generation": 3}')  # a plain put moves the version too
    assert s.version_of(K) == "3" and s.get_json(K) == {"generation": 3}
    s.put_if("oracle/ora/2026-11-05/T1/awr.txt", b"WORKLOAD\x00", None)  # bytea rows the same way
    assert s.get("oracle/ora/2026-11-05/T1/awr.txt") == b"WORKLOAD\x00"
    # a table created before the version column existed gets it on connect
    s.client.run(f"ALTER TABLE {s.TABLE} DROP COLUMN version")
    assert st.PostgresStorage(pg_url).version_of(K) == "1"


def test_postgres_ddl_adds_the_version_column_only_when_missing():
    """ALTER needs the owner even with IF NOT EXISTS, so the check comes first and a read-write role keeps working."""
    from dbtriage.storage import PostgresStorage

    ddl = " ".join(c for c in PostgresStorage.__init__.__code__.co_consts if isinstance(c, str))
    assert "information_schema.columns" in ddl and "ADD COLUMN IF NOT EXISTS" not in ddl
