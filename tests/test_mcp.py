"""The MCP server: off unless targets.toml turns it on, read-only tools that reuse the reviewer's, run_review only
when allowed, and a real stdio round trip with an MCP client."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from dbtriage import mcp_server  # noqa: E402
from dbtriage.config import load_config  # noqa: E402
from dbtriage.storage import LocalStorage  # noqa: E402

DEMO = "postgres/demo/2026-11-05/20261105T100000Z"
DEMO_BASE = "postgres/demo/2026-11-05/20261105T090000Z"


def conf(tmp_path, mcp: str = "") -> Path:
    from tests.conftest import seed_store

    seed_store(tmp_path / "data")
    p = tmp_path / "targets.toml"
    p.write_text('[storage]\nurl = "file://./data"\n'
                 '[targets.demo]\nengine = "postgres"\ndsn = "postgresql://u:p@127.0.0.1:1/x"\nnotes = "batch at 02:00"\n'
                 '[targets.quiet]\nengine = "postgres"\ndsn = "postgresql://u:p@127.0.0.1:1/y"\n' + mcp)
    return p


@pytest.fixture
def cfg_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")

    def make(mcp=""):
        return load_config(conf(tmp_path, mcp)), LocalStorage(tmp_path / "data")

    return make


def run_async(coro):
    """asyncio.run on a thread of its own: a test before this one (Playwright) may leave a loop running here."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def call(server, name, args=None):
    from mcp import Client

    async def go():
        async with Client(server) as c:
            if name is None:
                return [t.name for t in (await c.list_tools()).tools]
            r = await c.call_tool(name, args or {})
            return "".join(getattr(b, "text", "") for b in r.content)

    return run_async(go())


def test_off_unless_targets_toml_turns_it_on(cfg_store):
    cfg, st = cfg_store()
    with pytest.raises(SystemExit, match=r"set enabled = true under \[mcp\]"):
        mcp_server.build(cfg, st)
    cfg, st = cfg_store('[mcp]\nenabled = "yes"\n')  # a real boolean only
    with pytest.raises(SystemExit, match="off"):
        mcp_server.build(cfg, st)
    cfg, st = cfg_store('[mcp]\nenabled = true\ntools = ["list_targets", "drop_tables"]\nverbose = 1\n')
    with pytest.raises(SystemExit) as exc:
        mcp_server.build(cfg, st)
    assert "unknown drop_tables" in str(exc.value) and "verbose: unknown setting" in str(exc.value)


def test_tools_offered(cfg_store):
    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    assert call(mcp_server.build(cfg, st), None) == list(mcp_server.READ_TOOLS)  # read-only by default
    cfg, st = cfg_store("[mcp]\nenabled = true\nallow_run = true\n")
    assert call(mcp_server.build(cfg, st), None)[-1] == "run_review"
    cfg, st = cfg_store('[mcp]\nenabled = true\nallow_run = true\ntools = ["list_targets", "get_review"]\n')
    assert call(mcp_server.build(cfg, st), None) == ["list_targets", "get_review"]  # listed: run_review not among them
    cfg, st = cfg_store('[mcp]\nenabled = true\ntools = ["get_review", "run_review"]\n')
    assert call(mcp_server.build(cfg, st), None) == ["get_review"]  # run_review needs allow_run too


def test_read_tools_answer_from_the_store(cfg_store):
    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    server = mcp_server.build(cfg, st)
    targets = json.loads(call(server, "list_targets"))
    demo = next(t for t in targets if t["target"] == "demo")
    assert demo["latest"] == DEMO and demo["snapshots"] == 2 and demo["notes"] == "batch at 02:00"
    assert "dsn" not in json.dumps(targets) and "127.0.0.1" not in json.dumps(targets)  # never a connection string
    snaps = json.loads(call(server, "list_snapshots", {"target": "demo", "limit": 5}))
    assert [s["snapshot_id"] for s in snaps] == [DEMO, DEMO_BASE] and snaps[0]["reason"] == "latency alert"
    review = json.loads(call(server, "get_review", {"snapshot_id": DEMO}))
    assert review["target"] == "demo" and review["severity"] and "findings" in review
    assert json.loads(call(server, "get_report", {"snapshot_id": DEMO, "section": "load_profile"}))["xact_commit"] == 5000
    assert "sql" in call(server, "compare_snapshots", {"snapshot_id": DEMO, "baseline_id": DEMO_BASE})
    from tests.conftest import LONG_SQL

    history = json.loads(call(server, "get_sql_history", {"target": "demo", "sql_id": LONG_SQL}))
    assert sorted(h["calls"] for h in history) == [100, 2628]  # the statement in both snapshots
    # mistakes come back as text the model can act on
    assert call(server, "list_snapshots", {"target": "nope"}).startswith("error: no target 'nope'")
    assert call(server, "get_review", {"snapshot_id": "../../etc"}).startswith("error: no snapshot")


def test_run_review_runs_the_pipeline_only_when_allowed(cfg_store):
    cfg, st = cfg_store("[mcp]\nenabled = true\nallow_run = true\n")
    seen = []

    def fake_run(req, cfg, use_llm):
        seen.append((req.target, req.mode, req.source, use_llm))
        return {"snapshot": DEMO, "severity": "low", "summary": "fine", "source": "rules", "delivered": ["stdout"], "findings": []}

    server = mcp_server.build(cfg, st, run=fake_run)
    out = json.loads(call(server, "run_review", {"target": "demo", "mode": "incident", "use_model": False}))
    assert out["snapshot"] == DEMO and seen == [("demo", "incident", "mcp", False)]
    assert call(server, "run_review", {"target": "demo", "mode": "sometimes"}).startswith("error: mode")


def test_settings_page_and_api_show_it_but_cannot_change_it(tmp_path):
    from tests.test_overlay_semantics import app_for

    p = conf(tmp_path)
    app = app_for(p)
    assert "The MCP server is off" in app.settings_page().decode() and app.api_settings()["mcp"]["enabled"] is False
    p.write_text(p.read_text() + "[mcp]\nenabled = true\n")
    app = app_for(p)
    assert "The MCP server is on" in app.settings_page().decode() and "all read-only" in app.settings_page().decode()
    assert app.api_settings()["mcp"]["offered"] == list(mcp_server.READ_TOOLS)


def test_the_settings_api_refuses_mcp(web_server):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{web_server}/api/settings", data=json.dumps({"mcp": {"enabled": True}}).encode(),
                                 method="PUT", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400 and "only be set in targets.toml" in exc.value.read().decode()


def test_stdio_round_trip_with_a_real_client(tmp_path):
    """dbtriage mcp as a client launches it: a subprocess speaking MCP over stdin/stdout."""
    from mcp import Client, StdioServerParameters

    p = conf(tmp_path, "[mcp]\nenabled = true\n")
    params = StdioServerParameters(command=sys.executable, args=["-m", "dbtriage.cli", "--config", str(p), "mcp"],
                                   env={"DBTRIAGE_NO_OVERLAY": "1", "PATH": os.environ.get("PATH", "")}, cwd=str(tmp_path))

    async def go():
        async with Client(params) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            r = await c.call_tool("get_review", {"snapshot_id": DEMO})
            return names, "".join(getattr(b, "text", "") for b in r.content)

    names, text = run_async(go())
    assert names == list(mcp_server.READ_TOOLS) and json.loads(text)["target"] == "demo"


def test_stdio_refuses_to_start_when_off(tmp_path):
    import subprocess

    p = conf(tmp_path)
    r = subprocess.run([sys.executable, "-m", "dbtriage.cli", "--config", str(p), "mcp"], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "DBTRIAGE_NO_OVERLAY": "1"}, stdin=subprocess.DEVNULL)
    assert r.returncode == 2 and "set enabled = true under [mcp]" in r.stderr


def test_a_failing_run_or_a_system_exit_does_not_end_the_server(cfg_store):
    """run_review on a target with no connection string raised SystemExit inside the stdio server and ended it
    (agent review on #9, 3595164): the call reports it and the server keeps answering."""
    cfg, st = cfg_store("[mcp]\nenabled = true\nallow_run = true\n")

    def no_dsn(req, cfg, use_llm):
        raise SystemExit("target 'demo': no connection string: environment variable DEMO_DSN is not set")

    server = mcp_server.build(cfg, st, run=no_dsn)
    from mcp import Client

    async def go():
        async with Client(server) as c:
            r = await c.call_tool("run_review", {"target": "demo"})
            after = await c.call_tool("list_targets", {})
            return "".join(b.text for b in r.content), "".join(b.text for b in after.content)

    failed, after = run_async(go())
    assert failed.startswith("error: run failed: SystemExit: target 'demo': no connection string") and "demo" in after


def test_s3_missing_keys_are_file_not_found(monkeypatch):
    """S3Storage.get let botocore's ClientError through for a missing key, unlike the other stores, so 'not found'
    handling missed it (agent review on #9, 3595164)."""
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
        st.get("postgres/a/2026-11-06/T1/triage.json")
    with pytest.raises(botocore.exceptions.ClientError):  # anything else is still an error, not 'missing'
        st.get("postgres/a/denied")


def test_an_outage_is_reported_as_one_not_as_missing(cfg_store):
    """While the store is down, exists() answers False and listings show only the spool: the tools must say the store
    is unreachable, not that the snapshot or database has none (agent review on #9, 09585da)."""
    from dbtriage.storage import StorageUnavailable

    cfg, st = cfg_store("[mcp]\nenabled = true\n")

    class Down:
        degraded = "postgres store unreachable"

        def get_json(self, key):
            raise StorageUnavailable(self.degraded)

        def list_snapshots(self, engine, target):
            return []

    tools = mcp_server._guard
    t = mcp_server.Tools(cfg, Down())
    for fn, args in ((t.get_review, (DEMO,)), (t.list_targets, ()), (t.list_snapshots, ("demo",)), (t.get_report, (DEMO,))):
        assert tools(fn)(*args).startswith("error: the snapshot store is unreachable"), fn.__name__
    assert tools(mcp_server.Tools(cfg, st).get_review)("postgres/demo/2026-11-05/nope").startswith("error: no snapshot")


RUN_THAT_PRINTS = """
import sys
from dbtriage import mcp_server
from dbtriage.config import load_config
from dbtriage.storage import LocalStorage

def run(req, cfg, use_llm):
    print("# DB review\\n\\n**1 finding**: printed by the stdout channel, as deliver() does")
    return {"snapshot": "postgres/demo/2026-11-05/20261105T100000Z", "severity": "low", "summary": "ok", "findings": []}

cfg = load_config(sys.argv[1])
mcp_server.build(cfg, LocalStorage(sys.argv[2]), run=run).run("stdio")
"""


def test_a_run_that_prints_does_not_break_the_stdio_session(tmp_path):
    """run_review delivers to the stdout channel by default, which prints the review: on the MCP wire that would
    corrupt the session (agent review on #9, 09585da). The client must get the run's result and keep working."""
    from mcp import Client, StdioServerParameters

    p = conf(tmp_path, "[mcp]\nenabled = true\nallow_run = true\n")
    script = tmp_path / "serve.py"
    script.write_text(RUN_THAT_PRINTS)
    params = StdioServerParameters(command=sys.executable, args=[str(script), str(p), str(tmp_path / "data")],
                                   env={"DBTRIAGE_NO_OVERLAY": "1", "PATH": os.environ.get("PATH", ""),
                                        "PYTHONPATH": os.pathsep.join(sys.path)}, cwd=str(tmp_path))

    async def go():
        async with Client(params) as c:
            ran = await c.call_tool("run_review", {"target": "demo"})
            after = await c.call_tool("list_targets", {})
            return "".join(b.text for b in ran.content), "".join(b.text for b in after.content)

    ran, after = run_async(go())
    assert json.loads(ran)["snapshot"] == DEMO and any(t["target"] == "demo" for t in json.loads(after))


def test_any_tool_failure_comes_back_as_text(cfg_store, tmp_path):
    """A missing changes_file, or the store going away mid-tool, failed the call instead of answering
    (agent review on #9, af3f85c)."""
    from dbtriage.storage import StorageUnavailable

    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    cfg.targets["demo"].changes_file = str(tmp_path / "no-such-changes.json")
    st.put_json(f"{DEMO}/manifest.json", {**st.get_json(f"{DEMO}/manifest.json"), "changes_file": str(tmp_path / "no-such-changes.json")})
    out = call(mcp_server.build(cfg, st), "get_recent_changes", {"snapshot_id": DEMO})
    assert out.startswith("error: FileNotFoundError:") and "no-such-changes.json" in out, out  # text, not a failed call

    def boom():
        raise StorageUnavailable("primary down")

    assert mcp_server._guard(boom)().startswith("error: the snapshot store is unreachable (primary down)")

    def broken():
        raise ValueError("Expecting value: line 1 column 1")

    assert mcp_server._guard(broken)() == "error: ValueError: Expecting value: line 1 column 1"


def test_the_instructions_say_what_reaches_a_database(cfg_store):
    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    assert "None of these tools touches a live database." in mcp_server.build(cfg, st).instructions
    cfg, st = cfg_store("[mcp]\nenabled = true\nallow_run = true\n")
    text = mcp_server.build(cfg, st).instructions
    assert "None of these tools" not in text and "run_review is the one tool that connects to a database" in text


def test_snapshot_ids_must_look_like_snapshot_ids(cfg_store, tmp_path):
    """An id from the client's model went straight into storage keys; on a file:// store '../' reached outside the
    store (agent review on #9, 95f9622). The storage fix (#7) refuses such keys too; this does not depend on it."""
    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "manifest.json").write_text('{"target": "leaked", "engine": "postgres"}')
    server = mcp_server.build(cfg, st)
    for bad in ("../outside", "postgres/demo/../../outside", "postgres/demo/2026-11-05/20261105T100000Z/..", "x"):
        out = call(server, "get_review", {"snapshot_id": bad})
        assert out.startswith("error: no snapshot") and "leaked" not in out, (bad, out)
    assert json.loads(call(server, "get_review", {"snapshot_id": DEMO}))["target"] == "demo"
    assert mcp_server.SNAPSHOT_ID.fullmatch("oracle/ora-lab/2026-11-06/20261106T165508Z-2")


def test_corrupt_stored_json_is_an_error_not_missing(cfg_store):
    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    st.put(f"{DEMO}/triage.json", b"{not json")
    out = mcp_server._guard(mcp_server.Tools(cfg, st).get_review)(DEMO)
    assert out.startswith(f"error: the stored {DEMO}/triage.json is not valid JSON"), out


def test_sql_history_reports_an_outage(cfg_store):
    cfg, st = cfg_store("[mcp]\nenabled = true\n")

    class Down:
        degraded = "postgres store unreachable"

        def list_snapshots(self, engine, target):
            return []

        def get_json(self, key):
            raise FileNotFoundError(key)

    out = mcp_server._guard(mcp_server.Tools(cfg, Down()).get_sql_history)("demo", "abc")
    assert out.startswith("error: the snapshot store is unreachable"), out


def test_raw_reports_and_sql_history_limits(cfg_store):
    """get_raw_report reported an outage as a missing report; get_sql_history's limit=0 listed every snapshot
    (agent review on #9, 6add316)."""
    cfg, st = cfg_store("[mcp]\nenabled = true\n")

    class Degraded:
        degraded = "postgres store unreachable"

        def __init__(self, st):
            self.st = st

        def get_json(self, key):
            return self.st.get_json(key)  # the manifest came from the spool

        def list_snapshots(self, engine, target):
            return self.st.list_snapshots(engine, target)

    out = mcp_server._guard(mcp_server.Tools(cfg, Degraded(st)).get_raw_report)(DEMO, "awr.txt")
    assert out.startswith("error: the snapshot store is unreachable"), out
    from tests.conftest import LONG_SQL

    tools = mcp_server.Tools(cfg, st)
    assert len(json.loads(tools.get_sql_history("demo", LONG_SQL, limit=0))) == 1  # clamped to at least 1, not 'all'


def test_the_settings_page_says_when_mcp_will_not_start(tmp_path):
    from tests.test_overlay_semantics import app_for

    p = conf(tmp_path, '[mcp]\nenabled = true\ntools = "list_targets"\n')
    app = app_for(p)
    page = app.settings_page().decode()
    assert "turned on in targets.toml but will not start" in page and "tools: a list of tool names" in page
    assert app.api_settings()["mcp"]["offered"] == [] and app.api_settings()["mcp"]["problems"]


def test_off_is_reported_without_the_sdk(tmp_path, monkeypatch):
    """With [mcp] off, dbtriage mcp says it is off even where the optional SDK is not installed."""
    import builtins

    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    cfg = load_config(conf(tmp_path))
    real = builtins.__import__

    def no_sdk(name, *a, **k):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named 'mcp'")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_sdk)
    with pytest.raises(SystemExit, match="dbtriage mcp is off"):
        mcp_server.build(cfg, LocalStorage(tmp_path / "data"))


def test_operator_answers_tool(cfg_store):
    """The reviewer's get_operator_answers is offered read-only like its other tools, and the instructions name it."""
    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    out = call(mcp_server.build(cfg, st), "get_operator_answers", {"snapshot_id": DEMO})
    assert out == f"no results supplied by a person for {DEMO} or the previous snapshot {DEMO_BASE}"
    assert "get_operator_answers" in mcp_server.instructions(cfg) and "get_operator_answers" in mcp_server.READ_TOOLS


def test_instructions_name_only_the_offered_tools(cfg_store):
    """With tools = ["list_targets", "get_review"] the model was told to use list_snapshots, get_report ... which the
    server does not offer (agent review on #9, 6bb5926)."""
    cfg, st = cfg_store('[mcp]\nenabled = true\ntools = ["list_targets", "get_review"]\n')
    text = mcp_server.instructions(cfg)
    assert "Start with list_targets, get_review." in text and "Dig in" not in text
    for absent in ("list_snapshots", "get_report", "compare_snapshots", "get_sql_history"):
        assert absent not in text
    cfg, st = cfg_store("[mcp]\nenabled = true\nallow_run = true\n")
    text = mcp_server.instructions(cfg)
    assert "read-only unless that database is opted into taking snapshots" in text
    assert "opted into taking snapshots" in mcp_server.DESCRIPTIONS["run_review"]


@pytest.mark.parametrize("section", ["mcp = true", "collect = 1", 'agent = "x"'])
def test_a_section_that_is_not_a_table_is_a_config_error(tmp_path, monkeypatch, section):
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    p = conf(tmp_path)
    p.write_text(section + "\n" + p.read_text())
    with pytest.raises(SystemExit, match="must be a table"):
        load_config(p)


def test_compare_reports_an_outage_and_sql_history_names_its_args(cfg_store):
    """compare_snapshots skipped the outage check; get_sql_history's description left out its required target
    (agent review on #9, d933d9e)."""
    cfg, st = cfg_store("[mcp]\nenabled = true\n")

    class Degraded:
        degraded = "postgres store unreachable"

        def get_json(self, key):
            return st.get_json(key)

        def list_snapshots(self, engine, target):
            return st.list_snapshots(engine, target)

    out = mcp_server._guard(mcp_server.Tools(cfg, Degraded()).compare_snapshots)(DEMO, DEMO_BASE)
    assert out.startswith("error: the snapshot store is unreachable"), out
    from mcp import Client

    async def go():
        async with Client(mcp_server.build(cfg, st)) as c:
            return {t.name: t for t in (await c.list_tools()).tools}["get_sql_history"]

    tool = run_async(go())
    assert "target (a name from list_targets)" in tool.description and "target" in tool.input_schema["required"]


def test_compare_honours_the_targets_band_weeks(cfg_store, monkeypatch):
    """The server builds the reviewer's tools without run(), which left band_weeks at the default: a target with 0
    still got band sentences and one with 12 got 6. compare_snapshots reads the target's own setting."""
    from dbtriage import band

    cfg, st = cfg_store("[mcp]\nenabled = true\n")
    asked = []

    def usual_band(store, engine, target, sid, weeks=6, hours_tolerance=1, baseline=None):
        asked.append((target, weeks, baseline))
        return None

    monkeypatch.setattr(band, "usual_band", usual_band)
    tools = mcp_server.Tools(cfg, st)
    cfg.targets["demo"].extra["band_weeks"] = 12
    assert "sql" in tools.compare_snapshots(DEMO, DEMO_BASE) and asked == [("demo", 12, DEMO_BASE)]
    cfg.targets["demo"].extra["band_weeks"] = 0
    assert "sql" in tools.compare_snapshots(DEMO, DEMO_BASE) and asked == [("demo", 12, DEMO_BASE)]  # off: not asked


def test_snapshot_ids_follow_configs_engines_and_name_rule():
    """SNAPSHOT_ID hard-coded three engines and its own name grammar (agent review on #9, 5752e9d)."""
    from dbtriage.config import ENGINES
    from dbtriage.mcp_server import SNAPSHOT_ID

    for engine in ENGINES:
        assert SNAPSHOT_ID.fullmatch(f"{engine}/ora.lab_2/2026-11-06/20261106T201500Z-2")
    for bad in ("db2/x/2026-11-06/20261106T201500Z", "postgres/../2026-11-06/20261106T201500Z",
                "postgres/demo./2026-11-06/20261106T201500Z", f"postgres/{'x' * 65}/2026-11-06/20261106T201500Z"):
        assert not SNAPSHOT_ID.fullmatch(bad), bad
