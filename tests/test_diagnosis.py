"""The diagnosis block: five sentences from stored data (dbtriage.diagnosis), and the question the Ask button sends."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbtriage import diagnosis

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def labels(rows):
    return [r["label"] for r in rows]


def test_every_input_missing_still_gives_five_honest_lines():
    rows = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=None, changes=None, schedule=None, now=NOW)
    assert labels(rows) == ["Health", "Open", "Changed", "Week", "Next"]
    assert rows[0]["text"].startswith("No review yet") and rows[1]["text"].startswith("No findings tracked yet")
    assert "No change feed configured" in rows[2]["text"] and rows[3]["text"] == "No history yet." and rows[4]["text"].startswith("No schedule")


def test_health_line_has_severity_age_band_and_the_rules_fallback():
    tr = {"severity": "high", "title": "New heavy SQL q1 takes 99% of SQL time", "source": "rules", "agent_error": "RuntimeError: no key"}
    cmp_ = {"band": {"applies": True, "sentence": "total SQL time is above every same-hour window in 6 weeks"}}
    rows = diagnosis.lines(triage=tr, comparison=cmp_, manifest={"captured_at": iso(hours=2)}, ledger=None, changes=None, schedule=None, now=NOW)
    h = rows[0]
    assert h["cls"] == "high" and h["text"].startswith("High: New heavy SQL q1 takes 99% of SQL time, reviewed 2 h ago")
    assert "The model failed, so this is the rules' review" in h["text"] and "Total SQL time is above every same-hour window in 6 weeks." in h["text"]
    quiet = diagnosis.lines(triage={"severity": "info", "title": "no significant change", "source": "agent"}, comparison={"band": {"applies": False, "note": "window too long"}},
                            manifest={"captured_at": iso(minutes=3)}, ledger=None, changes=None, schedule=None, now=NOW)[0]
    assert quiet["text"] == "Info: no significant change, reviewed 3 min ago." and quiet["cls"] == "info"


def test_open_findings_count_worst_oldest_tickets_and_recurring():
    led = {"entries": [
        {"kind": "sql_regressed", "subject": "q1", "severity": "medium", "state": "still_open", "first_seen_at": iso(days=3), "tickets": {"jira": {"key": "DBA-12"}}},
        {"kind": "blocking", "subject": "", "severity": "high", "state": "worse", "first_seen_at": iso(hours=1), "tickets": {}},
        {"kind": "sql_top", "subject": "b", "severity": "low", "state": "recurring", "first_seen_at": iso(days=9), "tickets": {"plane": {"key": "OPS-3"}}},
    ], "tombstones": [{"kind": "deadlocks", "subject": "", "gone_at": iso(days=2)}, {"kind": "bloat", "subject": "t", "gone_at": iso(days=20)}]}
    rows = diagnosis.lines(triage={"severity": "high", "title": "x", "source": "agent", "agreement": []}, comparison={}, manifest={"captured_at": iso(minutes=1)},
                           ledger=led, changes=None, schedule=None, now=NOW)
    assert rows[1]["text"] == f"3 open findings, worst high, oldest since {iso(days=9)[:10]}, 1 recurring; tickets DBA-12, OPS-3." and rows[1]["cls"] == "high"
    assert rows[3]["text"] == "2 opened and 1 cleared in the last 7 days."  # the 9-day-old finding and the 20-day-old tombstone are outside the week
    empty = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger={"entries": [], "tombstones": []}, changes=None, schedule=None, now=NOW)
    assert empty[1]["text"] == "No open findings." and empty[1]["cls"] == "info"
    down = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=None, changes=None, schedule=None, now=NOW, ledger_error="s3 down")
    assert down[1]["text"] == "The ledger could not be read: s3 down."


def test_changed_line_names_the_last_deploy_and_what_opened_after_it():
    feed = [{"time": iso(hours=5), "kind": "deploy", "description": "billing@1.4.1 to production"},
            {"time": iso(minutes=40), "kind": "deploy", "description": "billing@1.4.2 to production"},
            {"time": iso(minutes=10), "kind": "other", "description": "github: 401"}]
    led = {"entries": [{"kind": "sql_new", "subject": "q9", "severity": "high", "state": "new", "first_seen_at": iso(minutes=5), "tickets": {}}], "tombstones": []}
    rows = diagnosis.lines(triage={"severity": "high", "title": "x"}, comparison={"changes": [feed[1]]}, manifest={"captured_at": iso(minutes=1), "changes_sources": ["github"]},
                           ledger=led, changes=feed, schedule=None, now=NOW)
    assert rows[2]["text"] == "Last change: deploy billing@1.4.2 to production 40 min ago, inside or just before the last window; 1 finding opened after it." and rows[2]["cls"] == "medium"
    none = diagnosis.lines(triage=None, comparison=None, manifest={"changes_sources": ["github"]}, ledger=None, changes=[], schedule=None, now=NOW)
    assert none[2]["text"] == "No deploy or setting change in the feed for the last window."


def test_week_line_counts_what_the_model_dropped():
    tr = {"severity": "medium", "title": "x", "source": "agent", "agreement": [{"mark": "rules_only", "severity": "high", "kind": "blocking", "subject": "", "model": None, "rules": 0},
                                                                                 {"mark": "rules_only", "severity": "low", "kind": "bloat", "subject": "t", "model": None, "rules": 1}]}
    rows = diagnosis.lines(triage=tr, comparison={}, manifest={"captured_at": iso(minutes=1)}, ledger={"entries": [], "tombstones": []}, changes=None, schedule=None, now=NOW)
    assert rows[3]["text"] == "0 opened and 0 cleared in the last 7 days; the model left 1 rules finding out of the last review." and rows[3]["cls"] == "medium"


def test_next_line_paused_next_and_failed():
    nxt = NOW + timedelta(hours=1)
    base = dict(triage=None, comparison=None, manifest=None, ledger=None, changes=None, now=NOW)
    assert diagnosis.lines(schedule={"schedule": "0 * * * *", "label": "hourly", "paused": True}, **base)[4]["text"] == "Schedule paused (hourly)."
    ok = diagnosis.lines(schedule={"schedule": "0 * * * *", "label": "hourly", "paused": False, "next": nxt, "last_status": "ok"}, **base)[4]
    assert ok["text"] == f"Next run {nxt:%m-%d %H:%M} (hourly)." and ok["cls"] == ""
    bad = diagnosis.lines(schedule={"schedule": "0 * * * *", "label": "hourly", "paused": False, "next": nxt, "last_status": "failed: pg-lab not reachable"}, **base)[4]
    assert bad["text"].endswith("the last scheduled run did not finish: failed: pg-lab not reachable.") and bad["cls"] == "high"


def test_the_question_carries_the_facts_and_fits_the_chat_limit():
    rows = diagnosis.lines(triage=None, comparison=None, manifest=None, ledger=None, changes=None, schedule=None, now=NOW)
    q = diagnosis.question("pg-prod", rows)
    assert q.startswith("Summarise the current state of pg-prod") and "- Health: No review yet" in q and "do not invent numbers" in q
    long_rows = [{"label": "Open", "text": "x" * 3000, "cls": ""}]
    assert len(diagnosis.question("pg-prod", long_rows)) <= diagnosis.MAX_QUESTION
