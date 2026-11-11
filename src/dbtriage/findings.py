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
    "setting_changed",
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


SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}


def agreement(review: dict, heuristics: list[dict] | None) -> list[dict] | None:
    """The marks between a model review and the rules on one run, stored in triage.json as `agreement`.

    One row per identity: mark, kind, subject, the index of the model finding in `findings` and of the
    rules finding in `heuristics` (None where that side has nothing), and a severity (the model's where it
    spoke, else the rules'). Indexes rather than copies keep the stored object small and let the page point
    at the finding it already renders. A rules-only review has nothing to agree with: None.
    """
    if review.get("source") != "agent":
        return None
    findings, heuristics = review.get("findings") or [], heuristics or []
    rows = []
    for r in match(findings, heuristics):
        mi = next((i for i, f in enumerate(findings) if f is r["model"]), None)
        ri = next((i for i, f in enumerate(heuristics) if f is r["rules"]), None)
        picked = r["model"] if r["model"] is not None else r["rules"]
        rows.append({"mark": r["mark"], "kind": r["kind"], "subject": r["subject"], "model": mi, "rules": ri,
                     "severity": picked.get("severity") or "low"})
    return rows


def dropped(rows: list[dict] | None, min_severity: str = "medium") -> list[dict]:
    """Agreement rows the rules raised at `min_severity` or above and the model did not: the roadmap's measure.

    A rising count is a prompt or model problem to look at, so the page and the markdown both show these.
    """
    floor = SEVERITY_RANK.get(min_severity, 2)
    return [r for r in rows or [] if r.get("mark") == "rules_only" and SEVERITY_RANK.get(r.get("severity"), 0) >= floor]


def rules_dropped(review: dict | None) -> int | None:
    """The per-snapshot number: dropped rules findings at medium or above. None when the review has no
    agreement rows (rules-only, or stored before the marks existed), which is not the same as zero."""
    rows = (review or {}).get("agreement")
    return len(dropped(rows)) if isinstance(rows, list) else None
