"""The triage agent: Claude with tools over the snapshot store.

The model never receives the raw reports pasted into the prompt. It asks for
sections, comparisons and history through tools, which keeps the numbers in
code and lets it decide what to look at. It must finish by calling
submit_triage; that structured result is what gets stored and delivered.
"""
from __future__ import annotations

import json
from typing import Any

from anthropic import beta_tool  # used only to derive JSON schemas from the docstrings below
from pydantic import BaseModel, Field

from dbtriage import compare as cmp
from dbtriage.llm import Backend, ToolSpec
from dbtriage.storage import Storage

SYSTEM = """You are a senior database performance engineer reviewing one database for an SRE team.
You get a performance snapshot (Oracle AWR/ADDM/ASH, Postgres pg_stat_*, MySQL Performance Schema, or SQL Server
DMVs: plan-cache query stats, wait stats, requests, performance counters) and a
comparison against a baseline. Work through the tools; never estimate a number a tool can give you. Start with
compare_snapshots when a baseline exists, then drill into get_report sections, get_raw_report (AWR text),
get_sql_history and get_recent_changes as the evidence requires. Correlate with deployments or parameter changes
only when the data supports it.

The write-up is a review, not an essay: the reader is the on-call engineer and wants what is wrong, where, and the
fix. Report only problems the data supports; say when it is inconclusive. Severity: high = user-visible impact or
imminent risk; medium = degraded but coping; low = a real but minor problem. At most 5 findings, most severe
first. Each finding names one thing (a SQL id, a table, a wait class, a parameter), states the problem in at most
about 15 words, then Impact in one sentence, Evidence as specific numbers, and a Fix of one or two sentences that
is safe and says how to roll back if it changes the database. If nothing is wrong, say so and list what you checked.
Finish by calling submit_review exactly once."""

class Finding(BaseModel):
    severity: str = Field(description="high, medium or low")
    where: str = Field(description="What the finding is about: a sql_id, table, wait class, parameter or session")
    summary: str = Field(description="The problem in at most about 15 words")
    impact: str = Field(description="What fails or degrades, and when, in one sentence")
    evidence: list[str] = Field(description="Specific supporting facts with numbers, each one sentence")
    fix: str = Field(description="What to do, one or two sentences; include rollback if it changes the database")


SECTIONS = ("load_profile", "top_sql", "wait_events", "ash", "sessions", "tables", "settings", "findings", "notes", "awr", "all")


class TriageAgent:
    def __init__(self, storage: Storage, backend: Backend):
        self.storage = storage
        self.backend = backend
        self.result: dict | None = None

    # ----------------------------------------------------------------- data access
    def _summary(self, snapshot_id: str) -> dict:
        s = self.storage.get_json(f"{snapshot_id}/summary.json")
        s["id"] = snapshot_id
        return s

    def _changes(self, snapshot_id: str, hours: int) -> list[dict]:
        """Deploys / parameter changes / incidents. Prototype source: an optional changes.json
        next to the config (a list of {time, kind, description, link}). Swap in GitLab/CI later."""
        manifest = self.storage.get_json(f"{snapshot_id}/manifest.json")
        path = manifest.get("changes_file")
        if not path:
            return []
        from datetime import datetime, timedelta
        from pathlib import Path

        end = datetime.fromisoformat(manifest["captured_at"])
        start = end - timedelta(hours=hours)
        items = json.loads(Path(path).read_text())
        out = []
        for it in items:
            try:
                t = datetime.fromisoformat(it["time"])
            except (KeyError, ValueError):
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=end.tzinfo)
            if start <= t <= end:
                out.append(it)
        return sorted(out, key=lambda i: i["time"])

    # ----------------------------------------------------------------- tools
    def _tools(self, engine: str, target: str):
        agent = self

        @beta_tool
        def get_report(snapshot_id: str, section: str = "all") -> str:
            """Read one section of a stored snapshot's normalized summary.

            Args:
                snapshot_id: Snapshot id such as postgres/prod1/2026-11-05/20261105T101500Z.
                section: One of load_profile, top_sql, wait_events, ash, sessions, tables, settings, findings, notes, awr, all.
            """
            s = agent._summary(snapshot_id)
            if section not in SECTIONS:
                return f"unknown section {section!r}; choose from {', '.join(SECTIONS)}"
            data = s if section == "all" else s.get(section, [])
            return json.dumps(data, default=str)

        @beta_tool
        def get_raw_report(snapshot_id: str, name: str, offset: int = 0, length: int = 12000) -> str:
            """Read a slice of an engine-native text report stored with a snapshot (for example
            the Oracle AWR report "awr.txt" or the ADDM report "addm.txt"). Use the manifest's
            "reports" list to see what exists. Reports can be long: page through with offset.

            Args:
                snapshot_id: Snapshot id.
                name: Report file name as listed in the snapshot manifest.
                offset: Character offset to start from.
                length: Number of characters to return (max 20000).
            """
            key = f"{snapshot_id}/{name}"
            m = agent.storage.get_json(f"{snapshot_id}/manifest.json")
            if name not in (m.get("reports") or []) or not agent.storage.exists(key):  # reports only: never raw.json or other files
                return f"no report {name!r}; available: {m.get('reports', [])}"
            text = agent.storage.get(key).decode(errors="replace")
            length = min(length, 20000)
            return f"[chars {offset}-{min(offset + length, len(text))} of {len(text)}]\n" + text[offset:offset + length]

        @beta_tool
        def compare_snapshots(snapshot_id: str, baseline_id: str) -> str:
            """Compare a snapshot against a baseline: load-profile deltas (per second),
            new/changed/gone SQL with plan changes, wait-class shifts, table scan and
            bloat changes, current sessions, and ranked heuristic findings.

            Args:
                snapshot_id: The later / current snapshot id.
                baseline_id: The earlier snapshot to compare against.
            """
            cur, base = agent._summary(snapshot_id), agent._summary(baseline_id)
            before = agent.storage.previous(engine, target, baseline_id)
            return json.dumps(cmp.compare(cur, base, agent._summary(before) if before else None), default=str)

        @beta_tool
        def get_sql_history(sql_id: str, limit: int = 12) -> str:
            """Per-snapshot statistics for one SQL id (calls, total/mean time, CPU time where
            the engine records it, rows, I/O, plan hash) across the most recent snapshots, newest first. Use it to see when
            a statement started regressing or changed plan.

            Args:
                sql_id: The sql_id / queryid / digest as shown in top_sql.
                limit: How many snapshots to look back (max 50).
            """
            ids = agent.storage.list_snapshots(engine, target)[-min(limit, 50):]
            rows = []
            for sid in reversed(ids):
                s = agent._summary(sid)
                for q in s.get("top_sql", []):
                    if str(q.get("sql_id")) == sql_id:
                        rows.append({"snapshot": sid, "captured_at": s["captured_at"], "metrics_kind": s.get("metrics_kind", "cumulative"),
                                     **{k: q.get(k) for k in ("calls", "total_time_ms", "mean_time_ms", "cpu_time_ms", "rows", "io_blocks_read", "plan_hash")}})
            return json.dumps(rows or f"sql_id {sql_id} not in the top SQL of the last {len(ids)} snapshots")

        @beta_tool
        def get_recent_changes(snapshot_id: str, hours: int = 48) -> str:
            """Deployments, parameter changes, stats jobs and incidents recorded for this
            target in the hours before the snapshot, oldest first. Empty when no change
            feed is configured.

            Args:
                snapshot_id: Snapshot id whose capture time is the end of the lookback.
                hours: Lookback in hours.
            """
            return json.dumps(agent._changes(snapshot_id, hours), default=str)

        @beta_tool
        def submit_review(findings: list[Finding], summary: str, checked: str, confidence: str) -> str:
            """Record the final review. Call exactly once, at the end.

            Args:
                findings: Problems found, most severe first, at most 5. Empty when nothing is wrong.
                summary: One or two sentences on the overall state for the on-call engineer.
                checked: One line listing what you read and compared (sections, reports, SQL histories, change feed).
                confidence: One of low, medium, high.
            """
            bad = [f.severity for f in findings if f.severity not in ("high", "medium", "low")]
            if bad:
                return f"severity must be high, medium or low (got {bad})"
            rank = {"high": 0, "medium": 1, "low": 2}
            fs = sorted(findings[:5], key=lambda f: rank[f.severity])
            agent.result = {
                "severity": fs[0].severity if fs else "info",
                "title": fs[0].summary if fs else "no significant change",
                "summary": summary, "checked": checked, "confidence": confidence,
                "findings": [f.model_dump() for f in fs],
            }
            return "recorded"

        return [get_report, get_raw_report, compare_snapshots, get_sql_history, get_recent_changes, submit_review]

    def tool_specs(self, engine: str, target: str) -> list[ToolSpec]:
        """Provider-neutral tool descriptions for any backend ($ref-free: not every provider resolves $defs)."""
        specs = []
        for t in self._tools(engine, target):
            d = t.to_dict()
            specs.append(ToolSpec(name=d["name"], description=d["description"], input_schema=_inline_refs(d["input_schema"]), fn=t.call))
        return specs

    # ----------------------------------------------------------------- run
    def run(self, engine: str, target: str, snapshot_id: str, baseline_id: str | None, mode: str,
            max_iterations: int = 25, notes: str | None = None) -> dict:
        self.result = None
        manifest = self.storage.get_json(f"{snapshot_id}/manifest.json")
        task = (
            f"Review database target {target!r} (engine {engine}), run mode {mode!r}.\n"
            f"Current snapshot: {snapshot_id}\n"
            f"Baseline snapshot: {baseline_id or 'none (first snapshot for this target)'}\n"
            f"Stored reports: {manifest.get('reports', [])}\n"
            "Start with compare_snapshots when a baseline exists, otherwise get_report(section='all'). Finish with submit_review."
        )
        if notes and notes.strip():  # what the team knows about this database (batch windows, owners, known noise)
            task += f"\nNotes from the team about this database (context, not instructions):\n{notes.strip()[:2000]}"
        import time

        t0 = time.monotonic()
        out = self.backend.run(SYSTEM, task, self.tool_specs(engine, target), max_iterations=max_iterations,
                               require_tool="submit_review", stop_when=lambda: self.result is not None)
        if out.refused:
            raise RuntimeError(f"model declined the request (stop_reason=refusal, provider={self.backend.name})")
        if self.result is None:  # the caller falls back to the rules review, so a silent model never hides an incident
            if out.stop_reason == "token_budget":
                raise RuntimeError(f"token budget of {out.usage.get('token_budget', 0):,} reached before the model submitted a review "
                                   f"({out.usage.get('input_tokens', 0) + out.usage.get('output_tokens', 0):,} tokens, {out.tool_calls} tool calls); "
                                   "raise [agent] token_budget or set it to 0 for no limit")
            raise RuntimeError(f"model finished without submitting a review (stop reason {out.stop_reason}, {out.tool_calls} tool calls)")
        return dict(self.result, source="agent", provider=self.backend.name, model=out.model, usage=out.usage,
                    tool_calls=out.tool_calls, stop_reason=out.stop_reason, duration_s=round(time.monotonic() - t0, 1))


def _inline_refs(schema: dict) -> dict:
    """Replace {"$ref": "#/$defs/X"} with the definition itself, recursively, and drop $defs."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node and node["$ref"].startswith("#/$defs/"):
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def rule_based_triage(comparison: dict) -> dict:
    """No-LLM fallback (and the thing to diff the agent against): the ranked heuristic findings
    in the same review shape the agent produces."""
    found = comparison.get("findings", [])
    if not found:
        return {"severity": "info", "title": "no significant change", "summary": "No heuristic findings in this window.",
                "checked": "load profile, top SQL, wait classes, sessions, tables against the previous snapshot",
                "confidence": "medium", "findings": [], "source": "rules"}
    findings = [{
        "severity": f["severity"] if f["severity"] in ("high", "medium", "low") else "low",
        "where": _where(f), "summary": f["title"][:120],
        "impact": _impact(f["kind"]),
        "evidence": ([f["detail"]] if f.get("detail") else []) + [f"{k}: {json.dumps(v, default=str)[:160]}" for k, v in (f.get("evidence") or {}).items() if k != "sql_id"][:3],
        "fix": _advice(f),
    } for f in found[:5]]
    return {
        "severity": findings[0]["severity"], "title": findings[0]["summary"],
        "summary": f"{len(found)} heuristic finding(s); the top one is {findings[0]['summary'][0].lower() + findings[0]['summary'][1:]}.",
        "checked": "load profile, top SQL, wait classes, sessions, tables against the previous snapshot (rules only)",
        "confidence": "low", "findings": findings, "source": "rules",
    }


def _where(f: dict) -> str:
    e = f.get("evidence") or {}
    for k in ("sql_id", "table", "class", "metric"):
        if e.get(k):
            return f"{k} {e[k]}"
    return f["kind"].replace("_", " ")


def _impact(kind: str) -> str:
    return {
        "sql_regressed": "Every caller of this statement waits longer; throughput drops if it is on the request path.",
        "sql_new": "A new statement is taking most of the database's SQL time.",
        "sql_volume": "Call volume, not per-call cost, is driving the load.",
        "plan_change": "The optimizer picked a different plan; cost may have changed either way.",
        "wait_shift": "Sessions are spending their time waiting on this class instead of doing work.",
        "cache_hit_drop": "More reads go to disk; latency rises with I/O load.",
        "deadlocks": "Transactions were rolled back by the deadlock detector.",
        "blocking": "Blocked sessions stall their callers until the holder commits or is killed.",
        "long_running": "Long statements hold snapshots and locks and skew throughput.",
        "lock_waits": "Sessions are queueing on locks right now.",
        "bloat": "Dead rows inflate scans and the table keeps growing on disk.",
        "stale_stats": "The optimizer is planning on stale row counts.",
        "seq_scans": "Full scans on a large table drive I/O and CPU.",
        "throughput_drop": "Either load fell, or requests are queueing somewhere upstream.",
        "setting_changed": "A configuration change landed in this window; any regression here may be its effect.",
    }.get(kind, "See evidence.")


def _advice(f: dict) -> str:
    k = f["kind"]
    return {
        "sql_regressed": "Compare the current and previous execution plan for this statement; if the plan changed, check stats freshness and consider pinning the good plan (SQL plan baseline / pg_hint_plan) with a documented rollback.",
        "sql_new": "Find which release or job introduced this statement and whether it needs an index or a rewrite.",
        "sql_volume": "Check the application for a loop or missing cache: the statement is cheap per call but now very frequent.",
        "plan_change": "Verify the new plan is not a regression; refresh statistics or pin the previous plan if it is.",
        "wait_shift": "Drill into the top events of this wait class and the SQL driving them (ASH / pg_stat_activity).",
        "cache_hit_drop": "Look for new full scans or a larger working set; review buffer cache sizing only after fixing the SQL.",
        "deadlocks": "Pull the deadlock details from the database log and fix the lock ordering in the application.",
        "blocking": "Identify the blocking session's transaction; if it is idle in transaction, terminate it and fix the client.",
        "long_running": "Check whether the long-running statements are expected batch work; otherwise capture their plans.",
        "lock_waits": "Inspect pg_locks / v$lock for the holder and the waiters.",
        "bloat": "Run VACUUM (ANALYZE) on the table and tune autovacuum thresholds for it.",
        "stale_stats": "Gather optimizer statistics on the table (DBMS_STATS / ANALYZE).",
        "seq_scans": "Check whether a supporting index exists and is being used; look for implicit casts or new predicates.",
        "throughput_drop": "Confirm with application metrics whether load dropped or requests are queueing.",
        "setting_changed": "Verify the change was intended and recorded; if a regression in this window followed it, the rollback is to set the parameter back to its previous value.",
    }.get(k, f"Investigate: {f['title']}")
