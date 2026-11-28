"""Judges: other models that check a finished review against the same data before it is stored.

Off unless [agent] judges lists at least one. Every review is judged, the rules' as well as a model's, so a judge can
catch a clean review that missed a problem as well as a finding the data does not support. Each judge gets the review,
what Second Look measured for each finding's subject (dbtriage.grounding), how busy the database was, the rules'
findings, and the reviewer's read-only tools, and returns a verdict per finding plus what the review missed.

A finding stands when at least `quorum` judges agree with it (default: a majority of the judges). One that falls short
is kept, marked disputed with the judges' reasons, and does not alert: the ledger does not track it and delivery does
not fire on it. Something the review missed is added as a finding when `quorum` judges name the same kind and subject;
otherwise it is shown as a note. A judge that fails is recorded with its error; when fewer than `quorum` answer, the
review stands as written and says it was not judged.

    [agent]
    judges = [{ provider = "openrouter", model = "anthropic/claude-sonnet-5.5" },
              { provider = "openrouter", model = "z-ai/glm-5.3" }]
    quorum = 2        # judges that must agree for a finding to stand; 0 or unset: a majority
"""
from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from anthropic import beta_tool
from pydantic import BaseModel, Field

from dbtriage import grounding
from dbtriage.findings import KINDS, known_kind

RANK = {"low": 1, "medium": 2, "high": 3}
MISSED_MAX = 3
# what a judge's entry may carry: its own provider and model, and where to reach them. Never api_key_cmd (a command
# on the server), and nothing of the main provider's carries over unless the judge names it.
JUDGE_KEYS = ("provider", "model", "base_url", "api_key_env", "region", "aws_profile", "project_id", "resource", "effort",
              "max_tokens", "token_budget", "max_iterations")
# what a judge inherits from [agent]: limits, not the provider
INHERITED = ("token_budget", "max_iterations", "max_tokens")

SYSTEM = """You are a senior database performance engineer checking a colleague's review of one database before it
reaches the on-call engineer. You get the review, what Second Look measured for each finding's subject (computed from
the snapshots, not written by anyone), how busy the database was, and the rules' findings. Use the tools to check
anything that decides a verdict; never estimate a number a tool can give you.

For every finding, agree or dispute:
- dispute when the data does not support it: the subject is not in the data, a key number is wrong or appears in no
  tool result, a running total since the statistics reset is read as a change in the window, or the problem is real
  but its severity is two levels off (high for an efficiency issue on an idle database);
- agree when it holds up, and give the severity you would give it when it is one level off.
Weigh impact against load: impacting means callers are affected now (a per-call time a request would notice, a large
share of a busy database's time, a resource near its limit). A change that costs the database almost nothing is an
efficiency issue, low at most. Severity: high = callers affected now or imminent risk; medium = degraded but coping;
low = a real but minor problem or an efficiency issue.

Then name what the review missed: a problem the data shows that no finding covers, with evidence a tool gave you.
At most 3, most severe first; none when nothing was missed, which is the usual case. Do not restate a finding the
review already has, even at another severity: that belongs in its verdict. Give each missed finding a kind from the
fixed list and its subject exactly as the reports print it (the sql_id verbatim, else the table, wait class,
parameter or session id).

Finish by calling submit_verdict once, with one verdict for every finding."""


class Verdict(BaseModel):
    finding: int = Field(description="The finding's number in the review, from 1")
    verdict: str = Field(description="agree or dispute")
    severity: str = Field(description="The severity you would give it: high, medium or low")
    reason: str = Field(description="One sentence: why, with the number that decides it")


class Missed(BaseModel):
    severity: str = Field(description="high, medium or low")
    kind: str = Field(description="One of: " + ", ".join(KINDS) + ". Use other when none fits")
    subject: str = Field(description="The one thing named, exactly as the reports print it. Empty string when there is none")
    where: str = Field(description="What the finding is about: a sql_id, table, wait class, parameter or session")
    summary: str = Field(description="The problem in at most about 15 words")
    impact: str = Field(description="What fails or degrades, and when, in one sentence")
    evidence: list[str] = Field(description="Specific supporting facts with numbers from the tools, each one sentence")
    fix: str = Field(description="What to do, one or two sentences; include rollback if it changes the database")


def judges_of(agent_cfg: dict[str, Any]) -> list[dict]:
    """The configured judges, each with the limits it inherits from [agent]. Entries without a provider and a model
    are skipped (the settings page refuses them; a hand-edited file may still carry one)."""
    out = []
    for j in agent_cfg.get("judges") or []:
        if not isinstance(j, dict) or not j.get("provider") or not j.get("model"):
            continue
        base = {k: agent_cfg[k] for k in INHERITED if agent_cfg.get(k) not in (None, "")}
        out.append({**base, **{k: v for k, v in j.items() if k in JUDGE_KEYS and v not in (None, "")}})
    return out


def quorum_of(agent_cfg: dict[str, Any], n: int) -> int:
    """How many judges must agree for a finding to stand: [agent] quorum, else a majority; never more than there are."""
    try:
        q = int(agent_cfg.get("quorum") or 0)
    except (TypeError, ValueError):
        q = 0
    return min(max(q, 1), n) if q > 0 else n // 2 + 1


def name_of(j: dict) -> str:
    return f"{j.get('provider')}:{j.get('model')}"


# ----------------------------------------------------------------------------- one judge
def task_text(review: dict, engine: str, target: str, snapshot_id: str, baseline_id: str | None, mode: str,
              measured: list[list[str]], load_sentence: str, rules: list[dict]) -> str:
    """What a judge is asked: the review as the reader would get it, with the measured lines beside each finding."""
    who = (f"the model {review.get('provider', '')}:{review.get('model', '')}".rstrip(":") if review.get("source") == "agent"
           else "the rules (no model)")
    lines = [f"Check this review of database target {target!r} (engine {engine}), run mode {mode!r}.",
             f"Current snapshot: {snapshot_id}",
             f"Baseline snapshot: {baseline_id or 'none'}"]
    if load_sentence:
        lines.append(f"How busy the database was: {load_sentence}")
    lines += ["", f"The review, written by {who}:", f"Summary: {review.get('summary') or ''}", f"Checked: {review.get('checked') or ''}"]
    findings = review.get("findings") or []
    if not findings:
        lines.append("Findings: none. The review says nothing is wrong.")
    for i, (f, ms) in enumerate(zip(findings, measured), 1):
        lines.append(f"Finding {i} · {f.get('severity')} · {f.get('kind') or 'other'} {f.get('subject') or ''}".rstrip()
                     + f": {f.get('summary') or ''}")
        if f.get("impact"):
            lines.append(f"  impact: {f['impact']}")
        lines += [f"  evidence: {e}" for e in f.get("evidence") or []]
        lines += [f"  measured by Second Look: {m}" for m in ms]
        if f.get("not_in_data"):
            lines.append(f"  in the evidence but in no tool result: {', '.join(map(str, f['not_in_data']))}")
        if f.get("fix"):
            lines.append(f"  fix: {f['fix']}")
    if rules:
        lines += ["", "The rules (no model) found:"]
        lines += [f"- {r.get('severity')} {r.get('kind')}: {r.get('title')}" for r in rules[:5]]
    lines += ["", "Give a verdict on every finding, then what the review missed, and call submit_verdict."]
    return "\n".join(lines)


def run_judge(judge_cfg: dict, storage, review: dict, engine: str, target: str, snapshot_id: str, baseline_id: str | None,
              mode: str, comparison: dict | None, band_weeks: int, backend=None) -> dict:
    """One judge's verdict on the review: {"judge", "model", "verdicts", "missed", "usage", "tool_calls", "duration_s",
    "calls"}, or {"judge", "error"} when it could not give one. Never raises: a judge must not cost the review."""
    from dbtriage.agent import TriageAgent
    from dbtriage.llm import make_backend

    name = name_of(judge_cfg)
    t0 = time.monotonic()
    agent = None
    try:
        backend = backend or make_backend(judge_cfg, env=False)  # DBTRIAGE_PROVIDER/MODEL pick the reviewer, never a judge
        agent = TriageAgent(storage, backend, band_weeks_for=lambda _t: band_weeks)
        agent.snapshot_id, agent._engine, agent._target, agent._baseline_id = snapshot_id, engine, target, baseline_id
        agent._run_compare = comparison
        findings = review.get("findings") or []
        measured = [f.get("measured") or m for f, m in zip(findings, agent._measured(findings))]
        comparison = agent._run_compare or None  # computed by _measured when the caller had none
        load = ((comparison or {}).get("load") or {}).get("sentence") or ""
        task = task_text(review, engine, target, snapshot_id, baseline_id, mode, measured, load, (comparison or {}).get("findings") or [])
        # the numbers a missed finding may cite: tool results and what Second Look computed, never the review's own text
        agent._seen = [load] + [m for ms in measured for m in ms]
        out: dict = {}

        @beta_tool
        def submit_verdict(verdicts: list[Verdict], missed: list[Missed], note: str) -> str:
            """Record your verdict, at the end: one verdict for every finding of the review, then what it missed.

            Args:
                verdicts: One per finding, by its number: agree or dispute, the severity you would give it, and why.
                missed: Problems the data shows that no finding covers, at most 3. Empty when nothing was missed.
                note: One sentence on the review as a whole.
            """
            nums = sorted(v.finding for v in verdicts)
            if nums != list(range(1, len(findings) + 1)):
                return f"give exactly one verdict for each finding 1 to {len(findings)} (got {nums or 'none'})" if findings \
                    else "the review has no findings: verdicts must be empty"
            if bad := [v.verdict for v in verdicts if v.verdict not in ("agree", "dispute")]:
                return f"verdict must be agree or dispute (got {bad})"
            if bad := [s for s in [v.severity for v in verdicts] + [m.severity for m in missed] if s not in RANK]:
                return f"severity must be high, medium or low (got {bad})"
            if bad := [m.kind for m in missed if not known_kind(m.kind)]:
                return f"kind must be one of {', '.join(KINDS)} (got {bad})"
            ms = sorted((m.model_dump() for m in missed), key=lambda m: -RANK[m["severity"]])[:MISSED_MAX]
            pool = grounding.pool(agent._seen or [])
            for m, lines in zip(ms, agent._measured(ms)):
                if lines:
                    m["measured"] = lines
                if nums_ := grounding.unseen(m.get("evidence") or [], pool):
                    m["not_in_data"] = nums_
            out.update(verdicts=[v.model_dump() for v in sorted(verdicts, key=lambda v: v.finding)], missed=ms, note=note)
            return "recorded"

        tools = [t for t in agent._tools(engine, target) if t.name != "submit_review"] + [submit_verdict]
        specs = agent.tool_specs(engine, target, tools=tools)
        res = backend.run(SYSTEM, task, specs, max_iterations=int(judge_cfg.get("max_iterations") or 25),
                          require_tool="submit_verdict", stop_when=lambda: bool(out))
        if res.refused:
            raise RuntimeError("the judge declined the request")
        if not out:
            raise RuntimeError(f"no verdict (stop reason {res.stop_reason}, {res.tool_calls} tool calls)")
        return {"judge": name, "model": res.model, **out, "usage": res.usage, "tool_calls": res.tool_calls,
                "duration_s": round(time.monotonic() - t0, 1), "calls": agent.tool_log}
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - an outage or a missing key: recorded, and the review stands
        print(f"judge {name} failed ({type(exc).__name__}: {str(exc)[:300]})", file=sys.stderr)
        return {"judge": name, "error": f"{type(exc).__name__}: {str(exc)[:300]}", "duration_s": round(time.monotonic() - t0, 1),
                "calls": agent.tool_log if agent else []}
    finally:
        if agent is not None:
            agent._seen = None


# ----------------------------------------------------------------------------- the quorum
def _norm(s: str | None) -> str:
    return str(s or "").strip().strip("`\"'").lower()


def combine(review: dict, results: list[dict], quorum: int) -> dict:
    """Put the judges' verdicts on the review (in place) and return it. See the module docstring for the rule."""
    answered = [r for r in results if not r.get("error")]
    summary = {"quorum": quorum, "judged": len(answered) >= quorum, "judges": [
        {k: r[k] for k in ("judge", "model", "note", "error", "usage", "tool_calls", "duration_s") if r.get(k) not in (None, "")}
        for r in results]}
    review["judges"] = summary
    if not summary["judged"]:
        summary["note"] = (f"{len(answered)} of {len(results)} judge{'s' if len(results) != 1 else ''} answered; the quorum is "
                           f"{quorum}, so the review stands as written")
        return review
    findings = review.get("findings") or []
    for i, f in enumerate(findings, 1):
        votes = [{"judge": r["judge"], **{k: v[k] for k in ("verdict", "severity", "reason")}}
                 for r in answered for v in r.get("verdicts") or [] if v.get("finding") == i]
        f["judges"] = votes
        f.pop("disputed", None)
        if sum(1 for v in votes if v["verdict"] == "agree") < quorum:
            f["disputed"] = True
    # what the review missed, grouped by what it names; a nameless one is its judge's alone
    have = {(f.get("kind") or "other", _norm(f.get("subject"))) for f in findings if f.get("subject")}
    groups: dict[tuple, list[tuple[str, dict]]] = {}
    for r in answered:
        for n, m in enumerate(r.get("missed") or []):
            key = (m["kind"], _norm(m["subject"])) if _norm(m.get("subject")) else (m["kind"], "", r["judge"], n)
            groups.setdefault(key, []).append((r["judge"], m))
    added, noted = [], []
    for key, items in groups.items():
        by = sorted({j for j, _ in items})
        first = items[0][1]
        if key[:2] in have or len(by) < quorum:  # already a finding (its verdicts say the rest), or short of the quorum
            noted.append({"judges": by, **{k: first[k] for k in ("severity", "kind", "subject", "summary") if k in first}})
            continue
        low = min((m for _, m in items), key=lambda m: RANK[m["severity"]])  # the most cautious of their severities
        added.append({**first, "severity": low["severity"], "added_by": by})
    if noted:
        summary["noted"] = noted
    if added:
        review["findings"] = sorted(findings + added, key=lambda f: -RANK.get(f.get("severity"), 0))
    standing = [f for f in review.get("findings") or [] if not f.get("disputed")]
    disputed = len(review.get("findings") or []) - len(standing)
    summary.update(disputed=disputed, added=len(added))
    # the review's own severity is what still stands: a disputed finding does not page
    if standing:
        review["severity"], review["title"] = standing[0]["severity"], standing[0].get("summary") or review.get("title")
    elif review.get("findings"):
        review["severity"] = "info"
        review["title"] = f"{disputed} finding{'s' if disputed != 1 else ''} disputed by the judges"
    return review


def judge_review(review: dict, storage, engine: str, target: str, snapshot_id: str, baseline_id: str | None, mode: str,
                 comparison: dict | None, agent_cfg: dict, band_weeks: int, backends: list | None = None) -> dict:
    """Run every configured judge on the review at once, combine their verdicts into it, and store their tool calls
    as judges.json beside the review's own tools.json. Returns the review; it is unchanged when no judge is set."""
    judges = judges_of(agent_cfg)
    if not judges:
        return review
    q = quorum_of(agent_cfg, len(judges))
    with ThreadPoolExecutor(max_workers=len(judges)) as pool:
        futures = [pool.submit(run_judge, j, storage, review, engine, target, snapshot_id, baseline_id, mode, comparison,
                               band_weeks, (backends or [None] * len(judges))[i]) for i, j in enumerate(judges)]
        results = [f.result() for f in futures]
    try:
        storage.put_json(f"{snapshot_id}/judges.json", {"snapshot": snapshot_id, "quorum": q, "judges": results})
    except Exception as exc:  # noqa: BLE001 - the verdicts are on the review either way
        print(f"could not store {snapshot_id}/judges.json ({type(exc).__name__}: {str(exc)[:200]})", file=sys.stderr)
    for r in results:
        r.pop("calls", None)
    return combine(review, results, q)
