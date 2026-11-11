import builtins
import hashlib
import sys

import pytest

from dbtriage import storage as st


class _NotFound(Exception):
    pass


class _Modified(Exception):
    pass


class _Exists(Exception):
    pass


class _Match:
    """Stand-in for azure.core.MatchConditions."""
    IfNotModified, IfMissing = "IfNotModified", "IfMissing"


def _etag(data: bytes) -> str:
    return '"' + hashlib.md5(data).hexdigest() + '"'


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def exists(self):
        return self.name in self.store

    def get_blob_properties(self):
        if self.name not in self.store:
            raise _NotFound()
        return type("P", (), {"etag": _etag(self.store[self.name])})()


class FakeDownload:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


class FakeContainer:
    """Dict-backed stand-in for azure.storage.blob.ContainerClient."""

    def __init__(self, exists=True):
        self.blobs: dict[str, bytes] = {}
        self.created = not exists
        self._exists = exists

    def upload_blob(self, name, data, overwrite=False, etag=None, match_condition=None):
        if not self._exists:
            raise _NotFound()
        if match_condition == _Match.IfMissing and name in self.blobs:
            raise _Exists()
        if match_condition == _Match.IfNotModified and (name not in self.blobs or _etag(self.blobs[name]) != etag):
            raise _Modified()
        self.blobs[name] = data

    def create_container(self):
        self._exists, self.created = True, True

    def download_blob(self, name):
        if name not in self.blobs:
            raise _NotFound()
        return FakeDownload(self.blobs[name])

    def get_blob_client(self, name):
        return FakeBlob(self.blobs, name)

    def list_blobs(self, name_starts_with=""):
        if not self._exists:
            raise _NotFound()
        return [type("B", (), {"name": n})() for n in self.blobs if n.startswith(name_starts_with)]


@pytest.fixture
def azure_core(monkeypatch):
    """Give storage.py an azure.core.exceptions.ResourceNotFoundError without installing the SDK."""
    core = type(sys)("azure.core"); exc = type(sys)("azure.core.exceptions")
    exc.ResourceNotFoundError, exc.ResourceModifiedError, exc.ResourceExistsError = _NotFound, _Modified, _Exists
    _NotFound.__name__ = "ResourceNotFoundError"
    core.MatchConditions = _Match
    monkeypatch.setitem(sys.modules, "azure", type(sys)("azure"))
    monkeypatch.setitem(sys.modules, "azure.core", core)
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", exc)


def test_put_get_exists_list_with_prefix(azure_core):
    c = FakeContainer()
    s = st.AzureBlobStorage("snaps", "/lab/", client=c)
    sid = "postgres/demo/2026-11-05/20261105T100000Z"
    s.put_json(f"{sid}/manifest.json", {"id": sid})
    s.put_text(f"{sid}/awr.txt", "hello")
    assert c.blobs[f"lab/{sid}/manifest.json"].startswith(b"{")           # prefix joined with "/"
    assert s.exists(f"{sid}/awr.txt") and not s.exists(f"{sid}/nope")
    assert s.get(f"{sid}/awr.txt") == b"hello" and s.get_json(f"{sid}/manifest.json") == {"id": sid}
    assert s.list_keys("postgres/demo/") == [f"{sid}/awr.txt", f"{sid}/manifest.json"]  # relative to prefix, sorted
    assert s.list_snapshots("postgres", "demo") == [sid] and s.latest("postgres", "demo") == sid
    with pytest.raises(FileNotFoundError):
        s.get("missing")


def test_no_prefix_and_container_created_on_first_put(azure_core):
    c = FakeContainer(exists=False)
    s = st.AzureBlobStorage("snaps", "", client=c)
    assert s.list_keys("") == []                      # no container yet: nothing stored, no error
    s.put("a/b.txt", b"x")
    assert c.created and c.blobs == {"a/b.txt": b"x"} and s.list_keys("a/") == ["a/b.txt"]


def test_open_storage_parses_az_urls(monkeypatch):
    made = []
    monkeypatch.setattr(st, "AzureBlobStorage", lambda container, prefix="": made.append((container, prefix)))
    st.open_storage("az://dbtriage/lab/snaps")
    st.open_storage("azure://other")
    assert made == [("dbtriage", "/lab/snaps"), ("other", "")]
    with pytest.raises(SystemExit):
        st.open_storage("gs://nope")


def test_missing_sdk_says_install_extra(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("azure"):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SystemExit, match=r"dbtriage\[azure\]"):
        st.AzureBlobStorage("c")


def test_auth_order(monkeypatch):
    calls = []
    blob = type(sys)("azure.storage.blob")

    class BSC:
        def __init__(self, account_url=None, credential=None, **kw):
            assert kw == {"connection_timeout": 5, "read_timeout": 15, "retry_total": 2}
            calls.append(("url", account_url, type(credential).__name__ if not isinstance(credential, str) else "sas"))

        @classmethod
        def from_connection_string(cls, conn, **kw):
            assert kw["connection_timeout"] == 5
            calls.append(("conn", "set" if conn else ""))
            return cls.__new__(cls)

        def get_container_client(self, name):
            return FakeContainer()

    blob.BlobServiceClient = BSC
    ident = type(sys)("azure.identity")
    ident.DefaultAzureCredential = type("DefaultAzureCredential", (), {})
    for mod, m in (("azure", type(sys)("azure")), ("azure.storage", type(sys)("azure.storage")), ("azure.storage.blob", blob), ("azure.identity", ident)):
        monkeypatch.setitem(sys.modules, mod, m)
    for v in ("AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_SAS_TOKEN", "AZURE_STORAGE_ENDPOINT"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SystemExit, match="AZURE_STORAGE_CONNECTION_STRING"):
        st.AzureBlobStorage("c")
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "acct")
    st.AzureBlobStorage("c")
    monkeypatch.setenv("AZURE_STORAGE_SAS_TOKEN", "sv=...")
    monkeypatch.setenv("AZURE_STORAGE_ENDPOINT", "http://127.0.0.1:10000/devstoreaccount1")
    st.AzureBlobStorage("c")
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "UseDevelopmentStorage=true")
    st.AzureBlobStorage("c")
    assert calls == [("url", "https://acct.blob.core.windows.net", "DefaultAzureCredential"),
                     ("url", "http://127.0.0.1:10000/devstoreaccount1", "sas"), ("conn", "set")]
