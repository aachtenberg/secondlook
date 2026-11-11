"""What makes two findings the same finding.

A finding has a `kind` (the rule vocabulary in compare.py, or `other`) and a
`subject` (the one thing it names: a sql_id, table, wait class, parameter or
session). This module is the one join on those two fields, used both for the
agreement marks between the model's review and the rules on one run, and for
following a finding from one snapshot to the next. It accepts review-shaped
findings (kind/subject keys, as in triage.json "findings") and raw rules
findings (kind plus an evidence dict, as in compare.json "findings" and
triage.json "heuristics"). Pure functions only: no storage, no model.
"""
from __future__ import annotations

KINDS = (
    "sql_regressed", "sql_new", "sql_volume", "sql_top", "plan_change",
    "wait_shift", "cache_hit_drop", "deadlocks", "throughput_drop",
    "load_temp_bytes", "load_temp_files", "load_tmp_disk_tables", "load_sorts_disk", "load_row_lock_waits", "load_hard_parses",
    "blocking", "long_running", "lock_waits",
    "bloat", "stale_stats", "seq_scans",
    "engine_finding",
    "other",
)

# Evidence keys that name the subject of a rules finding, most specific first.
SUBJECT_KEYS = ("sql_id", "table", "class", "parameter", "metric")


def known_kind(kind: str) -> bool:
    """Engine-native findings (ADDM) carry their own type after `engine_`, so that prefix is open."""
    return kind in KINDS or kind.startswith("engine_")


def subject_of(f: dict) -> str:
    """The thing a finding names, as a string; empty when it names nothing (deadlocks, lock waits).

    Review-shaped findings carry `subject`; rules findings carry it inside the evidence dict.
    """
    if f.get("subject") is not None:
        return str(f["subject"])
    e = f.get("evidence")
    if isinstance(e, dict):
        for k in SUBJECT_KEYS:
            if e.get(k):
                return str(e[k])
    return ""


def identity(f: dict) -> tuple[str, str]:
    """Normalized (kind, subject); whitespace and case never make two findings different."""
    kind = (f.get("kind") or "other").strip().lower()
    return kind, subject_of(f).strip().lower()


def same_finding(a: dict, b: dict) -> bool:
    return identity(a) == identity(b)


def match(model_findings: list[dict], rules_findings: list[dict]) -> list[dict]:
    """One row per distinct identity, model findings first, with mark both / model_only / rules_only.

    When several findings share an identity the first one wins; the rows carry the
    matched `model` and `rules` entries, or None where that side has nothing.
    """
    rows: dict[tuple[str, str], dict] = {}
    for side, findings in (("model", model_findings or []), ("rules", rules_findings or [])):
        for f in findings:
            key = identity(f)
            row = rows.setdefault(key, {"kind": key[0], "subject": key[1], "model": None, "rules": None})
            if row[side] is None:
                row[side] = f
    for row in rows.values():
        row["mark"] = "both" if row["model"] and row["rules"] else "model_only" if row["model"] else "rules_only"
    return list(rows.values())
