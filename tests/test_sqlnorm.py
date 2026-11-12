"""The normalized text hash: one id for one statement whatever the engine's bind style or the literals."""
from __future__ import annotations

from dbtriage.redact import redact_sql
from dbtriage.sqlnorm import annotate, normalize, text_hash

SAME = {
    "postgres": "select * from t where id = $1 and name = $2",
    "oracle positional": "SELECT * FROM t WHERE id = :1 AND name = :2",
    "oracle named": "SELECT * FROM t WHERE id = :id AND name = :nm",
    "mysql digest": "SELECT * FROM t WHERE id = ? AND name = ?",
    "sqlserver": "SELECT * FROM t WHERE id = @p1 AND name = @P2",
    "literals": "select * from t where id = 42 and name = 'O''Brien'",
    "redacted": "SELECT * FROM t WHERE id = ? AND name = '?'",
    "comments and spacing": "/* app: billing */ select *  -- the row\n   from t\n where id=$1 and name=$2;",
}


def test_every_bind_style_hashes_the_same():
    hashes = {k: text_hash(v) for k, v in SAME.items()}
    assert len(set(hashes.values())) == 1, hashes
    assert normalize(SAME["postgres"]) == "select * from t where id=? and name=?"
    assert len(hashes["postgres"]) == 16 and all(c in "0123456789abcdef" for c in hashes["postgres"])


def test_different_statements_differ():
    assert text_hash("select * from t where id = $1") != text_hash("select * from t where id = $1 and name = $2")
    assert text_hash("select * from orders where id = $1") != text_hash("select * from users where id = $1")
    assert text_hash("delete from t where id = ?") != text_hash("select * from t where id = ?")


def test_redaction_then_hash_equals_hash_of_the_original():
    """The collectors store redacted text; the hash of that must equal the hash of what the application sent."""
    raw = "UPDATE orders SET amount = amount + 12.5, note = 'late fee' WHERE id = 991 AND status IN ('open', 'due')"
    assert text_hash(redact_sql(raw)) == text_hash(raw)


def test_in_lists_collapse_and_casts_survive():
    assert normalize("select 1 from t where a in (1, 2, 3)") == normalize("select 7 from t where a in (4)")
    assert normalize("select $1::int from t") == "select ?::int from t"  # the :: cast is not a :bind
    assert normalize("begin x := 5; end") == "begin x :=?;end"  # := is not a :bind either
    assert normalize("select @@version") == "select @@version"  # a server variable, not a bind
    assert normalize("select t1.a from t1 join t2 on t1.id = t2.id") == "select t1.a from t1 join t2 on t1.id=t2.id"  # digits in names stay


def test_empty_text_has_no_hash():
    assert normalize(None) == "" and normalize("") == "" and text_hash(None) == "" and text_hash("  ") == ""


def test_annotate_marks_rows_with_text_only():
    s = {"top_sql": [{"sql_id": "a", "text": "select 1"}, {"sql_id": "b", "text": None}, {"sql_id": "c"}],
         "sessions": [{"state": "active", "text": "select 2"}, {"state": "idle"}], "wait_events": [{"class": "IO"}]}
    annotate(s)
    assert s["top_sql"][0]["text_hash"] == text_hash("select 1") and "text_hash" not in s["top_sql"][1] and "text_hash" not in s["top_sql"][2]
    assert s["sessions"][0]["text_hash"] == text_hash("select 2") and "text_hash" not in s["sessions"][1]
    assert annotate({}) == {}
