"""Render a triage record as a review, the way agent-review posts on GitHub: a count line,
numbered findings most severe first with Impact / Evidence / Fix, or a "nothing found" line
with what was checked, then a footer with the model and the run."""
from __future__ import annotations

ICON = {"high": "🔴 **High**", "medium": "🟠 **Medium**", "low": "🟡 **Low**", "info": "🟢 **Info**"}
HEADER_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡", "info": "✅"}


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
    if triage.get("agent_error"):
        from dbtriage.egress import plain_provider_error

        lines += [f"⚠️ _agent unavailable, rules-based review shown: {plain_provider_error(str(triage['agent_error']))}_", ""]
    lines.append(count_line(findings))
    lines.append("")
    for i, f in enumerate(findings, 1):
        lines.append(f"{i}. {ICON.get(f['severity'], f['severity'])} · `{f.get('where', '')}`: {f['summary']}")
        if f.get("impact"):
            lines.append(f"   - **Impact:** {f['impact']}")
        for e in f.get("evidence", [])[:4]:
            lines.append(f"   - **Evidence:** {e}")
        if f.get("fix"):
            lines.append(f"   - **Fix:** {f['fix']}")
        lines.append("")
    if triage.get("checked"):
        lines += [f"**Checked:** {triage['checked']}", ""]
    lines.append(footer(triage, comparison))
    return "\n".join(lines)


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
    ref = f"snapshot `{triage['snapshot']}`" + (f" vs `{triage['baseline']}`" if triage.get("baseline") else "")
    if triage.get("baseline_reason"):
        ref += f" ({triage['baseline_reason']})"
    return "_" + " · ".join(bits) + " · " + ref + "_"
