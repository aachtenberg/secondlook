"""A finding that stays a finding: the per-target ledger (dbtriage.ledger), the finding-level delivery gate and
tickets as state (dbtriage.deliver), and the life shown on the snapshot page and in the markdown."""
from __future__ import annotations

import json

from dbtriage import deliver as d
from dbtriage import ledger
from dbtriage.storage import LocalStorage, ResilientStorage

ENGINE, TARGET = "postgres", "demo"


def rule(kind: str, subject: str | None = "q1", severity: str = "medium", title: str | None = None) -> dict:
    ev = {"sql_id": subject} if subject else {}
    return {"kind": kind, "severity": severity, "title": title or f"{kind} on {subject}", "detail": "", "evidence": ev, "score": 50}


def review(f: dict) -> dict:
    """The review-shaped finding the rules review writes for a rules finding."""
    return {"kind": f["kind"], "subject": (f.get("evidence") or {}).get("sql_id", ""), "severity": f["severity"],
            "where": f"sql_id {(f.get('evidence') or {}).get('sql_id', '')}", "summary": f["title"], "impact": "", "evidence": [], "fix": ""}


def sid(n: int) -> str:
    return f"{ENGINE}/{TARGET}/2026-11-05/20261105T{9 + n:02d}0000Z"


def at(n: int) -> str:
    return f"2026-11-05T{9 + n:02d}:00:00+00:00"


def run(store, n: int, rules: list[dict], gone_after: int = 3) -> dict:
    return ledger.update(store, ENGINE, TARGET, sid(n), at(n), rules, [review(f) for f in rules[:5]], gone_after)


# ---------------------------------------------------------------- states
def test_every_state_in_order(tmp_path):
    st = LocalStorage(tmp_path)
    out = run(st, 0, [rule("plan_change")])
    assert [l["state"] for l in out["life"]] == ["new"] and out["still_open"] == [] and out["gone"] == []
    assert out["life"][0]["first_seen"] == sid(0) and out["life"][0]["first_seen_at"] == at(0) and out["life"][0]["tickets"] == {}

    out = run(st, 1, [rule("plan_change")])
    assert out["life"][0]["state"] == "still_open" and out["life"][0]["first_seen"] == sid(0)  # the first sighting stays

    out = run(st, 2, [rule("plan_change", severity="high")])
    assert out["life"][0]["state"] == "worse"
    e = ledger.load(st, ENGINE, TARGET)["entries"][0]
    assert (e["severity"], e["peak"], e["last_seen"], e["misses"]) == ("high", "high", sid(2), 0)

    out = run(st, 3, [rule("plan_change", severity="low")])
    assert out["life"][0]["state"] == "quiet"  # present, below its own severity last run
    e = ledger.load(st, ENGINE, TARGET)["entries"][0]
    assert (e["severity"], e["peak"]) == ("low", "high")  # the peak is remembered

    out = run(st, 4, [rule("plan_change", severity="low")])
    assert out["life"][0]["state"] == "still_open"  # quiet is one run's word; the same lower severity again is just open


def test_gone_after_n_consecutive_misses_then_dropped(tmp_path):
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("plan_change")])
    # absent, but not yet gone: still listed as open, with how long it has been unseen
    out = run(st, 1, [])
    assert out["life"] == [] and out["gone"] == []
    assert [(e["state"], e["misses"]) for e in out["still_open"]] == [("still_open", 1)]
    out = run(st, 2, [])
    assert out["gone"] == [] and out["still_open"][0]["misses"] == 2
    out = run(st, 3, [])  # the third miss: reported gone, once, and removed
    assert [e["state"] for e in out["gone"]] == ["gone"] and out["gone"][0]["misses"] == 3 and out["still_open"] == []
    assert ledger.load(st, ENGINE, TARGET)["entries"] == []
    out = run(st, 4, [])
    assert out["gone"] == []  # not reported twice
    out = run(st, 5, [rule("plan_change")])  # back within the tombstone window: the same finding, with its history
    assert out["life"][0]["state"] == "returned" and out["life"][0]["first_seen"] == sid(0)
    assert ledger.load(st, ENGINE, TARGET)["tombstones"] == []


def test_a_sighting_resets_the_misses(tmp_path):
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("plan_change")])
    run(st, 1, [])
    run(st, 2, [])
    out = run(st, 3, [rule("plan_change")])  # seen again on what would have been the third miss
    assert out["life"][0]["state"] == "returned" and out["gone"] == []
    assert ledger.load(st, ENGINE, TARGET)["entries"][0]["misses"] == 0
    out = run(st, 4, [])
    assert out["gone"] == [] and out["still_open"][0]["misses"] == 1  # the count starts over


def test_the_threshold_is_configurable():
    led = {"entries": [], "updated": None}  # in memory, through apply()
    ledger.apply(led, sid(0), at(0), [rule("deadlocks", None)], [], gone_after=1)
    out = ledger.apply(led, sid(1), at(1), [], [], gone_after=1)
    assert [e["kind"] for e in out["gone"]] == ["deadlocks"] and led["entries"] == []  # N=1: gone on the first miss
    assert ledger.gone_after({}) == 3 and ledger.gone_after(None) == 3
    assert ledger.gone_after({"gone_after_runs": 5}) == 5 and ledger.gone_after({"gone_after_runs": "2"}) == 2
    assert ledger.gone_after({"gone_after_runs": 0}) == 3 and ledger.gone_after({"gone_after_runs": "x"}) == 3  # blank or nonsense: the default


def hourly(store, day: int, hour: int, rules: list[dict], gone_after: int = 3) -> dict:
    """One hourly run on 2026-11-(5+day) at `hour`, the shape of an AWR interval a day."""
    s = f"{ENGINE}/{TARGET}/2026-11-{5 + day:02d}/202611{5 + day:02d}T{hour:02d}0000Z"
    return ledger.update(store, ENGINE, TARGET, s, f"2026-11-{5 + day:02d}T{hour:02d}:00:00+00:00", rules, [review(f) for f in rules[:5]], gone_after)


def test_a_nightly_batch_under_hourly_runs_does_not_flap(tmp_path):
    """Hourly snapshots, a statement that only shows at 02:00: the first night it is gone by 05:00 (no cadence yet),
    the second night it is a return with a one-day period, and from then on three quiet hours do not clear it."""
    st = LocalStorage(tmp_path)
    batch = [rule("sql_top", "batch1", "high")]
    hourly(st, 0, 2, batch)
    for h in range(3, 6):
        out = hourly(st, 0, h, [])
    assert [e["kind"] for e in out["gone"]] == ["sql_top"]  # night one: three misses, no period to go on
    assert len(ledger.load(st, ENGINE, TARGET)["tombstones"]) == 1
    for h in range(6, 24):
        assert hourly(st, 0, h, [])["gone"] == []
    out = hourly(st, 1, 2, batch)  # night two
    assert out["life"][0]["state"] == "returned" and out["life"][0]["first_seen"].endswith("20261105T020000Z")
    assert out["life"][0]["period_s"] == 86400.0
    for h in range(3, 24):  # the whole day: still open, never gone (3 x 24h is the new threshold)
        out = hourly(st, 1, h, [])
        assert out["gone"] == [] and out["still_open"][0]["state"] == "still_open"
    out = hourly(st, 2, 2, batch)  # night three: same gap as before, so it is the usual batch
    assert out["life"][0]["state"] == "recurring" and out["life"][0]["returns"] == 2
    # it does get reported gone when it really stops: three days past its last sighting
    for day in (3, 4):
        for h in range(0, 24):
            assert hourly(st, day, h, [])["gone"] == []
    assert [e["kind"] for e in hourly(st, 5, 3, [])["gone"]] == ["sql_top"]


def test_a_recurring_finding_that_gets_worse_is_worse(tmp_path):
    st = LocalStorage(tmp_path)
    hourly(st, 0, 2, [rule("sql_top", "b", "medium")])
    hourly(st, 0, 5, [])
    hourly(st, 1, 2, [rule("sql_top", "b", "medium")])
    hourly(st, 1, 5, [])
    out = hourly(st, 2, 2, [rule("sql_top", "b", "high")])
    assert out["life"][0]["state"] == "worse" and ledger.load(st, ENGINE, TARGET)["entries"][0]["peak"] == "high"


def test_tombstones_expire(tmp_path):
    st = LocalStorage(tmp_path)
    hourly(st, 0, 2, [rule("deadlocks", None)])
    for h in (3, 4, 5):
        hourly(st, 0, h, [])
    assert len(ledger.load(st, ENGINE, TARGET)["tombstones"]) == 1
    hourly(st, 8, 2, [])  # eight days later the tombstone is past TOMBSTONE_DAYS
    assert ledger.load(st, ENGINE, TARGET)["tombstones"] == []
    out = hourly(st, 8, 3, [rule("deadlocks", None)])
    assert out["life"][0]["state"] == "new"  # too long ago to be the same finding


def test_triaging_the_same_snapshot_again_does_not_count_as_another_run(tmp_path):
    """The rules review runs, then the model review of the same snapshot: one run, not two misses."""
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("plan_change")])
    out = run(st, 1, [])
    assert out["still_open"][0]["misses"] == 1
    out = run(st, 1, [])  # the same snapshot, triaged again
    assert out["still_open"][0]["misses"] == 1 and ledger.load(st, ENGINE, TARGET)["entries"][0]["misses"] == 1
    out = run(st, 1, [rule("plan_change")])  # and again, this time the review names it: a sighting, not a return
    assert out["life"][0]["state"] == "still_open" and out["life"][0]["misses"] == 0
    out = run(st, 2, [])
    assert out["still_open"][0]["misses"] == 1  # history continues from the last word on snapshot 1
    # re-triaging an older snapshot leaves the ledger alone and gives that review no life
    assert ledger.update(st, ENGINE, TARGET, sid(0), at(0), [], []) is None
    assert ledger.load(st, ENGINE, TARGET)["updated"] == sid(2)
    result = {"snapshot": sid(0)}
    from dbtriage.config import Config
    ledger.attach(result, st, Config(storage_url="x", targets={}, delivery={}), {"findings": []}, {"engine": ENGINE, "target": TARGET, "captured_at": at(0)})
    assert "life" not in result


def test_a_save_that_lost_the_race_is_applied_again(tmp_path, monkeypatch):
    """Two writers on one ledger: the second sees the generation moved and redoes its work on the new state."""
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("plan_change")])
    real_load, raced = ledger.load, {"done": False}

    def load_then_rival_writes(store, engine, target):
        led = real_load(store, engine, target)
        if not raced["done"]:  # between our load and our save, another process records a ticket
            raced["done"] = True
            ledger.record_tickets(store, {"engine": engine, "target": target, "snapshot": None,
                                          "findings": [review(rule("plan_change"))], "life": [{"tickets": {"jira": {"key": "DBA-3"}}}]})
        return led

    monkeypatch.setattr(ledger, "load", load_then_rival_writes)
    out = run(st, 1, [rule("plan_change"), rule("deadlocks", None)])
    monkeypatch.setattr(ledger, "load", real_load)
    led = ledger.load(st, ENGINE, TARGET)
    by = {e["kind"]: e for e in led["entries"]}
    assert by["plan_change"]["tickets"]["jira"]["key"] == "DBA-3" and by["deadlocks"]["state"] == "new"  # both writers' work kept
    assert out["life"][0]["tickets"]["jira"]["key"] == "DBA-3" and led["generation"] == 3


def test_runs_of_one_target_take_turns():
    from dbtriage.cli import target_lock

    assert target_lock("pg") is target_lock("pg") and target_lock("pg") is not target_lock("ora")


def test_a_copy_that_lost_every_race_is_never_written(tmp_path, monkeypatch):
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("plan_change")])
    real_load = ledger.load

    def load_then_rival_always_writes(store, engine, target):
        led = real_load(store, engine, target)
        rival = dict(real_load(store, engine, target), updated=sid(0))  # someone else saves every time (an older-looking id so ours still applies)
        rival["entries"][0]["title"] = "rival"
        ledger.save(store, engine, target, rival)
        return led

    monkeypatch.setattr(ledger, "load", load_then_rival_always_writes)
    import pytest
    with pytest.raises(ledger.LedgerContended):
        run(st, 1, [rule("deadlocks", None)])
    monkeypatch.setattr(ledger, "load", real_load)
    led = ledger.load(st, ENGINE, TARGET)
    assert led["entries"][0]["title"] == "rival" and [e["kind"] for e in led["entries"]] == ["plan_change"]  # the winner's file, untouched


def test_declined_updates_do_not_write(tmp_path):
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("plan_change")]); run(st, 1, [])
    gen = ledger.load(st, ENGINE, TARGET)["generation"]
    assert ledger.update(st, ENGINE, TARGET, sid(0), at(0), [], []) is None  # an older snapshot
    ledger.record_tickets(st, {"engine": ENGINE, "target": TARGET, "snapshot": None, "findings": [review(rule("bloat", "t"))],
                              "life": [{"tickets": {"jira": {"key": "DBA-0"}}}], "still_open": []})  # matches no entry
    assert ledger.load(st, ENGINE, TARGET)["generation"] == gen


def test_re_triaging_the_latest_snapshot_keeps_tickets_recorded_since(tmp_path):
    """Rules review, delivery opens a ticket, then the model review of the same snapshot: the ticket survives."""
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("plan_change")])
    out = run(st, 1, [rule("plan_change")])
    ledger.record_tickets(st, {"engine": ENGINE, "target": TARGET, "snapshot": None, "findings": [review(rule("plan_change"))],
                              "life": [dict(out["life"][0], tickets={"jira": {"key": "DBA-4"}})], "still_open": []})
    out = run(st, 1, [rule("plan_change")])  # the same snapshot again
    assert out["life"][0]["tickets"]["jira"]["key"] == "DBA-4"
    assert ledger.load(st, ENGINE, TARGET)["entries"][0]["tickets"]["jira"]["key"] == "DBA-4"


def test_gone_rows_carry_the_cadence(tmp_path):
    st = LocalStorage(tmp_path)
    batch = [rule("sql_top", "b", "low")]
    hourly(st, 0, 2, batch); hourly(st, 0, 5, []); hourly(st, 1, 2, batch)
    out = None
    for day in (2, 3, 4):
        for h in range(0, 24):
            out = hourly(st, day, h, [])
            if out["gone"]:
                break
        if out["gone"]:
            break
    assert out["gone"][0]["period_s"] == 86400.0 and out["gone"][0]["returns"] == 1 and out["gone"][0]["title"]


def test_rebuild_starts_over_when_a_run_lands_meanwhile(tmp_path, monkeypatch):
    st = LocalStorage(tmp_path)
    st.put_json(f"{sid(0)}/manifest.json", {"captured_at": at(0)}); st.put_json(f"{sid(0)}/compare.json", {"findings": [rule("plan_change")]})
    st.put_json(f"{sid(0)}/triage.json", {"findings": [review(rule("plan_change"))], "source": "rules"})
    run(st, 0, [rule("plan_change")])
    real, calls = ledger._rebuilt, {"n": 0}

    def rebuilt_while_a_run_lands(store, engine, target, gone_after):
        calls["n"] += 1
        led = real(store, engine, target, gone_after)
        if calls["n"] == 1:  # a run saves while we were reading
            st.put_json(f"{sid(1)}/manifest.json", {"captured_at": at(1)}); st.put_json(f"{sid(1)}/compare.json", {"findings": []})
            st.put_json(f"{sid(1)}/triage.json", {"findings": [], "source": "rules"})
            run(st, 1, [])
        return led

    monkeypatch.setattr(ledger, "_rebuilt", rebuilt_while_a_run_lands)
    led = ledger.rebuild(st, ENGINE, TARGET)
    assert calls["n"] == 2 and led["updated"] == sid(1) and led["entries"][0]["misses"] == 1  # the second pass saw the new run


def test_spool_never_replays_an_older_ledger_over_a_newer_one(tmp_path):
    """Coming out of an outage: the spooled ledger is dropped when the store already has a newer generation,
    pushed when it is the newer one, and a read never returns the stale copy."""
    primary = LocalStorage(tmp_path / "primary")
    st = ResilientStorage(primary, LocalStorage(tmp_path / "spool"))
    k = ledger.key(ENGINE, TARGET)
    st.spool.put_json(k, {"entries": [{"kind": "old"}], "tombstones": [], "updated": sid(0), "prior": None, "generation": 2})
    primary.put_json(k, {"entries": [{"kind": "newer"}], "tombstones": [], "updated": sid(1), "prior": None, "generation": 3})
    assert ledger.load(st, ENGINE, TARGET)["entries"] == [{"kind": "newer"}]  # the read prefers the newer generation
    assert not st.spool.exists(k)  # and the stale copy is gone
    st.spool.put_json(k, {"entries": [{"kind": "fresh"}], "tombstones": [], "updated": sid(2), "prior": None, "generation": 4})
    assert st.flush() == (1, 0) and primary.get_json(k)["generation"] == 4 and not st.spool.exists(k)
    st.spool.put_json(k, {"entries": [{"kind": "stale"}], "tombstones": [], "updated": sid(0), "prior": None, "generation": 1})
    assert st.flush() == (1, 0) and primary.get_json(k)["generation"] == 4  # dropped, not replayed
    # while a run holds the database, the ledger file waits for the next flush
    from dbtriage.locks import target_lock
    st.spool.put_json(k, {"entries": [{"kind": "waiting"}], "tombstones": [], "updated": sid(3), "prior": None, "generation": 5})
    with target_lock(TARGET):
        assert st.flush() == (0, 1)
    assert st.flush() == (1, 0) and primary.get_json(k)["entries"] == [{"kind": "waiting"}]


def test_still_open_rows_carry_the_cadence(tmp_path):
    st = LocalStorage(tmp_path)
    batch = [rule("sql_top", "b", "low")]
    hourly(st, 0, 2, batch); hourly(st, 0, 5, []); hourly(st, 1, 2, batch)
    out = hourly(st, 1, 5, [rule("blocking", "", "high")] * 0 + [])
    row = out["still_open"][0]
    assert row["period_s"] == 86400.0 and row["returns"] == 1 and row["title"] and row["severity"] == "low"
    from dbtriage.report import life_note
    assert "about every 24h" in life_note(row)


def test_a_shared_ticket_is_not_told_cleared_while_a_sibling_is_open(monkeypatch):
    seen = fake_post(monkeypatch)
    t = triage([{"state": "still_open", "first_seen": sid(0), "tickets": {"jira": {"key": "DBA-5", "id": "5", "url": "u"}}}],
               [review(rule("plan_change", severity="medium"))],
               gone=[{"kind": "deadlocks", "subject": "", "title": "2 deadlocks", "misses": 3, "first_seen_at": at(0), "tickets": {"jira": {"key": "DBA-5", "id": "5", "url": "u"}}}])
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, t, "md") == ["jira"]
    assert seen[0][0].endswith("/issue/DBA-5/comment") and seen[0][1]["body"].startswith("deadlocks : 2 deadlocks cleared; this issue still tracks 1 open finding")
    t2 = triage([], [], gone=[{"kind": "deadlocks", "subject": "", "title": "2 deadlocks", "misses": 3, "first_seen_at": at(0), "tickets": {"jira": {"key": "DBA-6", "id": "6", "url": "u"}}}])
    d.deliver({"channels": ["jira"], "jira": JIRA}, t2, "md")
    assert seen[-1][1]["body"].startswith("cleared: deadlocks")


def test_period_words():
    assert ledger.describe_period(None) == "" and ledger.describe_period(0) == ""
    assert ledger.describe_period(3600) == "about every 1h" and ledger.describe_period(86400) == "about every 24h"
    assert ledger.describe_period(7 * 86400) == "about every 7d" and ledger.describe_period(600) == "about every 10min"


def test_identity_is_kind_and_subject_not_wording(tmp_path):
    st = LocalStorage(tmp_path)
    run(st, 0, [rule("sql_regressed", "ABC", title="SQL ABC is 3x slower")])
    out = run(st, 1, [rule("sql_regressed", "abc", title="SQL abc is 5x slower")])
    assert out["life"][0]["state"] == "still_open"  # case and words differ, the finding does not
    e = ledger.load(st, ENGINE, TARGET)["entries"][0]
    assert e["title"] == "SQL abc is 5x slower" and (e["kind"], e["subject"]) == ("sql_regressed", "abc")


# ---------------------------------------------------------------- the five slots
def test_still_open_items_do_not_take_a_slot_from_the_five(tmp_path):
    st = LocalStorage(tmp_path)
    seven = [rule("sql_regressed", f"q{i}") for i in range(7)]
    out = run(st, 0, seven)
    assert len(out["life"]) == 5 and all(l["state"] == "new" for l in out["life"])
    assert sorted(e["subject"] for e in out["still_open"]) == ["q5", "q6"]  # tracked, just not in the five
    # next run: a new high leads, the seven are still there; the review's five are life-marked, the rest are still open
    rules = [rule("blocking", "", severity="high")] + seven
    out = run(st, 1, rules)
    assert [l["state"] for l in out["life"]] == ["new", "still_open", "still_open", "still_open", "still_open"]
    assert sorted(e["subject"] for e in out["still_open"]) == ["q4", "q5", "q6"] and all(e["state"] == "still_open" for e in out["still_open"])
    assert len(ledger.load(st, ENGINE, TARGET)["entries"]) == 8


def test_a_model_only_finding_is_tracked_by_its_own_appearances(tmp_path):
    st = LocalStorage(tmp_path)
    model = {"kind": "wait_shift", "subject": "IO", "severity": "medium", "where": "IO", "summary": "IO waits doubled"}
    out = ledger.update(st, ENGINE, TARGET, sid(0), at(0), [rule("plan_change")], [model, review(rule("plan_change"))])
    assert [l["state"] for l in out["life"]] == ["new", "new"]
    out = ledger.update(st, ENGINE, TARGET, sid(1), at(1), [rule("plan_change")], [review(rule("plan_change"))])
    assert [e["subject"] for e in out["still_open"]] == ["io"] and out["still_open"][0]["misses"] == 1


# ---------------------------------------------------------------- storage
def test_ledger_round_trips_through_the_store_without_disturbing_snapshots(tmp_path):
    st = LocalStorage(tmp_path)
    st.put_json(f"{sid(0)}/manifest.json", {"captured_at": at(0)})
    run(st, 0, [rule("plan_change")])
    assert ledger.key(ENGINE, TARGET) == "postgres/demo/findings.json" and st.exists("postgres/demo/findings.json")
    data = json.loads(st.get("postgres/demo/findings.json"))
    assert data["updated"] == sid(0) and data["entries"][0]["kind"] == "plan_change"
    assert st.list_snapshots(ENGINE, TARGET) == [sid(0)]  # the ledger is not a snapshot
    assert ledger.load(st, ENGINE, "nobody") == {"entries": [], "tombstones": [], "updated": None, "prior": None, "generation": 0}


def test_ledger_writes_go_through_the_spool_during_an_outage(tmp_path, capsys):
    from dbtriage.config import Config
    from tests.test_pg_resilience import Flaky

    class WriteFails(Flaky):
        """The store answers reads but refuses writes (a bucket gone read-only, a replica)."""
        def put(self, key, data):
            raise ConnectionError("store is down")

    ResilientStorage.RETRY_S = 0.0
    remote = WriteFails(tmp_path / "remote")
    st = ResilientStorage(remote, LocalStorage(tmp_path / "spool"))
    out = run(st, 0, [rule("plan_change")])
    assert out["life"][0]["state"] == "new" and "postgres/demo/findings.json" in st.spooled()  # kept locally like any snapshot file
    out = run(st, 1, [rule("plan_change", severity="high")])
    assert out["life"][0]["state"] == "worse"  # reads merge the spool, so the second run sees the first
    remote.__class__ = Flaky
    assert st.flush()[0] == 1 and st.spooled() == []
    assert json.loads(remote.get("postgres/demo/findings.json"))["entries"][0]["severity"] == "high"  # replayed
    # a store that cannot be read is not mistaken for an empty ledger: the run goes on without a life, nothing is written
    remote.up = False
    result = {"snapshot": sid(2), "findings": [review(rule("plan_change"))]}
    ledger.attach(result, st, Config(storage_url="x", targets={}), {"findings": [rule("plan_change")]}, {"engine": ENGINE, "target": TARGET, "captured_at": at(2)})
    assert "life" not in result and st.spooled() == [] and "finding life skipped" in capsys.readouterr().err
    remote.up = True
    assert ledger.load(st, ENGINE, TARGET)["entries"][0]["severity"] == "high"  # the real ledger is intact


# ---------------------------------------------------------------- rebuild
def stored_run(st, n: int, rules: list[dict], base: str | None, tickets: dict | None = None) -> dict:
    """What cli.triage leaves behind for one run, then the ledger update and (optionally) a delivered ticket."""
    st.put_json(f"{sid(n)}/manifest.json", {"id": sid(n), "engine": ENGINE, "target": TARGET, "captured_at": at(n), "window": {"mode": "adhoc"}, "trigger": {}})
    st.put_json(f"{sid(n)}/summary.json", {"captured_at": at(n)})
    result = {"snapshot": sid(n), "engine": ENGINE, "target": TARGET, "severity": rules[0]["severity"] if rules else "info",
              "findings": [review(f) for f in rules[:5]], "heuristics": rules}
    if base is not None:
        st.put_json(f"{sid(n)}/compare.json", {"findings": rules})
        result.update(ledger.update(st, ENGINE, TARGET, sid(n), at(n), rules, result["findings"]))
    st.put_json(f"{sid(n)}/triage.json", result)
    if tickets:
        for i, t in tickets.items():
            result["life"][i]["tickets"] = t
        ledger.record_tickets(st, result)
    return result


def test_rebuild_equals_the_ledger_built_run_by_run(tmp_path):
    st = LocalStorage(tmp_path)
    stored_run(st, 0, [], None)  # a first snapshot: nothing compared, nothing recorded
    assert not st.exists(ledger.key(ENGINE, TARGET))
    stored_run(st, 1, [rule("plan_change"), rule("sql_regressed", "q2", "high")], sid(0), tickets={1: {"jira": {"key": "DBA-1", "id": "1", "url": "u"}}})
    stored_run(st, 2, [rule("plan_change", severity="high")], sid(1), tickets={0: {"plane": {"key": "OPS-7", "id": "uuid", "url": "p"}}})
    stored_run(st, 3, [rule("plan_change", severity="high"), rule("deadlocks", None)], sid(2))
    stored_run(st, 4, [rule("deadlocks", None)], sid(3))  # sql_regressed q2: third miss, gone
    stored_run(st, 5, [rule("deadlocks", None, "low")], sid(4))
    built = ledger.load(st, ENGINE, TARGET)
    assert [e["subject"] for e in built["entries"]] == ["q1", ""] and built["entries"][0]["tickets"] == {"plane": {"key": "OPS-7", "id": "uuid", "url": "p"}}
    assert json.loads(st.get(f"{sid(1)}/triage.json"))["life"][1]["tickets"]["jira"]["key"] == "DBA-1"  # the review keeps its ticket
    st._p(ledger.key(ENGINE, TARGET)).unlink()
    same = lambda a, b: {k: v for k, v in a.items() if k != "generation"} == {k: v for k, v in b.items() if k != "generation"}  # noqa: E731
    assert same(ledger.rebuild(st, ENGINE, TARGET), built)  # the save counter is the one thing a rebuild need not match
    assert same(ledger.load(st, ENGINE, TARGET), built)


def test_attach_is_a_no_op_without_a_comparison(tmp_path):
    from dbtriage.config import Config

    st = LocalStorage(tmp_path)
    result = {"snapshot": sid(0), "findings": []}
    ledger.attach(result, st, Config(storage_url="x", targets={}), None, {"engine": ENGINE, "target": TARGET, "captured_at": at(0)})
    assert "life" not in result and not st.exists(ledger.key(ENGINE, TARGET))
    ledger.attach(result, st, Config(storage_url="x", targets={}, delivery={"gone_after_runs": 2}), {"findings": [rule("blocking", "")]},
                  {"engine": ENGINE, "target": TARGET, "captured_at": at(0)})
    assert result["life"] == [] and [e["kind"] for e in result["still_open"]] == ["blocking"] and result["gone"] == []


def test_cli_triage_records_the_life_and_the_command_rebuilds(tmp_path, capsys):
    from dbtriage import cli
    from dbtriage.config import Config, Target
    from tests.conftest import seed_store

    st = seed_store(tmp_path / "data")
    cfg = Config(storage_url=f"file://{tmp_path / 'data'}", targets={"demo": Target("demo", "postgres", "d")}, base_dir=str(tmp_path))
    s10, s09 = "postgres/demo/2026-11-05/20261105T100000Z", "postgres/demo/2026-11-05/20261105T090000Z"
    result, _ = cli.triage(cfg, st, s10, s09, "adhoc", use_llm=False)
    assert result["life"] and all(l["state"] == "new" for l in result["life"]) and len(result["life"]) == len(result["findings"])
    assert json.loads(st.get(f"{s10}/triage.json"))["life"][0]["first_seen"] == s10
    led = ledger.load(st, "postgres", "demo")
    assert led["updated"] == s10 and {e["kind"] for e in led["entries"]} == {f["kind"] for f in result["heuristics"]}
    n = len(led["entries"])
    (tmp_path / "targets.toml").write_text(f'[storage]\nurl = "file://{tmp_path / "data"}"\n[targets.demo]\nengine = "postgres"\ndsn = "d"\n')
    cli.main(["--config", str(tmp_path / "targets.toml"), "rebuild-ledger", "--target", "demo"])
    assert f"postgres/demo/findings.json: {n} open finding(s), as of {s10}" in capsys.readouterr().out
    rebuilt = ledger.load(st, "postgres", "demo")
    assert {k: v for k, v in rebuilt.items() if k != "generation"} == {k: v for k, v in led.items() if k != "generation"}


# ---------------------------------------------------------------- delivery: tickets as state and the finding gate
JIRA = {"base_url": "https://j.example.net/", "project_key": "DBA"}
PLANE = {"base_url": "https://plane.example.com", "workspace": "acme", "project_id": "pid", "project_identifier": "OPS"}


def fake_post(monkeypatch, answers: dict | None = None) -> list[tuple[str, dict]]:
    """Replace deliver._post; records (url, body) and answers each URL from `answers` (default: an empty body)."""
    seen = []

    def post(url, body, headers=None, json_body=True):
        seen.append((url, body))
        for frag, ans in (answers or {}).items():
            if frag in url:
                return ans
        return ""

    monkeypatch.setattr(d, "_post", post)
    return seen


def triage(life: list[dict] | None, findings: list[dict], **extra) -> dict:
    t = {"severity": max((f["severity"] for f in findings), key=lambda s: d.RANK[s], default="info"), "target": "demo", "engine": "postgres",
         "title": "x", "snapshot": sid(1), "findings": findings, **extra}
    if life is not None:
        t["life"] = life
    return t


def test_ticket_keys_are_captured_from_the_response(monkeypatch):
    seen = fake_post(monkeypatch, {"/rest/api/2/issue": {"id": "10001", "key": "DBA-12"}, "/issues/": {"id": "uuid-1", "sequence_id": 12}})
    assert d.send_jira(JIRA, {"severity": "high", "target": "t", "title": "x"}, "md") == {"key": "DBA-12", "id": "10001", "url": "https://j.example.net/browse/DBA-12"}
    assert d.send_plane(PLANE, {"severity": "high", "target": "t", "title": "x"}, "md") == {"key": "OPS-12", "id": "uuid-1", "url": "https://plane.example.com/acme/browse/OPS-12/"}
    assert d.send_plane({k: v for k, v in PLANE.items() if k != "project_identifier"}, {"severity": "high", "target": "t", "title": "x"}, "md") == {
        "key": "uuid-1", "id": "uuid-1", "url": "https://plane.example.com/acme/projects/pid/issues/uuid-1"}
    assert seen[0][0] == "https://j.example.net/rest/api/2/issue" and seen[1][0].endswith("/projects/pid/issues/")
    monkeypatch.setattr(d, "_post", lambda *a, **k: "")  # a non-JSON answer (the UI's Send test fake) is not a ticket
    assert d.send_jira(JIRA, {"severity": "high", "target": "t", "title": "x"}, "md") is None


def test_a_new_finding_opens_a_ticket_and_the_ticket_lands_on_its_life(monkeypatch):
    seen = fake_post(monkeypatch, {"/rest/api/2/issue": {"id": "1", "key": "DBA-12"}})
    f = review(rule("plan_change", severity="high"))
    t = triage([{"state": "new", "first_seen": sid(1), "tickets": {}}], [f])
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, t, "md") == ["jira"]
    assert t["life"][0]["tickets"]["jira"]["key"] == "DBA-12" and len(seen) == 1
    assert seen[0][1]["fields"]["summary"].startswith("[HIGH]")


def test_the_second_run_comments_on_the_ticket_instead_of_opening_another(monkeypatch):
    seen = fake_post(monkeypatch)
    f = review(rule("plan_change", severity="high"))
    ticket = {"key": "DBA-12", "id": "1", "url": "u"}
    worse = triage([{"state": "worse", "first_seen": sid(0), "first_seen_at": at(0), "tickets": {"jira": ticket}}], [f])
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, worse, "the review") == ["jira"]
    assert [u for u, _ in seen] == ["https://j.example.net/rest/api/2/issue/DBA-12/comment"]
    assert seen[0][1]["body"].startswith("worse: high · sql_id q1:") and "open since 2026-11-05" in seen[0][1]["body"] and "the review" in seen[0][1]["body"]
    seen.clear()
    # Plane comments by the item's id, as HTML
    plane_t = triage([{"state": "worse", "first_seen": sid(0), "tickets": {"plane": {"key": "OPS-7", "id": "uuid-7", "url": "u"}}}], [f])
    assert d.deliver({"channels": ["plane"], "plane": PLANE}, plane_t, "md") == ["plane"]
    assert seen[0][0] == "https://plane.example.com/api/v1/workspaces/acme/projects/pid/issues/uuid-7/comments/" and seen[0][1]["comment_html"].startswith("<p>worse:")
    seen.clear()
    # still open with a ticket: nothing is said (quiet unless worse)
    still = triage([{"state": "still_open", "first_seen": sid(0), "tickets": {"jira": ticket}}], [f])
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, still, "md") == [] and seen == []


def test_a_gone_finding_tells_its_ticket_it_cleared(monkeypatch):
    seen = fake_post(monkeypatch)
    gone = [{"kind": "plan_change", "subject": "q1", "title": "plan change on q1", "misses": 3, "first_seen_at": at(0),
             "tickets": {"jira": {"key": "DBA-12", "id": "1"}, "plane": {"key": "OPS-7", "id": "uuid-7"}}},
            {"kind": "deadlocks", "subject": "", "title": "deadlocks", "misses": 3, "tickets": {}}]  # never ticketed: nothing to tell
    t = triage([], [], gone=gone)
    assert d.deliver({"channels": ["jira", "plane", "slack"], "jira": JIRA, "plane": PLANE, "slack": {"webhook_url": "https://s"}}, t, "md") == ["jira", "plane"]
    assert [u for u, _ in seen] == ["https://j.example.net/rest/api/2/issue/DBA-12/comment",
                                    "https://plane.example.com/api/v1/workspaces/acme/projects/pid/issues/uuid-7/comments/"]
    assert seen[0][1]["body"].startswith("cleared: plan_change q1: plan change on q1\nnot seen in the last 3 runs")


def test_the_gate_is_on_the_finding_when_the_review_has_a_life(monkeypatch):
    seen = fake_post(monkeypatch)
    cfg = {"channels": ["slack"], "min_severity": "low", "slack": {"webhook_url": "https://s"}}
    medium = review(rule("plan_change", severity="medium"))
    high = review(rule("blocking", "", severity="high"))
    # a still-open medium does not repost, though the run's severity (medium) clears the gate
    assert d.deliver(cfg, triage([{"state": "still_open", "first_seen": sid(0), "tickets": {}}], [medium]), "md") == [] and seen == []
    # a quiet finding does not either
    assert d.deliver(cfg, triage([{"state": "quiet", "first_seen": sid(0), "tickets": {}}], [medium]), "md") == []
    # a new high beside it fires
    assert d.deliver(cfg, triage([{"state": "new", "first_seen": sid(1), "tickets": {}}, {"state": "still_open", "first_seen": sid(0), "tickets": {}}], [high, medium]), "md") == ["slack"]
    # worse fires; a new finding below the channel's gate does not
    assert d.deliver(cfg, triage([{"state": "worse", "first_seen": sid(0), "tickets": {}}], [medium]), "md") == ["slack"]
    assert d.deliver(dict(cfg, min_severity="high"), triage([{"state": "new", "first_seen": sid(1), "tickets": {}}], [medium]), "md") == []
    assert len(seen) == 2


def test_returned_fires_and_recurring_stays_quiet(monkeypatch):
    seen = fake_post(monkeypatch, {"/rest/api/2/issue": {"id": "1", "key": "DBA-1"}})
    f = review(rule("sql_top", "b", severity="high"))
    cfg = {"channels": ["jira"], "jira": JIRA}
    assert d.deliver(cfg, triage([{"state": "recurring", "first_seen": sid(0), "tickets": {}}], [f]), "md") == []  # the usual batch
    assert seen == []
    t = triage([{"state": "returned", "first_seen": sid(0), "tickets": {"jira": {"key": "DBA-1", "id": "1", "url": "u"}}}], [f])
    assert d.deliver(cfg, t, "md") == ["jira"]  # back after being cleared: its ticket hears about it, no new issue
    assert len(seen) == 1 and seen[0][0].endswith("/issue/DBA-1/comment") and seen[0][1]["body"].startswith("returned:")


def test_force_posts_regardless_of_the_gates(monkeypatch):
    seen = fake_post(monkeypatch, {"/rest/api/2/issue": {"id": "1", "key": "DBA-2"}})
    f = review(rule("sql_top", "b", severity="medium"))
    t = triage([{"state": "still_open", "first_seen": sid(0), "tickets": {"jira": {"key": "DBA-1", "id": "1", "url": "u"}}}], [f])
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, t, "md") == []  # a still-open medium: silent
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, t, "md", force=True) == ["jira"]  # a person asked: a fresh issue
    assert len(seen) == 1 and seen[0][0].endswith("/rest/api/2/issue")


def test_an_open_finding_outside_the_five_that_got_worse_still_fires(monkeypatch):
    seen = fake_post(monkeypatch, {"/rest/api/2/issue": {"id": "1", "key": "DBA-7"}})
    medium = review(rule("plan_change", severity="medium"))
    quiet_five = [{"state": "still_open", "first_seen": sid(0), "tickets": {}}]
    worse_outside = {"kind": "blocking", "subject": "", "title": "3 sessions blocked", "severity": "high", "state": "worse", "first_seen": sid(0), "tickets": {}}
    slack = {"channels": ["slack"], "min_severity": "low", "slack": {"webhook_url": "https://s"}}
    assert d.deliver(slack, triage(quiet_five, [medium], still_open=[dict(worse_outside, state="still_open")]), "md") == []
    assert d.deliver(slack, triage(quiet_five, [medium], still_open=[worse_outside]), "md") == ["slack"]
    # a ticket channel opens an issue for it and the ticket lands on the still-open entry, where record_tickets finds it
    t = triage(quiet_five, [medium], still_open=[dict(worse_outside)])
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, t, "md") == ["jira"]
    assert t["still_open"][0]["tickets"]["jira"]["key"] == "DBA-7"
    # next time it gets worse again, its ticket hears about it instead of a new issue
    n = len(seen)
    t2 = triage(quiet_five, [medium], still_open=[dict(worse_outside, tickets={"jira": {"key": "DBA-7", "id": "1", "url": "u"}})])
    assert d.deliver({"channels": ["jira"], "jira": JIRA}, t2, "md") == ["jira"]
    assert seen[n][0].endswith("/issue/DBA-7/comment") and "blocking" in seen[n][1]["body"]


def test_record_tickets_keeps_a_still_open_entry_ticket(tmp_path):
    st = LocalStorage(tmp_path)
    out = run(st, 0, [rule("sql_regressed", f"q{i}") for i in range(6)])
    result = {"engine": ENGINE, "target": TARGET, "snapshot": sid(0), "findings": [], "life": [], "still_open": out["still_open"]}
    result["still_open"][0]["tickets"] = {"jira": {"key": "DBA-9", "id": "9", "url": "u"}}
    ledger.record_tickets(st, result)
    by = {e["subject"]: e for e in ledger.load(st, ENGINE, TARGET)["entries"]}
    assert by["q5"]["tickets"]["jira"]["key"] == "DBA-9"


def test_legacy_findings_without_a_kind_are_not_an_identity(tmp_path):
    """A review stored before kinds existed: a rebuild must not fold its findings into one ("other", "") entry."""
    st = LocalStorage(tmp_path)
    legacy = [{"severity": "high", "where": "sql_id q1", "summary": "slow", "impact": "", "evidence": [], "fix": ""},
              {"severity": "medium", "where": "table t", "summary": "bloat", "impact": "", "evidence": [], "fix": ""}]
    rules = [rule("sql_regressed", "q1", "high")]
    st.put_json(f"{sid(0)}/manifest.json", {"captured_at": at(0)})
    st.put_json(f"{sid(0)}/compare.json", {"findings": rules})
    st.put_json(f"{sid(0)}/triage.json", {"findings": legacy, "source": "agent"})
    led = ledger.rebuild(st, ENGINE, TARGET)
    assert [(e["kind"], e["subject"]) for e in led["entries"]] == [("sql_regressed", "q1")]
    out = ledger.apply({"entries": [], "tombstones": [], "updated": None}, sid(1), at(1), rules, legacy)
    assert [l["state"] for l in out["life"]] == ["untracked", "untracked"]  # parallel to the findings, says nothing
    assert d.deliver({"channels": ["slack"], "slack": {"webhook_url": "https://s"}}, {"severity": "high", "findings": legacy, "life": out["life"], "still_open": []}, "md") == []


def test_legacy_reviews_keep_the_whole_run_gate_and_stdout_always_fires(monkeypatch, capsys):
    seen = fake_post(monkeypatch)
    cfg = {"channels": ["stdout", "slack", "pushgateway"], "min_severity": "low", "slack": {"webhook_url": "https://s"}, "pushgateway": {"url": "http://pg"}}
    puts = []
    monkeypatch.setitem(d.CHANNELS, "pushgateway", lambda c, t, md: puts.append(t["severity"]))
    medium = review(rule("plan_change", severity="medium"))
    # no life (an older store, a rules-only first run): the run's severity decides, as before
    assert d.deliver(cfg, triage(None, [medium]), "legacy") == ["stdout", "slack", "pushgateway"] and len(seen) == 1
    assert "legacy" in capsys.readouterr().out
    # with a life and nothing new: stdout still prints, the pushgateway still records the run, slack stays quiet
    assert d.deliver(cfg, triage([{"state": "still_open", "first_seen": sid(0), "tickets": {}}], [medium]), "quiet run") == ["stdout", "pushgateway"]
    assert "quiet run" in capsys.readouterr().out and len(seen) == 1 and puts == ["medium", "medium"]
    # the UI's Send test bypasses every gate, as it did
    assert d.test_channel(cfg, "slack", triage([{"state": "still_open", "first_seen": sid(0), "tickets": {}}], [medium]), "test") == (True, "sent via slack")


def test_record_tickets_persists_them_in_the_ledger_and_the_review(tmp_path):
    st = LocalStorage(tmp_path)
    rules = [rule("plan_change", severity="high"), rule("sql_regressed", "q2")]
    result = {"snapshot": sid(0), "engine": ENGINE, "target": TARGET, "findings": [review(f) for f in rules]}
    st.put_json(f"{sid(0)}/triage.json", result)
    result.update(ledger.update(st, ENGINE, TARGET, sid(0), at(0), rules, result["findings"]))
    ledger.record_tickets(st, result)  # no ticket made: nothing written
    assert "life" not in st.get_json(f"{sid(0)}/triage.json")
    result["life"][0]["tickets"] = {"jira": {"key": "DBA-1", "id": "1", "url": "u"}}
    ledger.record_tickets(st, result)
    led = ledger.load(st, ENGINE, TARGET)
    assert {e["subject"]: e["tickets"] for e in led["entries"]} == {"q1": {"jira": {"key": "DBA-1", "id": "1", "url": "u"}}, "q2": {}}
    assert st.get_json(f"{sid(0)}/triage.json")["life"][0]["tickets"]["jira"]["key"] == "DBA-1"
    # the next run's life carries the ticket, so delivery comments instead of opening another
    out = ledger.update(st, ENGINE, TARGET, sid(1), at(1), rules, result["findings"])
    assert out["life"][0]["tickets"]["jira"]["key"] == "DBA-1" and out["life"][1]["tickets"] == {}


# ---------------------------------------------------------------- the page and the markdown
def test_markdown_shows_the_life_and_the_still_open_list():
    from dbtriage.report import render_markdown

    f = review(rule("plan_change", severity="high"))
    t = triage([{"state": "worse", "first_seen": sid(0), "first_seen_at": at(0), "tickets": {"jira": {"key": "DBA-1", "url": "https://j/browse/DBA-1"}}}], [f],
               still_open=[{"severity": "medium", "kind": "sql_regressed", "subject": "q2", "title": "q2 slower", "state": "still_open", "first_seen_at": at(0), "misses": 2, "tickets": {}}],
               gone=[{"kind": "deadlocks", "subject": "", "title": "deadlocks", "first_seen_at": at(0), "misses": 3, "tickets": {"plane": {"key": "OPS-7"}}}])
    md = render_markdown(t)
    assert "1. 🔴 **High** · `sql_id q1`: plan_change on q1 — _worse · open since 2026-11-05 · ticket [DBA-1](https://j/browse/DBA-1)_" in md
    assert "**Still open** (1 not among the findings above):" in md
    assert "- 🟠 **Medium** · `sql_regressed q2`: q2 slower — _still open · open since 2026-11-05 · not seen in the last 2 runs_" in md
    assert "**Cleared:** `deadlocks` (gone · open since 2026-11-05 · ticket OPS-7)" in md
    new = render_markdown(triage([{"state": "new", "first_seen": sid(1), "first_seen_at": at(1), "tickets": {}}], [f]))
    assert "— _new_" in new and "open since" not in new
    assert "_new_" not in render_markdown(triage(None, [f])) and "Still open" not in render_markdown(triage(None, [f]))  # a legacy review


def test_snapshot_page_shows_the_life_and_renders_legacy_reviews_without_one(tmp_path):
    from dbtriage.web import App
    from tests.test_web import SID, config, store

    st = store(tmp_path)
    f = review(rule("plan_change", severity="high"))
    legacy = {"severity": "high", "title": "Bad query", "source": "rules", "findings": [f]}
    st.put_json(f"{SID}/triage.json", legacy)
    page = App(config(tmp_path), st, False, None).snapshot(SID).decode()
    assert "plan_change on q1" in page and "class='life'" not in page and "class='stillopen'" not in page
    st.put_json(f"{SID}/triage.json", dict(legacy, life=[{"state": "still_open", "first_seen": SID, "first_seen_at": "2026-11-04T10:00:00+00:00",
                                                             "tickets": {"jira": {"key": "DBA-1", "id": "1", "url": "https://j/browse/DBA-1"}, "plane": {"key": "OPS-7"}}}],
                                            still_open=[{"severity": "medium", "kind": "sql_regressed", "subject": "q2", "title": "q2 slower", "state": "still_open",
                                                         "first_seen_at": "2026-11-03T10:00:00+00:00", "misses": 1, "tickets": {}}],
                                            gone=[{"kind": "deadlocks", "subject": "", "title": "deadlocks", "misses": 3, "tickets": {}}]))
    page = App(config(tmp_path), st, False, None).snapshot(SID).decode()
    assert "<span class='life'><span class='state still_open'>still open</span><span>open since 2026-11-04</span>" in page
    assert "<a class='ticket' href='https://j/browse/DBA-1' title='jira ticket'>DBA-1</a>" in page and "<code class='ticket' title='plane ticket'>OPS-7</code>" in page
    assert "<h3 id='still-h'>Still open (1 not among the findings above)</h3>" in page and "<code class='where'>sql_regressed q2</code><span>q2 slower</span>" in page
    assert "not seen in the last 1 run</span>" in page and "<p class='cleared'>Cleared: <code class='where'>deadlocks</code>" in page
    assert "style=" not in page.split("<ol class='flist'>")[1].split("</section>")[0]  # themed by class, never inline
