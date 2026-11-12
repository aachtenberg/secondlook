"""The diagnosis block: five sentences from stored data (dbtriage.diagnosis), and the question the Ask button sends."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbtriage import diagnosis

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def labels(rows):
    return [r["label"] for r in rows]


def test_every_input_missing_still_gives_five_honest_cells():
    rows = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=None, changes=None, schedule=None, now=NOW, target="demo")
    assert labels(rows) == ["Health", "Open", "Changes", "Last 7 days", "Next"]
    assert rows[0]["head"] == "No review yet" and rows[1]["head"] == "Nothing tracked yet"
    assert rows[2]["head"] == "No change feed" and rows[2]["gap"] and rows[2]["link"] == ("Set one", "/targets/demo")
    assert rows[3]["head"] == "No history yet" and rows[4]["head"] == "No schedule" and rows[4]["gap"] and rows[4]["link"] == ("Add schedule", "/settings#schedule")
    assert diagnosis.gaps(rows) == 2
    assert diagnosis.synopsis("demo", rows) == "demo: no review yet. 2 setup gaps would make reviews sharper: no change feed and no schedule."


def test_health_cell_has_title_age_band_and_the_rules_fallback():
    tr = {"severity": "high", "title": "New heavy SQL q1 takes 99% of SQL time", "source": "rules", "agent_error": "RuntimeError: no key"}
    cmp_ = {"band": {"applies": True, "sentence": "total SQL time is above every same-hour window in 6 weeks"}}
    rows = diagnosis.lines(triage=tr, comparison=cmp_, manifest={"captured_at": iso(hours=2)}, ledger=None, changes=None, schedule=None, now=NOW)
    h = rows[0]
    assert h["cls"] == "high" and h["head"] == "New heavy SQL q1 takes 99% of SQL time"
    assert h["sub"] == "reviewed 2 h ago · the model failed, so this is the rules' review · total SQL time is above every same-hour window in 6 weeks"
    quiet = diagnosis.lines(triage={"severity": "info", "title": "no significant change", "source": "agent"}, comparison={"band": {"applies": False, "note": "window too long"}},
                            manifest={"captured_at": iso(minutes=3)}, ledger=None, changes=None, schedule=None, now=NOW)[0]
    assert quiet["head"] == "no significant change" and quiet["sub"] == "reviewed 3 min ago" and quiet["cls"] == "info"


def test_open_cell_count_worst_oldest_tickets_and_recurring():
    led = {"entries": [
        {"kind": "sql_regressed", "subject": "q1", "severity": "medium", "state": "still_open", "first_seen_at": iso(days=3), "tickets": {"jira": {"key": "DBA-12"}}},
        {"kind": "blocking", "subject": "", "severity": "high", "state": "worse", "first_seen_at": iso(hours=1), "tickets": {}},
        {"kind": "sql_top", "subject": "b", "severity": "low", "state": "recurring", "first_seen_at": iso(days=9), "tickets": {"plane": {"key": "OPS-3"}}},
    ], "tombstones": [{"kind": "deadlocks", "subject": "", "gone_at": iso(days=2)}, {"kind": "bloat", "subject": "t", "gone_at": iso(days=20)}]}
    rows = diagnosis.lines(triage={"severity": "high", "title": "x", "source": "agent", "agreement": []}, comparison={}, manifest={"captured_at": iso(minutes=1)},
                           ledger=led, changes=None, schedule=None, now=NOW, target="demo", latest_sid="postgres/demo/d/s9")
    o = rows[1]
    assert o["head"] == "3 findings, worst high" and o["head_sev"] == "high" and o["cls"] == "high"
    assert o["sub"] == "1 recurring · tickets DBA-12, OPS-3" and o["link"] == (f"oldest since {iso(days=9)[:10]}", "/s/postgres/demo/d/s9")
    assert rows[3]["head"] == "2 opened · 1 cleared" and rows[3]["sub"] == "net +1"  # the 9-day-old finding and the 20-day-old tombstone are outside the week
    empty = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger={"entries": [], "tombstones": []}, changes=None, schedule=None, now=NOW)
    assert empty[1]["head"] == "No open findings" and empty[1]["cls"] == "info"
    rebuilt = {"entries": [{"kind": "bloat", "subject": "t", "severity": "low", "state": "still_open", "first_seen": "postgres/demo/2026-11-01/x", "first_seen_at": None, "tickets": {}}], "tombstones": []}
    row = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=rebuilt, changes=None, schedule=None, now=NOW, target="demo", latest_sid="s")[1]
    assert row["link"] is None and "oldest since" not in row["sub"]  # a snapshot id is not a date
    down = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=None, changes=None, schedule=None, now=NOW, ledger_error="s3 down")
    assert down[1]["head"] == "Ledger unavailable" and down[1]["sub"] == "s3 down"
    assert diagnosis.suggestions(rows)[0] == "What is the open high finding and is it still true?"


def test_changes_cell_names_the_last_deploy_and_what_opened_after_it():
    feed = [{"time": iso(hours=5), "kind": "deploy", "description": "billing@1.4.1 to production"},
            {"time": iso(minutes=40), "kind": "deploy", "description": "billing@1.4.2 to production"},
            {"time": iso(minutes=10), "kind": "other", "description": "github: 401"}]
    led = {"entries": [{"kind": "sql_new", "subject": "q9", "severity": "high", "state": "new", "first_seen_at": iso(minutes=5), "tickets": {}}], "tombstones": []}
    rows = diagnosis.lines(triage={"severity": "high", "title": "x"}, comparison={"changes": [feed[1]]}, manifest={"captured_at": iso(minutes=1), "changes_sources": ["github"]},
                           ledger=led, changes=feed, schedule=None, now=NOW)
    c = rows[2]
    assert c["head"] == "deploy billing@1.4.2 to production" and c["sub"] == "40 min ago, inside or just before the last window · 1 finding opened after it" and c["cls"] == "medium" and not c["gap"]
    assert "Did the deploy billing@1.4.2 to production cause anything that is open now?" in diagnosis.suggestions(rows)
    none = diagnosis.lines(triage=None, comparison=None, manifest={"changes_sources": ["github"]}, ledger=None, changes=[], schedule=None, now=NOW)
    assert none[2]["head"] == "No change in the window" and not none[2]["gap"]
    unknown = diagnosis.lines(triage=None, comparison=None, manifest={"changes_sources": ["github"]}, ledger=None, changes=feed, schedule=None, now=NOW)
    assert unknown[2]["sub"] == "40 min ago"  # no ledger: nothing is claimed about what opened after it


def test_week_cell_counts_what_the_model_dropped():
    tr = {"severity": "medium", "title": "x", "source": "agent", "agreement": [{"mark": "rules_only", "severity": "high", "kind": "blocking", "subject": "", "model": None, "rules": 0},
                                                                                 {"mark": "rules_only", "severity": "low", "kind": "bloat", "subject": "t", "model": None, "rules": 1}]}
    rows = diagnosis.lines(triage=tr, comparison={}, manifest={"captured_at": iso(minutes=1)}, ledger={"entries": [], "tombstones": []}, changes=None, schedule=None, now=NOW)
    assert rows[3]["head"] == "0 opened · 0 cleared" and rows[3]["sub"] == "net +0 · the model left 1 rules finding out of the last review" and rows[3]["cls"] == "medium"


def test_next_cell_paused_next_and_failed():
    nxt = NOW + timedelta(hours=1)
    base = dict(triage=None, comparison=None, manifest=None, ledger=None, changes=None, now=NOW)
    paused = diagnosis.lines(schedule={"schedule": "0 * * * *", "label": "hourly", "paused": True}, **base)[4]
    assert paused["head"] == "Schedule paused" and paused["gap"] and paused["link"] == ("Resume", "/settings#schedule")
    ok = diagnosis.lines(schedule={"schedule": "0 * * * *", "label": "hourly", "paused": False, "next": nxt, "last_status": "ok"}, **base)[4]
    assert ok["head"] == f"Next run {nxt:%m-%d %H:%M}" and ok["sub"] == "hourly" and ok["cls"] == "" and not ok["gap"]
    bad = diagnosis.lines(schedule={"schedule": "0 * * * *", "label": "hourly", "paused": False, "next": nxt, "last_status": "failed: pg-lab not reachable"}, **base)[4]
    assert bad["sub"].endswith("the last scheduled run did not finish: failed: pg-lab not reachable") and bad["cls"] == "high"
    assert "Suggest a snapshot schedule for this workload" in diagnosis.suggestions(diagnosis.lines(schedule=None, **base))


def test_suggestions_name_a_trend_spike():
    rows = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=None, changes=None, schedule={"schedule": "x", "label": "hourly", "paused": False, "next": NOW}, now=NOW)
    pts = [{"value": 10, "t": NOW - timedelta(hours=3)}, {"value": 900, "t": NOW - timedelta(hours=2)}, {"value": 12, "captured_at": iso(hours=1)}]
    assert diagnosis.suggestions(rows, pts, "aborted_connects/s") == [f"Why did aborted_connects spike at {iso(hours=2)[11:16]}?"]
    assert diagnosis.suggestions(rows, [], None) == ["What deserves attention first on this database, and why?"]


def test_the_question_carries_the_facts_and_fits_the_chat_limit():
    rows = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=None, changes=None, schedule=None, now=NOW)
    q = diagnosis.question("pg-prod", rows)
    assert q.startswith("Summarise the current state of pg-prod") and "- Health: No review yet" in q and "do not invent numbers" in q
    long_rows = [{"label": "Open", "text": "x" * 3000, "cls": ""}]
    assert len(diagnosis.question("pg-prod", long_rows)) <= diagnosis.MAX_QUESTION
