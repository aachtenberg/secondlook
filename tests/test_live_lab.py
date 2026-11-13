"""The real pipeline against the lab: collect -> store -> compare -> review, for every engine that is up.

Uses the lab targets from targets.toml and the DSNs from the environment or .env, writes into a throwaway store,
delivers to stdout only, and never applies the UI settings overlay. A database that is not reachable skips.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.live


@pytest.fixture
def lab_cfg(lab_env, tmp_path, monkeypatch):
    from dbtriage.config import load_config

    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    monkeypatch.delenv("DBTRIAGE_STORAGE", raising=False)
    try:
        cfg = load_config(lab_env)
    except SystemExit as exc:
        pytest.skip(f"lab config not loadable: {exc}")
    return replace(cfg, storage_url=f"file://{tmp_path / 'store'}", base_dir=str(tmp_path), delivery={"channels": ["stdout"]})


def reachable(target) -> tuple[bool, str]:
    from dbtriage.settings import probe

    return probe(target, timeout_s=3)


@pytest.mark.parametrize("name", ["pg-lab", "mysql-lab", "mariadb-lab", "ora-lab", "mssql-lab"])
def test_pipeline_end_to_end(lab_cfg, name, capsys):
    from dbtriage.cli import run_request
    from dbtriage.storage import open_storage
    from dbtriage.triggers import TriageRequest

    t = lab_cfg.targets.get(name)
    if t is None:
        pytest.skip(f"{name} is not in targets.toml")
    ok, why = reachable(t)
    if not ok:
        pytest.skip(f"{name} not reachable: {why}")
    first = run_request(TriageRequest(target=name, mode="scheduled", window_minutes=60, source="test", reason="live test 1"), lab_cfg, use_llm=False)
    second = run_request(TriageRequest(target=name, mode="incident", window_minutes=60, source="test", reason="live test 2"), lab_cfg, use_llm=False)
    store = open_storage(lab_cfg.storage_url)
    ids = store.list_snapshots(t.engine, name)
    assert len(ids) == 2 and first["snapshot"] == ids[0] and second["snapshot"] == ids[1]
    for sid in ids:
        for f in ("manifest.json", "summary.json", "raw.json", "triage.json", "triage.md"):
            assert store.exists(f"{sid}/{f}"), f"{sid}/{f} missing"
        s = store.get_json(f"{sid}/summary.json")
        assert s["load_profile"] and isinstance(s.get("top_sql"), list)
    m = store.get_json(f"{ids[1]}/manifest.json")
    assert m["trigger"] == {"source": "test", "reason": "live test 2"} and m["window"]["mode"] == "incident"
    assert second["source"] == "rules" and second["severity"] in ("high", "medium", "low", "info") and second["delivered"] == ["stdout"]
    if t.engine == "oracle":
        assert set(m.get("reports", [])) >= {"awr.txt"} or not t.diagnostics_pack
    if store.exists(f"{ids[1]}/compare.json"):  # cumulative engines compare the second run against the first
        c = store.get_json(f"{ids[1]}/compare.json")
        assert "load_profile" in c and "sql" in c and "findings" in c
    assert "DB review" in capsys.readouterr().out


def test_agent_tool_loop_over_http(lab_cfg, fake_llm):
    """The reviewer's real loop (OpenAI-compatible backend, real tools over a real snapshot), the model faked."""
    from dbtriage.cli import run_request
    from dbtriage.triggers import TriageRequest

    t = lab_cfg.targets.get("pg-lab")
    if t is None or not reachable(t)[0]:
        pytest.skip("pg-lab not reachable")
    cfg = replace(lab_cfg, agent={"provider": "ollama", "model": "fake-model", "base_url": fake_llm.url})
    n0 = len(fake_llm.requests)
    run_request(TriageRequest(target="pg-lab", mode="scheduled", source="test"), cfg, use_llm=True)
    out = run_request(TriageRequest(target="pg-lab", mode="incident", source="test"), cfg, use_llm=True)
    assert out["source"] == "agent" and out["provider"] == "ollama" and out["tool_calls"] >= 2, out
    assert out["findings"][0]["summary"] == "Fake model saw the comparison" and out["confidence"] == "low"
    calls = [m["tool_calls"][0]["function"]["name"] for r in fake_llm.requests[n0:] for m in r["messages"] if m.get("tool_calls")]
    assert "compare_snapshots" in calls or "get_report" in calls
    # the tool results the fake model received were real data from the stored snapshot
    tool_results = [m["content"] for r in fake_llm.requests[n0:] for m in r["messages"] if m["role"] == "tool"]
    assert any("load_profile" in c or "xact_commit" in c for c in tool_results)


def test_s3_store_round_trip(lab_env):
    import boto3

    if not os.environ.get("AWS_ENDPOINT_URL"):
        pytest.skip("no AWS_ENDPOINT_URL: the lab S3 is not configured")
    from dbtriage.storage import S3Storage

    bucket = f"dbtriage-test-{uuid.uuid4().hex[:8]}"
    st = S3Storage(bucket, "lab/x")
    try:
        st.put_json("pg/a/2026-11-05/T1/manifest.json", {"id": "T1"})
        st.put_text("pg/a/2026-11-05/T1/awr.txt", "WORKLOAD")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"lab S3 not reachable: {type(exc).__name__}")
    try:
        assert st.get_json("pg/a/2026-11-05/T1/manifest.json") == {"id": "T1"} and st.exists("pg/a/2026-11-05/T1/awr.txt")
        with pytest.raises(FileNotFoundError):  # a real S3 reports a missing key like every other store
            st.get("pg/a/2026-11-05/T1/missing.json")
        assert st.list_snapshots("pg", "a") == ["pg/a/2026-11-05/T1"] and not st.exists("nope")
        # the conditional write the ledger relies on: the endpoint must refuse a stale ETag, not take the fallback
        from dbtriage.storage import Conflict, S3Storage

        st.put_if("pg/a/findings.json", b'{"generation": 1}', None)
        v = st.version_of("pg/a/findings.json")
        st.put_if("pg/a/findings.json", b'{"generation": 2}', v)
        with pytest.raises(Conflict):
            st.put_if("pg/a/findings.json", b'{"generation": 9}', v)
        with pytest.raises(Conflict):
            st.put_if("pg/a/findings.json", b'{"generation": 9}', None)
        assert st.get_json("pg/a/findings.json") == {"generation": 2} and S3Storage._conditional, "endpoint took the read-compare-write fallback"
    finally:
        s3 = boto3.client("s3", endpoint_url=os.environ["AWS_ENDPOINT_URL"])
        for o in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=o["Key"])
        s3.delete_bucket(Bucket=bucket)


def test_oracle_capture_ends_on_the_newest_awr_snapshot(lab_cfg):
    """The capture window must line up with the database's own clock whatever this machine's time zone is
    (a session-time-zone conversion once shifted it by the client's UTC offset and picked hours-old AWR data)."""
    import oracledb

    from dbtriage.cli import collect
    from dbtriage.storage import open_storage
    from dbtriage.triggers import TriageRequest

    t = lab_cfg.targets.get("ora-lab")
    if t is None or not t.diagnostics_pack or not reachable(t)[0]:
        pytest.skip("ora-lab with the Diagnostics Pack is not reachable")
    with oracledb.connect(dsn=t.dsn, user=t.extra.get("user"), password=t.extra.get("password")) as c:
        cur = c.cursor()
        cur.execute("alter session set time_zone = '-05:00'")  # what a client in Toronto or New York sends
        cur.callproc("dbms_workload_repository.create_snapshot")
        cur.execute("select max(snap_id) from dba_hist_snapshot where dbid = (select dbid from v$database)")
        newest = cur.fetchone()[0]
    store = open_storage(lab_cfg.storage_url)
    sid = collect(lab_cfg, store, TriageRequest(target="ora-lab", mode="scheduled", window_minutes=60, source="test"))
    awr = store.get_json(f"{sid}/summary.json")["awr"]
    assert awr["end_snap"] == newest, awr


def test_opt_in_awr_snapshot_on_the_lab(lab_cfg):
    """With writes allowed and the target asking, a run takes an AWR snapshot first and its window ends with it;
    a run right after finds it recent enough. With writes off, the same target takes none."""
    from dbtriage.cli import collect
    from dbtriage.storage import open_storage
    from dbtriage.triggers import TriageRequest

    t = lab_cfg.targets.get("ora-lab")
    if t is None or not t.diagnostics_pack or not reachable(t)[0]:
        pytest.skip("ora-lab with the Diagnostics Pack is not reachable")
    t = replace(t, extra={**t.extra, "create_snapshot": "always", "min_snapshot_age_min": 0})  # take one whatever the age
    store = open_storage(lab_cfg.storage_url)
    req = TriageRequest(target="ora-lab", mode="scheduled", window_minutes=60, source="test")
    off = replace(lab_cfg, targets={**lab_cfg.targets, "ora-lab": t}, collect={})
    assert store.get_json(f"{collect(off, store, req)}/manifest.json")["db_snapshot"]["status"] == "skipped"
    on = replace(off, collect={"allow_db_writes": True})
    sid = collect(on, store, req)
    taken = store.get_json(f"{sid}/manifest.json")["db_snapshot"]
    assert taken["status"] == "taken" and taken["detail"].startswith("AWR snapshot "), taken
    snap_id = int(taken["detail"].rsplit(" ", 1)[1])
    assert store.get_json(f"{sid}/summary.json")["awr"]["end_snap"] == snap_id  # the review's window ends with it
    patient = replace(on, targets={**on.targets, "ora-lab": replace(t, extra={**t.extra, "min_snapshot_age_min": 60})})
    again = collect(patient, store, req)  # the one just taken is recent enough
    assert store.get_json(f"{again}/manifest.json")["db_snapshot"]["status"] == "not needed"


def test_oracle_without_the_pack_falls_back_from_statspack_to_v_dollar(lab_cfg):
    """ora-lab reads the CDB root, where Statspack is not installed (it lives in the PDB, see ora-statspack): auto
    without the pack says so and reads V$, never DBA_HIST; an explicit statspack stops. Read only: no snapshot is taken."""
    from datetime import timedelta

    from dbtriage.collectors.base import Window
    from dbtriage.collectors.oracle import OracleCollector

    t = lab_cfg.targets.get("ora-lab")
    if t is None or not reachable(t)[0]:
        pytest.skip("ora-lab not reachable")
    now = datetime.now(timezone.utc)
    window = Window(start=now - timedelta(hours=1), end=now, mode="adhoc")
    s = OracleCollector(replace(t, diagnostics_pack=False, extra={**t.extra, "oracle_source": "auto"})).collect(window).summary
    assert s["oracle_source"] == "vdollar" and s["metrics_kind"] == "cumulative" and "awr" not in s, s.get("notes")
    assert "Statspack cannot be read" in s["notes"][0] and "ORA-00942" in s["notes"][0], s["notes"]
    with pytest.raises(SystemExit, match="oracle_source = statspack, but PERFSTAT.STATS\\$SNAPSHOT cannot be read"):
        OracleCollector(replace(t, diagnostics_pack=False, extra={**t.extra, "oracle_source": "statspack"})).collect(window)


def test_statspack_is_read_from_the_lab_pdb(lab_cfg):
    """The lab PDB with Statspack installed and at least two snapshots from one startup: auto without the pack reads
    Statspack, with full statement text from STATS$SQLTEXT and plan hashes at level 6+. Read only: it reads the
    snapshots that are there and takes none. Skips until Statspack is installed (docs/operations.md, The Oracle lab)."""
    from datetime import timedelta

    from dbtriage.collectors import oracle
    from dbtriage.collectors.base import Window

    t = lab_cfg.targets.get("ora-statspack")
    if t is None or not reachable(t)[0]:
        pytest.skip("ora-statspack not reachable (ORACLE_PDB_DSN in .env)")
    conn = oracle.connect(t)
    try:
        cur = conn.cursor()
        cur.execute("""select count(*), max(snap_level) from perfstat.stats$snapshot
                       where startup_time = (select max(startup_time) from perfstat.stats$snapshot)""")
        n, level = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - not installed, or not granted: nothing to test yet
        pytest.skip(f"Statspack not readable in the lab PDB: {str(exc).splitlines()[0]}")
    finally:
        conn.close()
    if n < 2:
        pytest.skip("fewer than two Statspack snapshots since the instance started")
    now = datetime.now(timezone.utc)
    snap = oracle.OracleCollector(t).collect(Window(start=now - timedelta(days=7), end=now, mode="adhoc"))
    s = snap.summary
    assert s["oracle_source"] == "statspack" and s["metrics_kind"] == "interval" and s["notes"][0].startswith("Source: Statspack"), s["notes"]
    assert "statspack.txt" in snap.reports and "Load profile" in snap.reports["statspack.txt"]
    if level >= 5 and s["top_sql"]:
        assert not any(n_.startswith("No full SQL text") for n_ in s["notes"]), s["notes"]  # SELECT on STATS$SQLTEXT is granted
        assert all(q["text"] and not q["text"].endswith(" …") for q in s["top_sql"])
        assert not any("statspack.snap" in q["text"].lower() for q in s["top_sql"])
    if level >= 6 and s["top_sql"]:
        assert any(q.get("plan_hash") for q in s["top_sql"])


@pytest.mark.parametrize("name", ["mysql-lab", "mariadb-lab"])
def test_mysql_collection_session_is_read_only(lab_cfg, name):
    from dbtriage.collectors.mysql import connect

    t = lab_cfg.targets.get(name)
    if t is None or not reachable(t)[0]:
        pytest.skip(f"{name} not reachable")
    conn = connect(t)
    try:
        cur = conn.cursor()
        cur.execute("select @@session.transaction_read_only as ro")
        assert cur.fetchone()["ro"] == 1
    finally:
        conn.close()


def test_awr_window_bounds_keep_fractions_of_a_second(lab_cfg):
    """A window ending a moment after a snapshot must include the next snapshot, whose interval begins there. The
    driver binds datetimes as DATE, dropping fractions of a second, so it was left out whenever both fell in the
    same second (the intermittent test_opt_in_awr_snapshot_on_the_lab failure)."""
    from datetime import timedelta

    from dbtriage.collectors.base import Window
    from dbtriage.collectors.oracle import OracleCollector, connect, take_awr_snapshot

    t = lab_cfg.targets.get("ora-lab")
    if t is None or not t.diagnostics_pack or not reachable(t)[0]:
        pytest.skip("ora-lab with the Diagnostics Pack is not reachable")
    t = replace(t, extra={**t.extra, "min_snapshot_age_min": 0})
    now = datetime.now(timezone.utc)
    take_awr_snapshot(t, Window(start=now - timedelta(hours=1), end=now))
    b = int(take_awr_snapshot(t, Window(start=now - timedelta(hours=1), end=now)).rsplit(" ", 1)[1])
    with connect(t) as c:
        cur = c.cursor()
        cur.execute("select begin_interval_time, (sysdate - cast(sys_extract_utc(systimestamp) as date)) * 86400 "
                    "from dba_hist_snapshot where snap_id = :b", b=b)
        begin_local, off_s = cur.fetchone()
    if begin_local.microsecond >= 999000:
        pytest.skip("snapshot B began at the very end of a second; nothing to tell apart")
    end_utc = (begin_local - timedelta(seconds=float(off_s))).replace(tzinfo=timezone.utc) + timedelta(milliseconds=1)
    snap = OracleCollector(t).collect(Window(start=end_utc - timedelta(minutes=60), end=end_utc, mode="adhoc"))
    assert snap.summary["awr"]["end_snap"] == b, (snap.summary["awr"], begin_local.isoformat(), end_utc.isoformat())


def test_sqlserver_login_reads_but_cannot_write(lab_cfg):
    """The monitoring login sees the DMVs and Query Store, and the database refuses it any change."""
    t = lab_cfg.targets.get("mssql-lab")
    if t is None or not reachable(t)[0]:
        pytest.skip("mssql-lab not reachable (kubectl -n dbtriage-lab port-forward svc/mssql 14330:1433)")
    from dbtriage.collectors.sqlserver import connect

    c = connect(t)
    try:
        cur = c.cursor()
        cur.execute("select count(*) from sys.dm_exec_query_stats")
        assert cur.fetchone()[0] >= 0
        with pytest.raises(Exception, match="permission was denied"):
            cur.execute("insert into dbo.orders (customer_id, status, amount, created_at) values (1, 'new', 1, sysutcdatetime())")
    finally:
        c.close()
