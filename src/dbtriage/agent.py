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

from dbtriage import band, changes
from dbtriage import compare as cmp
from dbtriage.findings import KINDS, SUBJECT_KEYS, known_kind, subject_of
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
is safe and says how to roll back if it changes the database. Give each finding a kind from the fixed list in the
tool schema (other when none fits) and a subject: the one thing it names, exactly as the reports print it, so for a
SQL finding the sql_id verbatim, otherwise the table name, wait class, parameter name or session id, and empty when
there is none. These two fields are how a finding is matched to the rules and to earlier reviews; where is the
display text. If nothing is wrong, say so and list what you checked.
Finish by calling submit_review exactly once."""

class Finding(BaseModel):
    severity: str = Field(description="high, medium or low")
    kind: str = Field(description="One of: " + ", ".join(KINDS) + ". Use other when none fits")
    subject: str = Field(description="The one thing named, exactly as the reports print it: a sql_id, table name, wait class, "
                                     "parameter name or session id. Empty string when there is none")
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
        self.tool_log: list[dict] = []  # every tool call of the current run, in order; stored as tools.json
        self.band_weeks = band.DEFAULT_WEEKS  # the target's band_weeks, set by run(); 0 leaves the usual band out of compare_snapshots

    # ----------------------------------------------------------------- data access
    def _summary(self, snapshot_id: str) -> dict:
        s = self.storage.get_json(f"{snapshot_id}/summary.json")
        s["id"] = snapshot_id
        return s

    def _stored_changes(self, snapshot_id: str) -> list[dict] | None:
        """The change feed captured with the snapshot (changes.json), or None when the snapshot has none."""
        key = f"{snapshot_id}/changes.json"
        return self.storage.get_json(key) if self.storage.exists(key) else None

    def _changes(self, snapshot_id: str, hours: int) -> list[dict]:
        """Deploys, parameter changes, jobs and failovers in the hours before the capture, from the feed stored with
        the snapshot. Snapshots from before the feed was stored carry only a changes_file path: those are still read
        from disk, so an old snapshot's review does not lose what it had."""
        manifest = self.storage.get_json(f"{snapshot_id}/manifest.json")
        from datetime import datetime, timedelta

        end = datetime.fromisoformat(manifest["captured_at"])
        items = self._stored_changes(snapshot_id)
        if items is None:
            if not manifest.get("changes_file"):
                return []
            items = changes.read_file(manifest["changes_file"])
        return changes.within(items, end - timedelta(hours=hours), end)

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
            bloat changes, current sessions, and ranked heuristic findings. "band" is the
            usual band for this clock hour over the past weeks (median and high per metric,
            the current value's position) when the target has enough history; findings
            about a banded metric carry one sentence about it in their evidence.

            Args:
                snapshot_id: The later / current snapshot id.
                baseline_id: The earlier snapshot to compare against.
            """
            cur, base = agent._summary(snapshot_id), agent._summary(baseline_id)
            before = agent.storage.previous(engine, target, baseline_id)
            margin = agent.storage.get_json(f"{snapshot_id}/manifest.json").get("correlate_minutes")
            margin = cmp.CORRELATE_MINUTES if margin in (None, "") else int(margin)  # an explicit 0 is a choice, not a blank
            usual = band.usual_band(agent.storage, engine, target, snapshot_id, weeks=agent.band_weeks) if agent.band_weeks else None
            return json.dumps(cmp.compare(cur, base, agent._summary(before) if before else None,
                                          changes=agent._stored_changes(snapshot_id), correlate_minutes=margin, band=usual), default=str)

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
            bad = [f.kind for f in findings if not known_kind(f.kind)]
            if bad:
                return f"kind must be one of {', '.join(KINDS)} (got {bad})"
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
        """Provider-neutral tool descriptions for any backend ($ref-free: not every provider resolves $defs).

        Each tool is wrapped so the call lands in self.tool_log: the backends only ever call ToolSpec.fn, so recording
        here keeps the transcript out of all three of them. The chat and MCP server build specs on a throwaway agent,
        where the log is simply never read."""
        import time

        keep = 2000  # characters of each result kept in tools.json; the full text is in the snapshot anyway

        def recorded(name: str, call):
            def fn(args: dict) -> str:
                entry = {"n": len(self.tool_log) + 1, "tool": name, "args": args}
                self.tool_log.append(entry)  # appended before the call, so a tool that raises still shows up in order
                t0 = time.monotonic()
                try:
                    result = call(args)
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    raise
                finally:
                    entry["ms"] = round((time.monotonic() - t0) * 1000)
                text = str(result)
                entry["result"] = text if len(text) <= keep else f"{text[:keep]}... ({len(text)} chars)"
                return result
            return fn

        specs = []
        for t in self._tools(engine, target):
            d = t.to_dict()
            specs.append(ToolSpec(name=d["name"], description=d["description"], input_schema=_inline_refs(d["input_schema"]),
                                  fn=recorded(d["name"], t.call)))
        return specs

    # ----------------------------------------------------------------- run
    def run(self, engine: str, target: str, snapshot_id: str, baseline_id: str | None, mode: str,
            max_iterations: int = 25, notes: str | None = None, band_weeks: int = band.DEFAULT_WEEKS) -> dict:
        self.result = None
        self.band_weeks = band_weeks
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
        import sys
        import time

        t0 = time.monotonic()
        self.tool_log = []
        out = None
        try:
            out = self.backend.run(SYSTEM, task, self.tool_specs(engine, target), max_iterations=max_iterations,
                                   require_tool="submit_review", stop_when=lambda: self.result is not None)
        finally:
            # Stored whether or not a review came back: when the model fails, which reports it read is exactly what
            # explains the failure. The arguments are the model's own text about already-redacted reports, so nothing
            # here needs further redaction. A store failure must not replace the review or hide why the model failed.
            try:
                self.storage.put_json(f"{snapshot_id}/tools.json", {"snapshot": snapshot_id, "provider": self.backend.name,
                                                                    "model": out.model if out else None, "calls": self.tool_log})
            except Exception as exc:  # noqa: BLE001
                print(f"could not store {snapshot_id}/tools.json ({type(exc).__name__}: {str(exc)[:200]})", file=sys.stderr)
        if out.refused:
            raise RuntimeError(f"model declined the request (stop_reason=refusal, provider={self.backend.name})")
        if self.result is None:  # the caller falls back to the rules review, so a silent model never hides an incident
            if out.stop_reason == "token_budget":
                raise RuntimeError(f"token budget of {out.usage.get('token_budget', 0):,} reached before the model submitted a review "
                                   f"({out.usage.get('input_tokens', 0) + out.usage.get('output_tokens', 0):,} tokens, {out.tool_calls} tool calls); "
                                   "raise [agent] token_budget or set it to 0 for no limit")
            raise RuntimeError(f"model finished without submitting a review (stop reason {out.stop_reason}, {out.tool_calls} tool calls)")
        return dict(self.result, source="agent", provider=self.backend.name, model=out.model, usage=out.usage,
                    tool_calls=out.tool_calls, tools_file="tools.json", stop_reason=out.stop_reason, duration_s=round(time.monotonic() - t0, 1))


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
    window_start = (comparison.get("current") or {}).get("start")
    findings = [{
        "severity": f["severity"] if f["severity"] in ("high", "medium", "low") else "low",
        "kind": f["kind"], "subject": subject_of(f), "where": _where(f), "summary": f["title"][:120],
        "impact": _impact(f["kind"]),
        "evidence": ([f["detail"]] if f.get("detail") else []) + _evidence_lines(f.get("evidence") or {}, window_start=window_start),
        "fix": _advice(f),
    } for f in found[:5]]
    summary = f"{len(found)} heuristic finding(s); the top one is {findings[0]['summary'][0].lower() + findings[0]['summary'][1:]}."
    usual = comparison.get("band") or {}
    if usual.get("above") and usual.get("sentence"):  # the whole hour is heavier than usual: that belongs in the first line
        summary += f" The interval is heavier than usual: {usual['sentence']}."
    return {
        "severity": findings[0]["severity"], "title": findings[0]["summary"], "summary": summary,
        "checked": "load profile, top SQL, wait classes, sessions, tables against the previous snapshot (rules only)",
        "confidence": "low", "findings": findings, "source": "rules",
    }


def _evidence_lines(evidence: dict, limit: int = 3, window_start: str | None = None) -> list[str]:
    """Three evidence lines, but a settings change, the usual band or a deploy in the same window always makes the
    cut: that context is the point of the finding, and it would otherwise be the fourth item."""
    items = [(k, v) for k, v in evidence.items() if k not in ("sql_id", "changes")]
    keep = [kv for kv in items if kv[0] in KEPT_EVIDENCE]
    rest = [kv for kv in items if kv[0] not in KEPT_EVIDENCE][:max(limit - len(keep), 0)]
    return [v if k == "band" else f"{k}: {json.dumps(v, default=str)[:160]}" for k, v in rest + keep] \
        + _change_lines(evidence.get("changes"), window_start)


KEPT_EVIDENCE = ("settings_changed", "band")  # a sentence already; the band line is printed as it is


def _change_lines(found: list[dict] | None, window_start: str | None, limit: int = 3) -> list[str]:
    """One sentence per correlated change: "deploy billing@1.4.2 to production landed 6 minutes before the window"."""
    from datetime import datetime

    out = []
    for c in (found or [])[:limit]:
        when = ""
        try:
            t, s = datetime.fromisoformat(c["time"]), datetime.fromisoformat(window_start)
            mins = round((s - t).total_seconds() / 60)
            when = (f" {mins} minute{'s' if mins != 1 else ''} before the window" if mins > 0
                    else f" {-mins} minute{'s' if mins != -1 else ''} into the window" if mins < 0 else " at the start of the window")
        except (KeyError, TypeError, ValueError):
            when = f" at {c.get('time')}" if c.get("time") else ""
        out.append(f"{c.get('kind', 'change')} {c.get('description', '')} landed{when}")
    return out


def _where(f: dict) -> str:
    """Display text: the subject with its evidence key in front, or the kind when nothing is named."""
    e = f.get("evidence") or {}
    for k in SUBJECT_KEYS:
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
