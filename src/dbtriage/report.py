"""Render a triage record as a review, the way agent-review posts on GitHub: a count line,
numbered findings most severe first with Impact / Evidence / Fix, or a "nothing found" line
with what was checked, then a footer with the model and the run."""
from __future__ import annotations
from dbtriage.ledger import describe_period

ICON = {"high": "🔴 **High**", "medium": "🟠 **Medium**", "low": "🟡 **Low**", "info": "🟢 **Info**"}
HEADER_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡", "info": "✅"}
LIFE = {"new": "new", "still_open": "still open", "worse": "worse", "quiet": "quiet", "returned": "returned", "recurring": "recurring", "gone": "gone"}


def life_note(life: dict | None) -> str:
    """The finding's life in a few words: its state, since when it has been open, and its ticket(s).
    "open since" is shown once a finding has a history (still open, worse, quiet), never for a new one."""
    if not life or life.get("state") not in LIFE:
        return ""
    bits = [LIFE[life["state"]]]
    if life["state"] != "new":
        since = (life.get("first_seen_at") or life.get("first_seen") or "")[:10]
        if since:
            bits.append(f"open since {since}")
    if period := describe_period(life.get("period_s")):
        bits.append(period)
    for name, t in sorted((life.get("tickets") or {}).items()):
        if isinstance(t, dict) and t.get("key"):
            bits.append(f"ticket [{t['key']}]({t['url']})" if t.get("url") else f"ticket {t['key']}")
    return " · ".join(bits)


def _ident(e: dict) -> str:
    return f"{e.get('kind') or ''} {e.get('subject') or ''}".strip()


def still_open_lines(triage: dict) -> list[str]:
    """Open findings that did not make the review's five, and findings that cleared this run."""
    lines = []
    if still := triage.get("still_open"):
        lines.append(f"**Still open** ({len(still)} not among the findings above):")
        for e in still:
            note = life_note(e)
            misses = int(e.get("misses") or 0)
            if misses:
                note += f" · not seen in the last {misses} run{'s' if misses != 1 else ''}"
            lines.append(f"- {ICON.get(e.get('severity'), e.get('severity', ''))} · `{_ident(e)}`: {e.get('title', '')} — _{note}_")
        lines.append("")
    if gone := triage.get("gone"):
        lines.append("**Cleared:** " + "; ".join(f"`{_ident(e)}` ({life_note(dict(e, state='gone'))})" for e in gone))
        lines.append("")
    return lines


def count_line(findings: list[dict]) -> str:
    if not findings:
        return "✅ **No significant change.**"
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("high", "medium", "low")}
    parts = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
    return f"**{len(findings)} finding{'s' if len(findings) != 1 else ''}**: {parts}"


def render_markdown(triage: dict, comparison: dict | None = None) -> str:
    sev = triage.get("severity", "info")
    findings = triage.get("findings", [])
    lines = [f"{HEADER_ICON.get(sev, '')} **DB review · {triage['target']}** ({triage['engine']}, {triage.get('mode', 'adhoc')})", ""]
    if triage.get("summary"):
        lines += [triage["summary"], ""]
    if usual := band_line(comparison):
        lines += [usual, ""]
    if triage.get("agent_error"):
        from dbtriage.egress import plain_provider_error

        lines += [f"⚠️ _agent unavailable, rules-based review shown: {plain_provider_error(str(triage['agent_error']))}_", ""]
    lines.append(count_line(findings))
    lines.append("")
    life = triage.get("life") or []
    for i, f in enumerate(findings, 1):
        note = life_note(life[i - 1]) if i <= len(life) else ""
        lines.append(f"{i}. {ICON.get(f['severity'], f['severity'])} · `{f.get('where', '')}`: {f['summary']}" + (f" — _{note}_" if note else ""))
        if f.get("impact"):
            lines.append(f"   - **Impact:** {f['impact']}")
        for e in f.get("evidence", [])[:4]:
            lines.append(f"   - **Evidence:** {e}")
        if f.get("fix"):
            lines.append(f"   - **Fix:** {f['fix']}")
        lines += proposal_lines(f.get("proposal"), indent="   ")
        lines.append("")
    lines += still_open_lines(triage)
    if triage.get("checked"):
        lines += [f"**Checked:** {triage['checked']}", ""]
    lines += agreement_lines(triage)
    lines.append(footer(triage, comparison))
    return "\n".join(lines)


def snapshot_ref(sid: str) -> str:
    """A snapshot id as the markdown writes one, in the footer and under a proposal."""
    return f"`{sid}`"


NEVER_APPLIED = "Second Look never applies changes; this is a suggestion with its undo, for a person to run."


def proposal_lines(p: dict | None, indent: str = "") -> list[str]:
    """The change proposal under a finding: the statement in a code fence, its undo, who runs it and when, and the
    snapshots whose numbers justify it. Empty for a finding without one, so older reviews render as they did."""
    if not p or not p.get("statement"):
        return []
    runner = " · ".join(x for x in (p.get("runner"), p.get("window")) if x)
    lines = [f"{indent}- **Change proposal:**", f"{indent}  ```", *(f"{indent}  {ln}" for ln in str(p["statement"]).splitlines()), f"{indent}  ```"]
    if p.get("rollback"):
        lines.append(f"{indent}  - **Undo:** {p['rollback']}")
    if runner:
        lines.append(f"{indent}  - **Run by:** {runner}")
    if ids := p.get("justified_by"):
        lines.append(f"{indent}  - **Justified by:** " + ", ".join(snapshot_ref(str(s)) for s in ids))
    lines.append(f"{indent}  - _{NEVER_APPLIED}_")
    return lines


def band_line(comparison: dict | None) -> str:
    """The usual band's sentence for the whole interval, under the summary: "total SQL time 12 ms/s is inside the
    usual Monday 02:00 band (6 weeks): median 10 ms/s, high 14 ms/s". Nothing when there is no band, or it did not
    apply to this window."""
    usual = (comparison or {}).get("band") or {}
    if not usual.get("sentence"):
        return ""
    return f"_Usual band: {usual['sentence']}_"


def agreement_lines(triage: dict) -> list[str]:
    """The rules-versus-model marks in a few lines, since this goes to Slack and Jira: the three counts, then
    one line per rules finding at medium or above that the model left out, so the prose cannot hide it.
    Nothing for a rules-only review or one stored before the marks existed."""
    rows = triage.get("agreement")
    if not isinstance(rows, list) or not rows:  # a clean review has nothing to agree about; the page hides it too
        return []
    from dbtriage.findings import dropped

    n = {m: sum(1 for r in rows if r.get("mark") == m) for m in ("both", "model_only", "rules_only")}
    lines = [f"**Agreement:** {n['both']} both, {n['model_only']} model only, {n['rules_only']} rules only"]
    heur = triage.get("heuristics") or []
    for r in dropped(rows):
        h = heur[r["rules"]] if r.get("rules") is not None and r["rules"] < len(heur) else {}
        title = h.get("title") or (r.get("kind", "") + (f" {r['subject']}" if r.get("subject") else ""))
        lines.append(f"- ⚠️ rules found, not in the review: {ICON.get(r.get('severity'), r.get('severity'))} · {title}")
    return lines + [""]


def footer(triage: dict, comparison: dict | None) -> str:
    bits = []
    if triage.get("source") == "agent":
        bits.append(f"{triage.get('provider', '')}:{triage.get('model', '')}".strip(":"))
        if triage.get("tool_calls") is not None:
            bits.append(f"{triage['tool_calls']} tool calls")
        u = triage.get("usage") or {}
        if u.get("input_tokens") is not None:
            bits.append(f"{u.get('input_tokens', 0):,} in / {u.get('output_tokens', 0):,} out tokens")
        if u.get("budget_reached"):
            bits.append(f"token budget {u.get('token_budget', 0):,} reached: stopped early")
        if triage.get("duration_s"):
            bits.append(f"{triage['duration_s']}s")
    else:
        bits.append("rules-based (no model)")
    bits.append(f"confidence {triage.get('confidence', '?')}")
    n = len((comparison or {}).get("findings", []))
    if n and triage.get("source") == "agent":
        bits.append(f"{n} heuristic finding{'s' if n != 1 else ''}")
    ref = f"snapshot {snapshot_ref(triage['snapshot'])}" + (f" vs {snapshot_ref(triage['baseline'])}" if triage.get("baseline") else "")
    if triage.get("baseline_reason"):
        ref += f" ({triage['baseline_reason']})"
    return "_" + " · ".join(bits) + " · " + ref + "_"
