"""Storage keys never leave the store: a snapshot id or file name carrying '..' or an absolute path must not reach
files outside it, through the store directly or through the web API."""
import urllib.parse

import pytest

from dbtriage.storage import LocalStorage

BAD = ["../outside/summary.json", "postgres/a/../../../outside/summary.json", "/etc/hostname", "a/./b", "a//b",
       "a\\..\\b", "a\x00b", "", "C:/Windows/win.ini", "c:", "postgres/a/summary.json:stream", "\\\\server\\share\\x"]


def test_local_storage_refuses_keys_that_leave_the_store(tmp_path):
    root, outside = tmp_path / "store", tmp_path / "outside"
    outside.mkdir()
    (outside / "summary.json").write_text('{"secret": 1}')
    st = LocalStorage(root)
    st.put_json("postgres/a/2026-11-06/T1/summary.json", {"ok": 1})
    assert st.get_json("postgres/a/2026-11-06/T1/summary.json") == {"ok": 1}  # ordinary keys still work
    for key in BAD:
        with pytest.raises(FileNotFoundError):
            st.get(key)
        assert not st.exists(key)
        with pytest.raises(ValueError):
            st.put(key, b"x")
    assert st.list_keys("../outside/") == [] and st.list_keys("postgres/") == ["postgres/a/2026-11-06/T1/summary.json"]
    assert not (tmp_path / "x").exists()  # nothing written outside


def test_web_api_cannot_read_outside_the_store(tmp_path):
    """GET /api/snapshots/<..>/files/summary.json read any summary.json the process could reach (found 2026-11-06)."""
    import threading

    from dbtriage.web import make_server
    from tests.conftest import free_port, wait_for
    from tests.test_web import _req, config, store

    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "summary.json").write_text('{"secret": "outside the store"}')
    st = store(tmp_path)
    srv = make_server(config(tmp_path), st, "127.0.0.1", free_port(), False, "s3cret", True, allow_chat=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    try:
        rel = urllib.parse.quote("../" * 6 + str(tmp_path / "outside").lstrip("/"), safe="")
        for path in (f"/api/snapshots/{rel}/files/summary.json", f"/api/snapshots/{rel}", f"/s/{rel}"):
            status, body = _req(url + path)
            assert status == 404 and b"outside the store" not in body, (path, status, body[:120])
        # a discussion thread is written at <snapshot id>/chat/<key>.json: none may land outside the store
        (tmp_path / "outside" / "manifest.json").write_text('{"engine": "postgres", "target": "x"}')
        status, body = _req(url + f"/api/snapshots/{rel}/chat/review", "POST", b'{"question": "hi"}',
                            {"Content-Type": "application/json"})
        assert status == 404 and not (tmp_path / "outside" / "chat").exists(), (status, body[:120])
    finally:
        srv.shutdown()
        srv.server_close()


def test_windows_absolute_keys_are_refused():
    """On Windows, root / "C:/x" is C:/x: drive letters (and ':' streams, UNC paths) must not pass (agent review on #7)."""
    from pathlib import PureWindowsPath

    from dbtriage.storage import key_ok

    for key in ("C:/Windows/win.ini", "D:relative", "a/b:c", "\\\\server\\share"):
        assert not key_ok(key)
        assert not key_ok(key, prefix=True)
    assert PureWindowsPath("store") / "C:/x" == PureWindowsPath("C:/x")  # why: the join discards the root
    assert key_ok("postgres/cnpg-node1/2026-11-06/20261106T165508Z/summary.json") and key_ok("oracle/", prefix=True)


@pytest.mark.parametrize("name", ["a:b", "a/b", "../x", "-lead", "with space", "x" * 65, "demo\\n", "demo\\t", "demo.", "demo ",
                                  "NUL", "con", "com1", "lpt9.prod"])
def test_targets_toml_names_follow_the_key_rules(tmp_path, monkeypatch, name):
    """A target name is part of every stored key: one the store refuses must be refused when targets.toml loads, with a
    message, not crash every capture later (agent review on #7, 70c058f)."""
    from dbtriage.config import load_config

    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    p = tmp_path / "targets.toml"
    p.write_text(f'[targets."{name}"]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    with pytest.raises(SystemExit, match="a name is letters, digits"):
        load_config(p)


def test_targets_toml_engine_is_checked_and_good_names_load(tmp_path, monkeypatch):
    from dbtriage.config import load_config

    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    p = tmp_path / "targets.toml"
    p.write_text('[targets.pg]\nengine = "postgresql"\ndsn = "postgresql://u@h/d"\n')
    with pytest.raises(SystemExit, match="engine must be one of postgres, mysql, oracle"):
        load_config(p)
    p.write_text('[targets."cnpg-node1"]\nengine = "postgres"\ndsn = "x"\n[targets."ora.lab_2"]\nengine = "oracle"\ndsn = "y"\n')
    assert sorted(load_config(p).targets) == ["cnpg-node1", "ora.lab_2"]


def test_a_discussion_failing_for_other_reasons_is_not_a_404(tmp_path, monkeypatch):
    """Only a missing snapshot is a 404; a missing api_key_cmd binary (FileNotFoundError too) stays a 502 with its
    cause (agent review on #7, 70c058f)."""
    from dbtriage import llm
    from dbtriage.web import App
    from tests.test_web import config, store

    st = store(tmp_path)
    app = App(config(tmp_path), st, False, None, False, True)
    sid = st.list_snapshots("postgres", "demo")[-1]

    def no_binary(cfg):
        raise FileNotFoundError(2, "No such file or directory", "gcloud")

    monkeypatch.setattr(llm, "make_backend", no_binary)
    code, body = app.chat_ask(sid, "review", "why?")
    assert code == 502 and "gcloud" in body["error"]
    assert app.chat_ask("postgres/demo/2026-11-05/nope", "review", "why?")[0] == 404
    assert app.chat_ask("../../etc", "review", "why?")[0] == 404


def test_control_characters_never_make_a_key(tmp_path):
    from dbtriage.storage import key_ok

    for key in ("demo\n/x", "a\tb", "a\x7fb", "postgres/demo\n"):
        assert not key_ok(key) and not key_ok(key, prefix=True)
    from dbtriage.config import TARGET_NAME

    assert TARGET_NAME.fullmatch("demo") and not TARGET_NAME.fullmatch("demo\n")  # '$' alone would match this


def test_a_discussion_during_a_store_outage_is_a_503_not_a_404(tmp_path):
    """ResilientStorage.exists answers False while the store is down: a discussion about an existing snapshot then
    said 'no such snapshot' (agent review on #7, 1b6d093)."""
    from dbtriage.storage import StorageUnavailable
    from dbtriage.web import App
    from tests.test_web import config, store

    st = store(tmp_path)
    sid = st.list_snapshots("postgres", "demo")[-1]
    app = App(config(tmp_path), st, False, None, False, True)

    def down(key):
        raise StorageUnavailable("postgres store unreachable")

    app.store.get = down
    code, body = app.chat_ask(sid, "review", "why?")
    assert code == 503 and "unavailable" in body["error"]


def test_s3_missing_keys_are_file_not_found():
    """S3Storage.get let botocore's ClientError through for a missing key, unlike the other stores, so a discussion
    about a missing snapshot on S3 escaped the 404 path and dropped the request (agent review on #7, d94fed2)."""
    import botocore.exceptions

    from dbtriage.storage import S3Storage

    class FakeS3:
        exceptions = botocore.exceptions

        def get_object(self, Bucket, Key):
            code = "AccessDenied" if Key.endswith("denied") else "NoSuchKey"
            raise botocore.exceptions.ClientError({"Error": {"Code": code, "Message": "x"}}, "GetObject")

    st = S3Storage.__new__(S3Storage)
    st.bucket, st.prefix, st.s3 = "b", "p", FakeS3()
    with pytest.raises(FileNotFoundError):
        st.get("postgres/a/2026-11-06/T1/manifest.json")
    with pytest.raises(botocore.exceptions.ClientError):  # anything else is still an error, not 'missing'
        st.get("postgres/a/denied")


def test_a_discussion_on_any_other_store_error_is_answered(tmp_path):
    from dbtriage.web import App
    from tests.test_web import config, store

    st = store(tmp_path)
    sid = st.list_snapshots("postgres", "demo")[-1]
    app = App(config(tmp_path), st, False, None, False, True)

    def denied(key):
        raise PermissionError("access denied by the bucket policy")

    app.store.get = denied
    code, body = app.chat_ask(sid, "review", "why?")
    assert code == 502 and "access denied" in body["error"]


def test_windows_trailing_dots_and_spaces_cannot_make_dotdot():
    """Windows strips trailing dots and spaces from a path segment, so '.. ' or '.. .' became '..' and left the store
    (agent review on #7, 773285f). Keys are an allowlist now."""
    from dbtriage.storage import key_ok

    for key in (".. /x", "a/.. /b", ".. ./x", "... /x", "a./b", "demo./x", "a b", "a\u00a0b", "ａ/b", "x/.", "x/.."):
        assert not key_ok(key), repr(key)
    for key in ("postgres/demo/2026-11-05/20261105T100000Z/summary.json", ".dbtriage-probe",
                "oracle/ora-lab/2026-11-06/20261106T165508Z-2/awr.txt", "postgres/demo/2026-11-05/x/chat/finding-1.json"):
        assert key_ok(key), key


def test_windows_device_names_are_never_keys():
    """On Windows NUL, CON, COM1 ... open the device whatever the extension (agent review on #7, 43b5f80)."""
    from dbtriage.storage import key_ok

    for key in ("postgres/nul/2026-11-05/x/summary.json", "postgres/demo/2026-11-05/x/aux.json", "CON", "a/Com3.txt", "lpt1"):
        assert not key_ok(key), key
    assert key_ok("postgres/console/2026-11-05/x/summary.json") and key_ok("postgres/demo/2026-11-05/x/com10.json")


def test_bad_names_in_the_settings_overlay_are_skipped_not_fatal(tmp_path, monkeypatch, capsys):
    """A target saved in the overlay under a name the store refuses (the old web rule allowed 'demo.') made every
    capture of it fail; it is skipped with one warning and the others keep working (agent review on #7, 43b5f80)."""
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {
        "demo.": {"engine": "postgres", "dsn": "postgresql://u@h/x"}, "nul": {"engine": "postgres", "dsn": "x"},
        "demo": {"engine": "postgresql"}, "ok-one": {"engine": "mysql", "dsn": "mysql://u@h/y"}}}))
    cfg = load_config(p)
    assert sorted(cfg.targets) == ["demo", "ok-one"] and cfg.targets["demo"].engine == "postgres"  # the file's, kept
    err = capsys.readouterr().err
    assert "'demo.'" in err and "'nul'" in err and "engine must be one of" in err
    load_config(p)
    assert capsys.readouterr().err == ""  # said once, not on every reload
    # and the Settings page shows what runs: the skipped entry is not 'ui', nor pre-filled (agent review on #7, 1d79ebc)
    st = settings.Settings.for_config(cfg.file_only or cfg)
    assert st.origin("demo", cfg.file_only) == "file" and st.form_of("demo", cfg.file_only)["engine"] == "postgres"
    assert st.origin("ok-one", cfg.file_only) == "ui"


def test_saves_replace_a_skipped_overlay_entry(tmp_path, monkeypatch):
    """Schedule and target saves patched an entry apply() skips, so the change never applied (agent review on #7,
    651600b): they replace it."""
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config
    from tests.test_overlay_semantics import app_for

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {"demo": {"engine": "postgresql", "schedule": "*/5 * * * *"}}}))
    app = app_for(p)
    assert app.save_schedule("demo", {"schedule": "0 * * * *"}) == []
    assert load_config(p).targets["demo"].extra.get("schedule") == "0 * * * *"
    assert app_for(p).save_target("demo", {"notes": "now applied"}) == []
    assert load_config(p).targets["demo"].extra.get("notes") == "now applied"


@pytest.mark.parametrize("entry", [{"top_n": "lots"}, {"dsn_env": 5}, {"links": "x"}, "not a table"])
def test_any_bad_overlay_field_skips_the_entry_not_the_config(tmp_path, monkeypatch, entry):
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {"demo": entry, "other": {"engine": "mysql", "dsn": "mysql://u@h/y"}}}))
    cfg = load_config(p)
    assert sorted(cfg.targets) == ["demo", "other"] and cfg.targets["demo"].top_n == 25  # the file's demo, untouched


def test_local_keys_are_slash_separated(tmp_path):
    """list_keys built keys with str(Path), which is backslash-separated on Windows (agent review on #7, 651600b)."""
    import inspect

    from dbtriage.storage import LocalStorage

    st = LocalStorage(tmp_path)
    st.put_json("postgres/demo/2026-11-06/T1/manifest.json", {})
    assert st.list_keys("postgres/") == ["postgres/demo/2026-11-06/T1/manifest.json"]
    assert "as_posix()" in inspect.getsource(LocalStorage.list_keys)  # the same on Windows, where str() uses '\\'


def test_a_save_refuses_what_apply_would_skip(tmp_path):
    """PUT /api/targets/<name> with a non-text notes returned 200, then apply() skipped the whole entry (agent review
    on #7, 39284b3): the save now refuses it."""
    from tests.test_overlay_semantics import app_for

    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    app = app_for(p)
    assert app.save_target("demo", {"notes": {"x": 1}}) == ["notes must be text"]
    assert app.save_target("demo", {"links": ["x"]}) == ["links must be a list of {label, url}, both text"]
    assert app.save_target("demo", {"top_n": "lots"}) == ["top_n must be a whole number"]
    assert app.save_target("demo", {"notes": "fine", "links": [{"label": "Grafana", "url": "https://g/x"}]}) == []


def test_link_entries_must_be_links_in_the_overlay(tmp_path, monkeypatch):
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {"demo": {"links": ["x"]}}}))
    assert not load_config(p).targets["demo"].extra.get("links")  # skipped, the file's demo kept


def test_listings_only_show_readable_keys(tmp_path):
    from dbtriage.storage import LocalStorage

    st = LocalStorage(tmp_path)
    st.put_json("postgres/demo/2026-11-06/T1/manifest.json", {})
    (tmp_path / "postgres" / "demo." / "x").mkdir(parents=True)
    (tmp_path / "postgres" / "demo." / "x" / "manifest.json").write_text("{}")  # left by an old name, say
    keys = st.list_keys("postgres/")
    assert keys == ["postgres/demo/2026-11-06/T1/manifest.json"] and all(st.exists(k) for k in keys)


def test_round_12_edges(tmp_path, monkeypatch, capsys):
    """agent review on #7, 9454ca5: a blank top_n, names that differ only in case, a link without a label, and
    copy-store into a local store."""
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config
    from tests.test_overlay_semantics import app_for

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {
        "demo": {"top_n": None}, "other": {"engine": "mysql", "dsn": "mysql://u@h/y", "top_n": ""},
        "Demo": {"engine": "postgres", "dsn": "postgresql://u@h/x"}}}))
    cfg = load_config(p)
    assert cfg.targets["demo"].top_n == 25 and cfg.targets["other"].top_n == 25  # blank top_n: the default
    assert "Demo" not in cfg.targets and "differs only in case" in capsys.readouterr().err
    assert app_for(p).save_target("DEMO", {"engine": "postgres", "dsn": "postgresql://u@h/z"})[0].startswith("name: 'demo' exists")
    assert settings._field_problems({"links": [{"url": "https://x"}]}) == ["links must be a list of {label, url}, both text"]
    assert settings._field_problems({"top_n": float("inf")}) == ["top_n must be a whole number"]
    p.write_text(p.read_text() + '[targets.Demo]\nengine = "postgres"\ndsn = "postgresql://u@h/x"\n')
    with pytest.raises(SystemExit, match="differ only in case"):
        load_config(p)


def test_copy_store_into_a_local_store_skips_keys_it_refuses(tmp_path, capsys, monkeypatch):
    from dbtriage import cli
    from dbtriage.storage import LocalStorage

    class Remote:
        def __init__(self):
            self.data = {"postgres/a/2026-11-06/T1/manifest.json": b"{}", "postgres/old name/x/manifest.json": b"{}"}

        def list_keys(self, prefix):
            return sorted(k for k in self.data if k.startswith(prefix))

        def get(self, k):
            return self.data[k]

    dst = LocalStorage(tmp_path / "dst")
    monkeypatch.setattr(cli, "open_storage", lambda url, *a: Remote() if url == "s3://src" else dst)
    (tmp_path / "targets.toml").write_text("")
    cli.main(["--config", str(tmp_path / "targets.toml"), "copy-store", "--from", "s3://src", "--to", f"file://{tmp_path}/dst"])
    out = capsys.readouterr()
    assert dst.list_keys("") == ["postgres/a/2026-11-06/T1/manifest.json"] and "skipped 'postgres/old name/x/manifest.json'" in out.err


def test_top_n_blank_means_25_and_zero_stays_zero(tmp_path, monkeypatch):
    """A blank overlay top_n crashed the edit page; 'or 25' turned an explicit 0 into 25 (agent review on #7, 4df965f)."""
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config, top_n_of
    from tests.test_overlay_semantics import app_for

    assert [top_n_of(v) for v in (None, "", 0, "7", 40)] == [25, 25, 0, 7, 40]
    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n[targets.zero]\nengine = "postgres"\ndsn = "x"\ntop_n = 0\n')
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {"demo": {"top_n": None}, "zero": {"notes": "edited"}}}))
    cfg = load_config(p)
    assert cfg.targets["demo"].top_n == 25 and cfg.targets["zero"].top_n == 0
    page = app_for(p).target_form("demo").decode()
    assert 'name="top_n" type="number" min="5" max="200" value="25"' in page


def test_a_non_text_field_is_refused_before_it_is_read(tmp_path):
    """{"dsn_env": 5} reached a regex before the type check and raised TypeError (agent review on #7, fa78c51)."""
    from tests.test_overlay_semantics import app_for

    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    app = app_for(p)
    assert app.save_target("demo", {"dsn_env": 5}) == ["dsn_env must be text"]
    assert app.save_target("new", {"engine": "postgres", "dsn_env": ["X"]}) == ["dsn_env must be text"]


def test_a_case_clash_the_overlay_skips_is_not_shown_as_live(tmp_path, monkeypatch):
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {"Demo": {"engine": "postgres", "dsn": "postgresql://u@h/x"}}}))
    cfg = load_config(p)
    st = settings.Settings.for_config(cfg.file_only)
    assert "Demo" not in cfg.targets and st.origin("Demo", cfg.file_only) == "file" and st.form_of("Demo", cfg.file_only) == {}


@pytest.mark.parametrize("first", [{"engine": "postgresql"}, {"deleted": True}])
def test_the_page_and_apply_agree_on_which_case_variant_runs(tmp_path, monkeypatch, first):
    """An earlier case-variant that apply() itself skipped or deleted made _entry hide the variant that does run
    (agent review on #7, ae8aa28): both now use one walk of the overlay."""
    import json as _json

    from dbtriage import settings
    from dbtriage.config import load_config

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text("")
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {
        "Demo": first, "demo": {"engine": "postgres", "dsn": "postgresql://u@h/x"}}}))
    cfg = load_config(p)
    st = settings.Settings.for_config(cfg.file_only)
    assert "demo" in cfg.targets and st.origin("demo", cfg.file_only) == "ui" and st.form_of("demo", cfg.file_only)["engine"] == "postgres"


def test_schedule_fields_from_the_api_are_typed_and_the_form_matches_the_rule(tmp_path):
    """{"schedule": 5} crashed in scheduler.normalize; the form's pattern allowed names the server refuses
    (agent review on #7, 5d3644c)."""
    import re as _re

    from dbtriage.config import TARGET_NAME
    from tests.test_overlay_semantics import app_for

    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    app = app_for(p)
    assert app.save_schedule("demo", {"schedule": 5}) == ["schedule must be text"]
    assert app.save_schedule("demo", {"schedule": "every 15m"}) == []
    page = app.target_form(None).decode()
    pattern = _re.search(r'name="name"[^>]*pattern="([^"]+)"', page)[1].replace("\\-", "-")
    for name in ("demo", "pg-lab", "ora.lab_2", "demo.", "-x", "x" * 65):
        assert bool(_re.fullmatch(pattern, name)) == bool(TARGET_NAME.fullmatch(name)), name


def test_infinity_and_null_links_are_handled(tmp_path, monkeypatch):
    """{"schedule_window": Infinity} raised OverflowError; links: null broke the edit page (agent review on #7, 7e9fcf7)."""
    import json as _json

    from dbtriage import scheduler, settings
    from tests.test_overlay_semantics import app_for

    monkeypatch.setattr(settings, "_WARNED", set())
    p = tmp_path / "targets.toml"
    p.write_text('[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u@h/d"\n')
    app = app_for(p)
    assert app.save_schedule("demo", {"schedule": "every 15m", "schedule_window": float("inf")}) == ["schedule_window must be a whole number"]
    assert settings._field_problems({"schedule_window": "lots"}) == ["schedule_window must be a whole number"]
    (tmp_path / "dbtriage-settings.json").write_text(_json.dumps({"targets": {"demo": {"links": None}}}))
    app = app_for(p)
    assert "Edit target demo" in app.target_form("demo").decode() and app.index()
    assert scheduler.schedule_of({"schedule": "every 15m", "schedule_window": "abc"})[1] == 60


def test_booleans_are_not_numbers_and_a_bad_toml_schedule_is_unscheduled():
    """{"schedule_window": true} was a 5-minute window and {"top_n": true} 1; schedule = 5 in targets.toml crashed
    schedule_of (agent review on #7, 103164f)."""
    from dbtriage import scheduler, settings

    assert settings._field_problems({"top_n": True}) == ["top_n must be a whole number"]
    assert settings._field_problems({"schedule_window": True}) == ["schedule_window must be a whole number"]
    assert settings._field_problems({"schedule_window": 2}) == ["schedule_window: 5 to 1440 minutes"]
    assert settings._field_problems({"top_n": "30", "schedule_window": 90}) == []
    for bad in (5, True, ["0 * * * *"]):
        assert scheduler.schedule_of({"schedule": bad}) == ("", 60, False)
