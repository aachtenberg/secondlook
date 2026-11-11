from dbtriage.findings import KINDS, identity, known_kind, match, same_finding, subject_of


def rules(kind, **evidence):
    return {"score": 50, "severity": "medium", "kind": kind, "title": kind, "detail": "", "evidence": evidence}


def review(kind, subject, **extra):
    return {"severity": "medium", "kind": kind, "subject": subject, "where": f"{kind} {subject}", "summary": "s",
            "impact": "i", "evidence": ["e"], "fix": "f", **extra}


def test_subject_of_reads_both_shapes():
    assert subject_of(rules("sql_regressed", sql_id="abc123", mean_before_ms=1)) == "abc123"
    assert subject_of(rules("bloat", table="public.orders", dead_rows=5)) == "public.orders"
    assert subject_of(rules("wait_shift", top_events=[], **{"class": "IO"})) == "IO"
    assert subject_of(rules("load_temp_bytes", metric="temp_bytes")) == "temp_bytes"
    assert subject_of(rules("setting_changed", parameter="work_mem")) == "work_mem"
    assert subject_of(rules("deadlocks", deadlocks=3)) == ""
    assert subject_of(rules("blocking", sessions=[{"pid": 1}])) == ""
    assert subject_of(review("sql_new", "q1")) == "q1"
    assert subject_of(review("other", "")) == ""
    assert subject_of({"kind": "other", "evidence": ["a list, not a dict"]}) == ""


def test_identity_normalizes_and_defaults_kind():
    assert identity(review("SQL_Regressed", " ABC123 ")) == ("sql_regressed", "abc123")
    assert identity(rules("sql_regressed", sql_id="abc123")) == ("sql_regressed", "abc123")
    assert identity({"severity": "low", "where": "q1", "summary": "s"}) == ("other", "")
    assert identity({"kind": None, "subject": "T"}) == ("other", "t")


def test_same_finding_across_shapes():
    assert same_finding(review("plan_change", "q1"), rules("plan_change", sql_id="q1", plan_before=1, plan_after=2))
    assert not same_finding(review("plan_change", "q1"), rules("sql_regressed", sql_id="q1"))
    assert not same_finding(review("bloat", "orders"), review("bloat", "events"))
    assert same_finding(rules("deadlocks", deadlocks=1), rules("deadlocks", deadlocks=7))


def test_match_marks_both_sides():
    model = [review("sql_regressed", "q1"), review("bloat", "orders"), review("other", "", where="general slowness")]
    heur = [rules("bloat", table="orders", dead_rows=1), rules("sql_regressed", sql_id="q1"), rules("deadlocks", deadlocks=2)]
    rows = match(model, heur)
    by = {(r["kind"], r["subject"]): r for r in rows}
    assert len(rows) == 4
    assert [r["mark"] for r in rows] == ["both", "both", "model_only", "rules_only"]  # model order first, then the rest
    assert by[("sql_regressed", "q1")]["model"] is model[0] and by[("sql_regressed", "q1")]["rules"] is heur[1]
    assert by[("other", "")]["rules"] is None and by[("deadlocks", "")]["model"] is None
    assert match([], []) == []
    assert [r["mark"] for r in match(None, heur)] == ["rules_only"] * 3


def test_match_keeps_the_first_of_duplicates():
    rows = match([review("bloat", "t"), review("bloat", "T")], [])
    assert len(rows) == 1 and rows[0]["model"]["subject"] == "t"


def test_kind_vocabulary():
    assert "other" in KINDS and len(set(KINDS)) == len(KINDS)
    assert known_kind("sql_regressed") and known_kind("engine_sql_tuning") and not known_kind("slow_query")
