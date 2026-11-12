"""One review for the fleet: the digest across targets, its two cross-target links, its delivery and its schedule."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from dbtriage import deliver as d
from dbtriage import fleet, ledger
from dbtriage import scheduler as sch
from dbtriage.config import Config, Target
from dbtriage.sqlnorm import text_hash
from dbtriage.storage import LocalStorage, StorageUnavailable

UNTIL = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)
SINCE = UNTIL - timedelta(days=7)
BILLING = "billing@1.4.2 to production"
PG_TEXT = "SELECT * FROM orders WHERE customer_id = $1 AND status = $2"
ORA_TEXT = "SELECT * FROM orders WHERE customer_id = :1 AND status = :2"  # the same shape; must never join across engines


def sid(engine: str, target: str, when: datetime) -> str:
    return f"{engine}/{target}/{when:%Y-%m-%d}/{when:%Y%m%dT%H%M%S}Z"


def snapshot(st, engine: str, target: str, when: datetime, top_sql: list[dict], deploys: list[tuple[str, datetime]] = ()):
    s = sid(engine, target, when)
    st.put_json(f"{s}/manifest.json", {"id": s, "engine": engine, "target": target, "captured_at": when.isoformat()})
    st.put_json(f"{s}/summary.json", {"captured_at": when.isoformat(), "top_sql": top_sql})
    if deploys:
        st.put_json(f"{s}/changes.json", [{"time": t.isoformat(), "kind": "deploy", "description": text, "link": None, "source": "file"} for text, t in deploys])
    return s


def entry(kind: str, subject: str, severity: str, first_seen_at: datetime, state: str = "new", title: str = "", tickets: dict | None = None) -> dict:
    return {"kind": kind, "subject": subject, "title": title or f"{kind} on {subject}", "severity": severity, "peak": severity, "state": state,
            "first_seen": "x", "first_seen_at": first_seen_at.isoformat(), "last_seen": "x", "last_seen_at": (first_seen_at + timedelta(hours=1)).isoformat(),
            "misses": 0, "returns": [], "tickets": tickets or {}}


def write_ledger(st, engine: str, target: str, entries: list[dict]):
    st.put_json(ledger.key(engine, target), {"entries": entries, "tombstones": [], "updated": "x", "prior": None, "generation": 1})


def fleet_store(tmp_path):
    """ora-prod and pg-prod took the billing deploy and opened findings after it; pg-replica runs the same statement as
    pg-prod (a different queryid, the same text) and is otherwise quiet; mysql-quiet was reviewed and opened nothing;
    ora-prod's statement has the same shape as pg-prod's but another engine."""
    st = LocalStorage(tmp_path / "data")
    deploy_at = UNTIL - timedelta(days=2, hours=3)
    pg_row = {"sql_id": "111", "text": PG_TEXT, "text_hash": text_hash(PG_TEXT), "calls": 10, "total_time_ms": 100.0}
    pg_rep_row = {"sql_id": "999", "text": PG_TEXT, "calls": 10, "total_time_ms": 100.0}  # stored before the hash existed: computed on read
    ora_row = {"sql_id": "abc", "text": ORA_TEXT, "text_hash": text_hash(ORA_TEXT), "calls": 10, "total_time_ms": 100.0}
    for day in (6, 3, 1):
        when = UNTIL - timedelta(days=day)
        snapshot(st, "postgres", "pg-prod", when, [pg_row], [(BILLING, deploy_at)] if day <= 3 else ())
        snapshot(st, "oracle", "ora-prod", when, [ora_row], [(BILLING, deploy_at)] if day <= 3 else ())
        snapshot(st, "postgres", "pg-replica", when, [pg_rep_row])
        snapshot(st, "mysql", "mysql-quiet", when, [])
    snapshot(st, "postgres", "pg-prod", UNTIL - timedelta(days=20), [pg_row])  # outside the window: not a reviewed day
    write_ledger(st, "postgres", "pg-prod", [
        entry("sql_regressed", "111", "high", deploy_at + timedelta(hours=1), tickets={"jira": {"key": "DBA-7", "id": "1", "url": "u"}}),
        entry("wait_shift", "io", "low", UNTIL - timedelta(days=30), state="still_open"),  # old: not in the window
    ])
    write_ledger(st, "oracle", "ora-prod", [entry("plan_change", "abc", "medium", deploy_at + timedelta(hours=2))])
    write_ledger(st, "postgres", "pg-replica", [entry("sql_top", "999", "medium", UNTIL - timedelta(days=20), state="still_open")])
    write_ledger(st, "mysql", "mysql-quiet", [])
    cfg = Config(storage_url=f"file://{tmp_path / 'data'}", base_dir=str(tmp_path), targets={
        "pg-prod": Target("pg-prod", "postgres", "d"), "ora-prod": Target("ora-prod", "oracle", "d"),
        "pg-replica": Target("pg-replica", "postgres", "d"), "mysql-quiet": Target("mysql-quiet", "mysql", "d"),
        "never-run": Target("never-run", "sqlserver", "d")}, delivery={"channels": ["stdout"]})
    return st, cfg


# ---------------------------------------------------------------- the digest
def test_opened_findings_are_the_ones_first_seen_in_the_window(tmp_path):
    st, cfg = fleet_store(tmp_path)
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert [f["subject"] for f in dg["targets"]["pg-prod"]["opened"]] == ["111"]  # the 30-day-old wait_shift is not news
    f = dg["targets"]["pg-prod"]["opened"][0]
    assert (f["severity"], f["state"], f["tickets"]) == ("high", "new", {"jira": "DBA-7"})
    assert dg["severity"] == "high" and dg["targets"]["pg-prod"]["snapshots"] == 3 and dg["targets"]["pg-prod"]["deploys"] == [BILLING]


def test_the_same_deploy_links_findings_across_engines(tmp_path):
    st, cfg = fleet_store(tmp_path)
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert len(dg["deploys"]) == 1
    link = dg["deploys"][0]
    assert link["deploy"] == BILLING and link["targets"] == ["ora-prod", "pg-prod"] and link["quiet"] == []


def test_a_deploy_on_one_target_or_before_the_findings_is_no_link(tmp_path):
    st, cfg = fleet_store(tmp_path)
    # the deploy also reached pg-replica, which opened nothing: it is named as quiet, not as linked
    when = UNTIL - timedelta(days=1, hours=1)
    snapshot(st, "postgres", "pg-replica", when, [], [(BILLING, UNTIL - timedelta(days=2, hours=3))])
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert dg["deploys"][0]["quiet"] == ["pg-replica"]
    # a finding opened before the deploy cannot be after it
    write_ledger(st, "oracle", "ora-prod", [entry("plan_change", "abc", "medium", UNTIL - timedelta(days=5))])
    assert fleet.digest(cfg, st, SINCE, UNTIL)["deploys"] == []


def test_the_same_statement_links_within_one_engine_and_never_across(tmp_path):
    st, cfg = fleet_store(tmp_path)
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert len(dg["statements"]) == 1
    s = dg["statements"][0]
    assert s["engine"] == "postgres" and s["text_hash"] == text_hash(PG_TEXT)
    assert [(f["target"], f["subject"], f["severity"]) for f in s["findings"]] == [("pg-prod", "111", "high"), ("pg-replica", "999", "medium")]
    # ora-prod's statement normalizes to the same text, and is not joined: another engine
    assert text_hash(ORA_TEXT) == text_hash(PG_TEXT) and all(f["target"] != "ora-prod" for f in s["findings"])


def test_quiet_and_unreviewed_are_told_apart(tmp_path):
    st, cfg = fleet_store(tmp_path)
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert dg["quiet"] == ["mysql-quiet", "pg-replica"]  # reviewed, nothing new or worse (pg-replica's finding is old)
    assert dg["unreviewed"] == ["never-run"]


def test_a_finding_that_got_worse_counts_as_significant(tmp_path):
    st, cfg = fleet_store(tmp_path)
    old = entry("sql_top", "999", "high", UNTIL - timedelta(days=20), state="worse")
    old["last_seen_at"] = (UNTIL - timedelta(days=1)).isoformat()
    write_ledger(st, "postgres", "pg-replica", [old])
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert "pg-replica" not in dg["quiet"] and dg["targets"]["pg-replica"]["opened"][0]["state"] == "worse"
    assert "(worse since 2026-10-22)" in fleet.render(dg)


def test_the_markdown_says_the_two_sentences(tmp_path):
    st, cfg = fleet_store(tmp_path)
    md = fleet.render(fleet.digest(cfg, st, SINCE, UNTIL))
    assert md.startswith("# Fleet digest 2026-11-04 to 2026-11-11\n")
    assert f"- After `{BILLING}`, findings opened on ora-prod and pg-prod." in md
    assert "- The same statement is high on pg-prod and medium on pg-replica (postgres, text hash `" in md
    assert "## pg-prod (postgres)\n\n- **high** sql_regressed on 111 (new since 2026-11-09, ticket DBA-7)" in md
    assert "No significant change: mysql-quiet and pg-replica." in md and "No snapshot in the window: never-run." in md
    assert "## mysql-quiet" not in md  # a quiet target is one name in one line, not a section
    assert len(md.splitlines()) < 40


def test_the_same_severity_reads_as_one_word(tmp_path):
    st, cfg = fleet_store(tmp_path)
    write_ledger(st, "postgres", "pg-replica", [entry("sql_top", "999", "high", UNTIL - timedelta(days=20), state="still_open")])
    assert "- The same statement is high on pg-prod and pg-replica (postgres" in fleet.render(fleet.digest(cfg, st, SINCE, UNTIL))


def test_a_sql_server_id_links_through_the_ledger(tmp_path):
    """The ledger stores subjects lowercased (findings.identity); SQL Server's ids are uppercase hex. The two
    replicas must still meet on the statement."""
    st, cfg = fleet_store(tmp_path)
    cfg.targets = {n: Target(n, "sqlserver", "d") for n in ("mssql-a", "mssql-b")}
    text = "SELECT * FROM dbo.orders WHERE customer_id = @p1"
    for n, sql_id in (("mssql-a", "0xB858F14FAB5DAA6A"), ("mssql-b", "0x1F2E3D4C5B6A7988")):
        when = UNTIL - timedelta(days=1)
        s = snapshot(st, "sqlserver", n, when, [{"sql_id": sql_id, "text": text, "text_hash": text_hash(text)}])
        led = ledger.load(st, "sqlserver", n)
        ledger.apply(led, s, when.isoformat(), [{"kind": "sql_regressed", "severity": "high", "title": "slower", "evidence": {"sql_id": sql_id}}])
        ledger.save(st, "sqlserver", n, led)
        assert ledger.load(st, "sqlserver", n)["entries"][0]["subject"] == sql_id.lower()
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert len(dg["statements"]) == 1 and [f["target"] for f in dg["statements"][0]["findings"]] == ["mssql-a", "mssql-b"]


def test_a_target_that_got_worse_after_the_deploy_is_not_where_nothing_opened(tmp_path):
    st, cfg = fleet_store(tmp_path)
    deploy_at = UNTIL - timedelta(days=2, hours=3)
    snapshot(st, "postgres", "pg-replica", UNTIL - timedelta(days=1, hours=1), [], [(BILLING, deploy_at)])
    old = entry("sql_top", "999", "high", UNTIL - timedelta(days=20), state="worse")
    old["last_seen_at"] = (deploy_at + timedelta(hours=4)).isoformat()  # worse on a run after the deploy
    write_ledger(st, "postgres", "pg-replica", [old])
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    link = dg["deploys"][0]
    assert link["targets"] == ["ora-prod", "pg-prod", "pg-replica"] and link["quiet"] == []
    md = fleet.render(dg)
    assert f"- After `{BILLING}`, findings opened on ora-prod, pg-prod and pg-replica." in md and "where nothing opened" not in md
    # worse on a run before the deploy: not after it, and still not "nothing opened"
    old["last_seen_at"] = (deploy_at - timedelta(hours=4)).isoformat()
    write_ledger(st, "postgres", "pg-replica", [old])
    link = fleet.digest(cfg, st, SINCE, UNTIL)["deploys"][0]
    assert link["targets"] == ["ora-prod", "pg-prod"] and link["quiet"] == []


class _Blinking(LocalStorage):
    """A store that cannot answer for one key."""

    def __init__(self, root, down_key: str):
        super().__init__(root)
        self.down_key = down_key

    def get(self, key):
        if key == self.down_key:
            raise StorageUnavailable("connection reset")
        return super().get(key)


def test_a_store_blip_lists_the_target_as_not_read_and_still_counts_it(tmp_path):
    st, cfg = fleet_store(tmp_path)
    # the blip hits pg-replica's summary read: after its ledger loaded, inside the hash lookup
    st = _Blinking(tmp_path / "data", f"{sid('postgres', 'pg-replica', UNTIL - timedelta(days=1))}/summary.json")
    dg = fleet.digest(cfg, st, SINCE, UNTIL)
    assert dg["targets"]["pg-replica"]["error"] == "connection reset" and dg["targets"]["pg-replica"]["opened"] == []
    assert "pg-replica" not in dg["quiet"] and "pg-replica" not in dg["unreviewed"] and dg["statements"] == []
    md = fleet.render(dg)
    assert "2 findings opened on 2 of 5 databases; 1 had no significant change." in md  # the fleet is still five
    assert md.rstrip().endswith("Could not be read:\n- pg-replica: connection reset")


def test_parse_since():
    assert fleet.parse_since("7d", UNTIL) == SINCE and fleet.parse_since("24h", UNTIL) == UNTIL - timedelta(hours=24)
    assert fleet.parse_since("90m", UNTIL) == UNTIL - timedelta(minutes=90)
    assert fleet.parse_since("2026-11-01T00:00:00", UNTIL) == datetime(2026, 11, 1, tzinfo=timezone.utc)
    try:
        fleet.parse_since("yesterday", UNTIL)
    except ValueError as exc:
        assert "7d, 24h" in str(exc)
    else:
        raise AssertionError("accepted 'yesterday'")


# ---------------------------------------------------------------- the command and the delivery
def test_cli_digest_prints_the_note(tmp_path, capsys, monkeypatch):
    from dbtriage import cli

    st, cfg = fleet_store(tmp_path)
    (tmp_path / "targets.toml").write_text(f'[storage]\nurl = "file://{tmp_path / "data"}"\n' + "".join(
        f'[targets.{n}]\nengine = "{t.engine}"\ndsn = "d"\n' for n, t in cfg.targets.items()))
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    monkeypatch.setattr(fleet, "parse_since", lambda text, now: SINCE if text == "7d" else fleet.parse_since(text, now))
    monkeypatch.setattr(cli, "datetime", _Clock)
    cli.main(["--config", str(tmp_path / "targets.toml"), "digest"])
    out = capsys.readouterr().out
    assert out.startswith("# Fleet digest 2026-11-04 to 2026-11-11") and f"After `{BILLING}`" in out


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return UNTIL


def test_delivery_wraps_the_note_as_a_triage(tmp_path, monkeypatch):
    st, cfg = fleet_store(tmp_path)
    sent = []
    monkeypatch.setitem(d.CHANNELS, "fake", lambda c, t, md: sent.append((t, md)))
    cfg.delivery = {"channels": ["fake"], "min_severity": "medium", "fake": {}}
    dg, md, done = fleet.send(cfg, st, SINCE, UNTIL)
    assert done == ["fake"] and len(sent) == 1
    t, text = sent[0]
    assert (t["severity"], t["target"], t["engine"], t["findings"]) == ("high", "fleet", "fleet", []) and "life" not in t
    assert t["title"] == "Fleet digest 2026-11-04 to 2026-11-11" and d._subject(t) == "[HIGH] db triage fleet: Fleet digest 2026-11-04 to 2026-11-11"
    assert text == md
    # the whole-run gate applies: a quiet week does not reach a channel gated at medium
    for n in cfg.targets:
        write_ledger(st, cfg.targets[n].engine, n, [])
    assert fleet.send(cfg, st, SINCE, UNTIL)[2] == [] and len(sent) == 1
    # the digest's own channels win over the delivery channels
    cfg.delivery["digest"] = {"schedule": "weekly", "channels": ["fake"]}
    cfg.delivery["channels"] = ["stdout"]
    assert fleet.schedule_of(cfg.delivery) == ("0 3 * * 1", ["fake"])
    assert fleet.schedule_of({"digest": {"schedule": "daily"}}) == ("0 3 * * *", None) and fleet.schedule_of({}) == ("", None)
    assert fleet.schedule_of({"digest": {"schedule": "every 7m"}}) == ("", None)  # not a schedule: off, not a crash


def test_cli_digest_deliver_uses_the_digest_channels(tmp_path, capsys, monkeypatch):
    from dbtriage import cli

    st, cfg = fleet_store(tmp_path)
    sent = []
    monkeypatch.setitem(d.CHANNELS, "fake", lambda c, t, md: sent.append(t["title"]))
    (tmp_path / "targets.toml").write_text(f'[storage]\nurl = "file://{tmp_path / "data"}"\n[delivery]\nchannels = ["stdout"]\n'
                                           f'[delivery.digest]\nschedule = "weekly"\nchannels = ["fake"]\n' + "".join(
        f'[targets.{n}]\nengine = "{t.engine}"\ndsn = "d"\n' for n, t in cfg.targets.items()))
    monkeypatch.setenv("DBTRIAGE_NO_OVERLAY", "1")
    monkeypatch.setattr(cli, "datetime", _Clock)
    cli.main(["--config", str(tmp_path / "targets.toml"), "digest", "--since", "7d", "--deliver"])
    out, err = capsys.readouterr()
    assert sent == ["Fleet digest 2026-11-04 to 2026-11-11"] and "delivered to: fake" in err and out.startswith("# Fleet digest")
    assert not st.exists(fleet.DIGEST_KEY)  # a hand-run digest does not move the schedule's clock


# ---------------------------------------------------------------- the schedule
def test_scheduler_fires_the_digest_once_per_slot_and_remembers_across_restarts(tmp_path, monkeypatch):
    st, cfg = fleet_store(tmp_path)
    sent = []
    monkeypatch.setitem(d.CHANNELS, "fake", lambda c, t, md: sent.append((t["since"], t["until"])))
    cfg.delivery = {"channels": ["stdout"], "digest": {"schedule": "weekly", "channels": ["fake"]}}
    dig = fleet.Digest(lambda: cfg, st)
    s = sch.Scheduler(lambda: {}, lambda *a: (True, "started"), digest=dig)
    t0 = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)  # a Sunday noon; the slot is Monday 03:00
    s.tick(t0)
    assert sent == [] and s.digest_state.next == datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
    s.tick(t0 + timedelta(hours=10))
    assert sent == []
    s.tick(datetime(2026, 11, 9, 3, 0, 30, tzinfo=timezone.utc))
    assert len(sent) == 1 and s.digest_state.last_status == "sent"
    since, until = sent[0]
    assert since == datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc).isoformat()  # the first digest covers one period back
    assert until == datetime(2026, 11, 9, 3, 0, 30, tzinfo=timezone.utc).isoformat()
    assert s.digest_state.next == datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
    s.tick(datetime(2026, 11, 9, 3, 1, tzinfo=timezone.utc))  # the next tick: not again
    s.tick(datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc))
    assert len(sent) == 1
    kept = json.loads(st.get(fleet.DIGEST_KEY))
    assert kept["sent_at"] == until and kept["delivered"] == ["fake"] and kept["severity"] == "info"  # the findings opened after 03:00
    # a restart in the same week: the stored send sets the next slot, nothing is resent
    s2 = sch.Scheduler(lambda: {}, lambda *a: (True, "started"), digest=fleet.Digest(lambda: cfg, st))
    s2.tick(datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))
    assert len(sent) == 1 and s2.digest_state.next == datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
    # a restart after a slot that passed while it was down: sent once, since the last digest
    s3 = sch.Scheduler(lambda: {}, lambda *a: (True, "started"), digest=fleet.Digest(lambda: cfg, st))
    s3.tick(datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc))
    assert len(sent) == 2 and sent[1][0] == until and s3.digest_state.next == datetime(2026, 11, 23, 3, 0, tzinfo=timezone.utc)
    # two processes on one store: the second finds the slot already sent
    s4 = sch.Scheduler(lambda: {}, lambda *a: (True, "started"), digest=fleet.Digest(lambda: cfg, st))
    s4.digest_state = sch.State(expr="0 3 * * 1", next=datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc))
    s4.tick(datetime(2026, 11, 17, 9, 1, tzinfo=timezone.utc))
    assert len(sent) == 2 and s4.digest_state.last_status.startswith("skipped: already sent at 2026-11-17 09:00")
    # no digest configured: nothing is scheduled; a bad schedule is off, not a crash
    cfg.delivery.pop("digest")
    s.tick(datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc))
    assert len(sent) == 2 and s.digest_state.next is None
    s5 = sch.Scheduler(lambda: {}, lambda *a: (True, "started"))
    s5.tick(datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc))  # no digest at all: the old call shape still works


SLOT = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
NOW = SLOT + timedelta(seconds=30)


def _digest_cfg(cfg, channels):
    cfg.delivery = {"channels": ["stdout"], "digest": {"schedule": "weekly", "channels": channels}}
    return cfg


class _Racing(LocalStorage):
    """A store whose first conditional write lets another process go first."""

    def __init__(self, root):
        super().__init__(root)
        self.before_first_put_if = None

    def put_if(self, key, data, expected):
        if self.before_first_put_if is not None:
            other, self.before_first_put_if = self.before_first_put_if, None
            other()
        super().put_if(key, data, expected)


def test_two_processes_on_one_store_send_one_digest(tmp_path, monkeypatch):
    """Both see no send for the slot; the claim is a conditional write, so the one the store refuses stands down."""
    st, cfg = fleet_store(tmp_path)
    sent = []
    monkeypatch.setitem(d.CHANNELS, "fake", lambda c, t, md: sent.append(t["title"]))
    _digest_cfg(cfg, ["fake"])
    racing = _Racing(tmp_path / "data")
    a, b = fleet.Digest(lambda: cfg, racing), fleet.Digest(lambda: cfg, LocalStorage(tmp_path / "data"))
    results = {}
    racing.before_first_put_if = lambda: results.__setitem__("b", b.fire(SLOT, NOW, "schedule"))  # b lands between a's read and a's claim
    results["a"] = a.fire(SLOT, NOW, "schedule")
    assert results["b"] == (True, "sent") and results["a"] == (False, "another process sent it")
    assert len(sent) == 1
    kept = json.loads(st.get(fleet.DIGEST_KEY))
    assert kept["sent_at"] == NOW.isoformat() and "claimed_at" not in kept  # the claim is gone with the send recorded
    # a claim under way (a process still sending) holds the slot; an abandoned one expires
    st.put_json(fleet.DIGEST_KEY, {**kept, "sent_at": (SLOT - timedelta(days=7)).isoformat(), "claimed_at": NOW.isoformat(), "claimed_slot": SLOT.isoformat()})
    assert b.fire(SLOT, NOW + timedelta(minutes=1), "schedule") == (False, "another process is sending it (claimed at 2026-11-09 03:00)")
    assert len(sent) == 1
    assert b.fire(SLOT, NOW + fleet.CLAIM_TTL, "schedule") == (True, "sent") and len(sent) == 2


def test_a_failed_send_leaves_the_clock_alone(tmp_path, monkeypatch):
    st, cfg = fleet_store(tmp_path)

    def broken(c, t, md):
        raise ConnectionError("smtp down")

    monkeypatch.setitem(d.CHANNELS, "fake", broken)
    _digest_cfg(cfg, ["fake"])
    dig = fleet.Digest(lambda: cfg, st)
    assert dig.fire(SLOT, NOW, "schedule") == (False, "no channel took it (fake failed); not sent")
    assert fleet.last_sent(st) is None and "claimed_at" not in json.loads(st.get(fleet.DIGEST_KEY))  # the claim was given back
    # once a channel is back the same slot goes out, covering the whole period
    sent = []
    monkeypatch.setitem(d.CHANNELS, "fake", lambda c, t, md: sent.append(t["since"]))
    assert dig.fire(SLOT, NOW, "schedule") == (True, "sent") and sent == [(SLOT - timedelta(days=7)).isoformat()]
    assert fleet.last_sent(st) == NOW
    # a week the severity gate held back still advances the clock: nothing failed, there was nothing to say
    for n in cfg.targets:
        write_ledger(st, cfg.targets[n].engine, n, [])
    cfg.delivery["fake"] = {"min_severity": "medium"}
    later = NOW + timedelta(days=7)
    assert dig.fire(SLOT + timedelta(days=7), later, "schedule") == (True, "sent") and len(sent) == 1
    assert fleet.last_sent(st) == later


class _Down(LocalStorage):
    def get(self, key):
        raise StorageUnavailable("store down")

    def version_of(self, key):
        raise StorageUnavailable("store down")


def test_a_store_outage_sends_nothing(tmp_path, monkeypatch):
    """No record is not "never sent": it may be "sent last week, and the store is down"."""
    st, cfg = fleet_store(tmp_path)
    sent = []
    monkeypatch.setitem(d.CHANNELS, "fake", lambda c, t, md: sent.append(t["title"]))
    _digest_cfg(cfg, ["fake"])
    down = _Down(tmp_path / "data")
    dig = fleet.Digest(lambda: cfg, down)
    try:
        dig.last_sent()
    except StorageUnavailable:
        pass
    else:
        raise AssertionError("an outage read as 'never sent'")
    assert dig.fire(SLOT, NOW, "schedule") == (False, "store unavailable (store down); not sent") and sent == []
    assert not st.exists(fleet.DIGEST_KEY)
    # the scheduler waits for the store before it counts the next slot, then catches up from the stored send
    s = sch.Scheduler(lambda: {}, lambda *a: (True, "started"), digest=dig)
    s.tick(NOW)
    assert sent == [] and s.digest_state.next is None and s.digest_state.last_status.startswith("waiting: store unavailable")
    st.put_json(fleet.DIGEST_KEY, {"sent_at": (SLOT - timedelta(days=7)).isoformat()})
    s.digest = fleet.Digest(lambda: cfg, st)
    s.tick(NOW + timedelta(minutes=1))
    assert sent == ["Fleet digest 2026-11-02 to 2026-11-09"] and s.digest_state.last_status == "sent"


# ---------------------------------------------------------------- the hash is stored with every capture
def test_collect_stores_the_text_hash_next_to_sql_id(monkeypatch, tmp_path):
    from dbtriage import cli
    from dbtriage.collectors.base import Snapshot
    from dbtriage.triggers import TriageRequest

    class Collector:
        def collect(self, window):
            return Snapshot(summary={"captured_at": UNTIL.isoformat(), "metrics_kind": "interval",
                                     "top_sql": [{"sql_id": "111", "text": "SELECT * FROM t WHERE id = ?"}, {"sql_id": "222", "text": None}],
                                     "sessions": [{"state": "active", "text": "select 1"}]}, raw={}, reports={})

    monkeypatch.setattr(cli, "get_collector", lambda t: Collector())
    st = LocalStorage(tmp_path / "s")
    cfg = Config(storage_url="", targets={"pg": Target("pg", "postgres", "d")}, base_dir=str(tmp_path))
    s = cli.collect(cfg, st, TriageRequest(target="pg", mode="scheduled", window_minutes=60, end=UNTIL, source="test"))
    summary = st.get_json(f"{s}/summary.json")
    assert summary["top_sql"][0]["text_hash"] == text_hash("select * from t where id = $1") and "text_hash" not in summary["top_sql"][1]
    assert summary["sessions"][0]["text_hash"] == text_hash("select 1")
