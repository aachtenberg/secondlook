"""MCP server: your databases' stored snapshots and reviews, for any MCP client (Claude Code, Claude Desktop ...).

Off by default. targets.toml turns it on, and only targets.toml can: the web view and its API cannot.

    [mcp]
    enabled = true                    # dbtriage mcp refuses to start without it
    allow_run = false                 # true adds run_review: collect and review a database now (spends model credit,
                                      # delivers to your channels, like the web view's Run button)
    tools = ["list_targets", "get_review"]   # optional: offer only these (default: every read tool)

Then point your client at it, for example:

    claude mcp add dbtriage -- uv --directory /path/to/db-triage run --extra mcp dbtriage mcp

It talks over stdio only: nothing listens on the network, and the client runs it as you. The read tools are the
reviewer's own (the same masking of SQL literals, the same limits), reading what is stored: none of them reaches a
live database or changes anything. Needs the optional SDK: uv sync --extra mcp.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
from typing import Any, Callable

from dbtriage.config import ENGINES, TARGET_NAME, Config
from dbtriage.storage import Storage

FIELDS = ("enabled", "allow_run", "tools")  # the [mcp] section of targets.toml
READ_TOOLS = ("list_targets", "list_snapshots", "get_review", "get_report", "compare_snapshots", "get_sql_history",
              "get_raw_report", "get_recent_changes", "get_operator_answers")
RUN_TOOL = "run_review"
# what dbtriage.cli.collect names snapshots: engine/target/YYYY-MM-DD/YYYYMMDDTHHMMSSZ, -n for a second one that second.
# Ids come from the client's model, so nothing else is used to build a storage key.
# built from config's engine list and target-name rule, so every target targets.toml accepts can be read here
SNAPSHOT_ID = re.compile(rf"(?:{'|'.join(map(re.escape, ENGINES))})/(?:{TARGET_NAME.pattern})/\d{{4}}-\d{{2}}-\d{{2}}/\d{{8}}T\d{{6}}Z(?:-\d{{1,4}})?")


def _section(cfg: Config) -> dict:
    """[mcp] as a table: load_config refuses anything else, but a Config can be built by hand."""
    m = getattr(cfg, "mcp", None)
    return m if isinstance(m, dict) else {}


def settings(cfg: Config) -> dict:
    """[mcp] as it applies: enabled and allow_run only when they are real booleans true; tools checked."""
    m = _section(cfg)
    tools = m.get("tools")
    return {"enabled": m.get("enabled") is True, "allow_run": m.get("allow_run") is True,
            "tools": list(tools) if isinstance(tools, list) else list(READ_TOOLS)}


def offered(cfg: Config) -> list[str]:
    """The tools the server offers: the read tools (or those listed in tools), plus run_review only with allow_run
    and, when tools lists names, only if run_review is among them."""
    s, listed = settings(cfg), isinstance(_section(cfg).get("tools"), list)
    read = [t for t in READ_TOOLS if t in s["tools"]] if listed else list(READ_TOOLS)
    return read + ([RUN_TOOL] if s["allow_run"] and (not listed or RUN_TOOL in s["tools"]) else [])


def problems(cfg: Config) -> list[str]:
    m = _section(cfg)
    out = [f"[mcp] {k}: unknown setting" for k in m if k not in FIELDS]
    tools = m.get("tools")
    if tools is not None and not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)):
        out.append("[mcp] tools: a list of tool names")
    elif tools:
        bad = [t for t in tools if t not in READ_TOOLS + (RUN_TOOL,)]
        if bad:
            out.append(f"[mcp] tools: unknown {', '.join(bad)}; choose from {', '.join(READ_TOOLS + (RUN_TOOL,))}")
    return out


class Tools:
    """What each tool does, as plain methods returning text (JSON), so tests and other front ends can call them."""

    def __init__(self, cfg: Config, store: Storage, run: Callable[..., dict] | None = None):
        self.cfg, self.store = cfg, store
        self._run = run  # cli.run_request, injected so tests need no database

    # ---------------------------------------------------------------- helpers
    def _agent(self):
        """The reviewer's tools without a run(): compare_snapshots reads each target's own band_weeks, as the CLI does."""
        from dbtriage.agent import TriageAgent
        from dbtriage.band import weeks_for

        return TriageAgent(self.store, None, band_weeks_for=lambda name: weeks_for(self.cfg, name))

    def _agent_tool(self, snapshot_id: str, name: str, args: dict) -> str:
        m = self._manifest(snapshot_id)
        spec = next(t for t in self._agent().tool_specs(m["engine"], m["target"]) if t.name == name)
        return spec.fn(args)

    def _read(self, key: str, missing):
        """A stored JSON file, or `missing` when it is not there. Read it rather than ask exists(): the outage-
        tolerant store answers exists() False while the store is down, and that must not read as 'no snapshot'."""
        from dbtriage.storage import StorageUnavailable

        try:
            return self.store.get_json(key)
        except FileNotFoundError:  # every store raises FileNotFoundError for a missing key
            return missing
        except ValueError as exc:  # there, but not JSON: say so rather than pretend it is missing
            raise LookupError(f"the stored {key} is not valid JSON ({exc})") from None
        except StorageUnavailable as exc:
            raise LookupError(f"the snapshot store is unreachable ({exc}); try again shortly") from None

    def _listed(self) -> None:
        """Listings keep working through an outage with only what is spooled locally: say so rather than answer
        with a partial or empty list."""
        if getattr(self.store, "degraded", None):
            raise LookupError(f"the snapshot store is unreachable ({self.store.degraded}); try again shortly")

    def _manifest(self, snapshot_id: str) -> dict:
        if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
            raise LookupError(f"no snapshot {snapshot_id!r}: ids look like postgres/<target>/2026-11-06/20261106T165508Z; "
                              "list_snapshots shows them")
        m = self._read(f"{snapshot_id}/manifest.json", None)
        if m is None:
            raise LookupError(f"no snapshot {snapshot_id!r}; list_snapshots shows the ids")
        return m

    def _target(self, name: str):
        t = self.cfg.targets.get(name)
        if t is None:
            raise LookupError(f"no target {name!r}; choose from {', '.join(sorted(self.cfg.targets))}")
        return t

    def _triage(self, snapshot_id: str) -> dict:
        if not SNAPSHOT_ID.fullmatch(snapshot_id or ""):  # never build a key from anything else
            return {}
        return self._read(f"{snapshot_id}/triage.json", {})  # {}: not reviewed yet (dbtriage collect alone)

    # ---------------------------------------------------------------- read tools
    def list_targets(self) -> str:
        out = []
        for name, t in sorted(self.cfg.targets.items()):
            ids = self.store.list_snapshots(t.engine, name)
            self._listed()
            tr = self._triage(ids[-1]) if ids else {}
            out.append({"target": name, "engine": t.engine, "snapshots": len(ids), "latest": ids[-1] if ids else None,
                        "severity": tr.get("severity"), "summary": tr.get("summary"), "notes": t.extra.get("notes")})
        return json.dumps(out, default=str)

    def list_snapshots(self, target: str, limit: int = 20) -> str:
        t = self._target(target)
        ids = self.store.list_snapshots(t.engine, target)[-max(1, min(int(limit), 200)):]
        self._listed()
        rows = []
        for sid in reversed(ids):
            m, tr = self._manifest(sid), self._triage(sid)
            rows.append({"snapshot_id": sid, "captured_at": m.get("captured_at"), "mode": (m.get("window") or {}).get("mode"),
                         "reason": (m.get("trigger") or {}).get("reason"), "severity": tr.get("severity"),
                         "summary": tr.get("summary")})
        return json.dumps(rows, default=str)

    def get_review(self, snapshot_id: str) -> str:
        m, tr = self._manifest(snapshot_id), self._triage(snapshot_id)
        keep = ("severity", "summary", "findings", "checked", "confidence", "source", "provider", "model", "baseline",
                "baseline_policy", "agent_error", "heuristics")
        return json.dumps({"snapshot_id": snapshot_id, "target": m.get("target"), "engine": m.get("engine"),
                           "captured_at": m.get("captured_at"), "window": m.get("window"), "trigger": m.get("trigger"),
                           "db_snapshot": m.get("db_snapshot"), "reports": m.get("reports", []),
                           **({k: tr[k] for k in keep if k in tr} if tr else {"review": "not reviewed yet"})}, default=str)

    def get_report(self, snapshot_id: str, section: str = "all") -> str:
        return self._agent_tool(snapshot_id, "get_report", {"snapshot_id": snapshot_id, "section": section})

    def compare_snapshots(self, snapshot_id: str, baseline_id: str) -> str:
        self._manifest(baseline_id)
        self._listed()  # the comparison lists the target's snapshots: during an outage only the spool's
        return self._agent_tool(snapshot_id, "compare_snapshots", {"snapshot_id": snapshot_id, "baseline_id": baseline_id})

    def get_sql_history(self, target: str, sql_id: str, limit: int = 12) -> str:
        t = self._target(target)
        spec = next(s for s in self._agent().tool_specs(t.engine, target) if s.name == "get_sql_history")
        out = spec.fn({"sql_id": sql_id, "limit": max(1, min(int(limit), 50))})  # 0 would mean every snapshot
        self._listed()  # it lists the target's snapshots: during an outage that is only the spool, not 'never seen'
        return out

    def get_raw_report(self, snapshot_id: str, name: str, offset: int = 0, length: int = 12000) -> str:
        self._manifest(snapshot_id)
        self._listed()  # the reviewer's tool asks exists(), which an outage answers False: not 'no such report'
        return self._agent_tool(snapshot_id, "get_raw_report",
                                {"snapshot_id": snapshot_id, "name": name, "offset": int(offset), "length": int(length)})

    def get_recent_changes(self, snapshot_id: str, hours: int = 48) -> str:
        return self._agent_tool(snapshot_id, "get_recent_changes", {"snapshot_id": snapshot_id, "hours": int(hours)})

    def get_operator_answers(self, snapshot_id: str) -> str:
        out = self._agent_tool(snapshot_id, "get_operator_answers", {"snapshot_id": snapshot_id})
        self._listed()  # it lists the snapshot's discussions: during an outage that is only the spool, not 'none pasted'
        return out

    # ---------------------------------------------------------------- the one that acts (allow_run only)
    def run_review(self, target: str, mode: str = "adhoc", reason: str = "", use_model: bool = True) -> str:
        from dbtriage.triggers import MODES, TriageRequest

        self._target(target)
        if mode not in MODES:
            raise LookupError(f"mode: one of {', '.join(MODES)}")
        try:
            # stdout is the MCP wire: the stdout channel (print) and anything else that prints go to stderr instead.
            # The SDK diverts fd 1 while serving, but only best-effort; this holds either way.
            with contextlib.redirect_stdout(sys.stderr):
                result = self._run(TriageRequest(target=target, mode=mode, source="mcp", reason=reason or "asked over MCP"),
                                   self.cfg, use_llm=bool(use_model))
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - as the web view and scheduler do: report it, keep serving
            raise LookupError(f"run failed: {type(exc).__name__}: {str(exc)[:300]}") from None
        return json.dumps({k: result.get(k) for k in ("snapshot", "severity", "summary", "source", "delivered", "agent_error")}
                          | {"findings": len(result.get("findings") or [])}, default=str)


DESCRIPTIONS = {
    "list_targets": "List the databases dbtriage watches, each with its latest snapshot id and review severity and summary.",
    "list_snapshots": "List a database's stored snapshots, newest first, with when, why, and the review's severity. "
                      "Args: target (a name from list_targets), limit (default 20).",
    "get_sql_history": "One statement across a database's stored snapshots: calls, time, CPU time (Oracle, SQL Server), rows and plan in each, newest "
                       "first. Args: target (a name from list_targets), sql_id (as in top SQL or a finding), limit "
                       "(snapshots, 1 to 50, default 12).",
    "get_review": "Get the review of one snapshot: severity, summary, findings (impact, evidence, fix), what was checked, "
                  "the baseline it was compared with, and which reports are stored.",
    RUN_TOOL: "Collect a snapshot of a database now and review it (as the web view's Run button does): connects to the "
              "database as collection does (read-only, unless that database is opted into taking snapshots on the "
              "database), uses the configured model unless use_model is false, and posts the review to the configured "
              "channels. Args: target, mode (adhoc | incident | scheduled), reason.",
}
ANNOTATE = {RUN_TOOL: {"read_only_hint": False, "destructive_hint": False, "idempotent_hint": False, "open_world_hint": True}}


def instructions(cfg: Config) -> str:
    """What the client's model is told, naming only the tools this server offers."""
    tools = offered(cfg)
    have = lambda *names: [n for n in names if n in tools]  # noqa: E731
    start, dig = have("list_targets", "list_snapshots", "get_review"), have("get_report", "compare_snapshots", "get_sql_history",
                                                                         "get_raw_report", "get_recent_changes", "get_operator_answers")
    text = ("dbtriage keeps snapshots of database health (load, top SQL, waits, sessions, tables; AWR/ADDM on Oracle) and "
            "a review of each.")
    if start:
        text += f" Start with {', '.join(start)}."
    if dig:
        text += f" Dig in with {', '.join(dig)}."
    text += " Quote numbers only from tool results."
    if RUN_TOOL in tools:
        return text + (" run_review is the one tool that connects to a database (as collection does: read-only unless "
                       "that database is opted into taking snapshots on it) and starts a review; it spends model credit "
                       "and posts the review to the team's channels, so use it only when asked. Every other tool reads "
                       "only what is stored.")
    return text + " None of these tools touches a live database."


def build(cfg: Config, store: Storage, run: Callable[..., dict] | None = None) -> Any:
    """The MCP server with the tools [mcp] allows. Raises SystemExit when [mcp] is off or invalid."""
    s = settings(cfg)
    if not s["enabled"]:
        raise SystemExit("dbtriage mcp is off: set enabled = true under [mcp] in targets.toml to turn it on "
                         "(only targets.toml can; see docs/mcp.md)")
    if errs := problems(cfg):
        raise SystemExit("; ".join(errs))
    from mcp.server.mcpserver import MCPServer  # the optional SDK: only once the config says the server may run
    from mcp.types import ToolAnnotations

    if run is None:
        from dbtriage.cli import run_request as run
    tools = Tools(cfg, store, run)
    from dbtriage.agent import TriageAgent

    reviewer = {t.name: t.description for t in TriageAgent(store, None).tool_specs("postgres", "x")}
    server = MCPServer("dbtriage", instructions=instructions(cfg))
    for name in offered(cfg):
        hints = ANNOTATE.get(name, {"read_only_hint": True, "destructive_hint": False, "idempotent_hint": True,
                                    "open_world_hint": False})
        server.add_tool(_guard(getattr(tools, name)), name=name,
                        description=DESCRIPTIONS.get(name) or reviewer.get(name, ""),
                        annotations=ToolAnnotations(**hints), structured_output=False)
    return server


def _guard(fn: Callable[..., str]) -> Callable[..., str]:
    """Tool errors the model can act on come back as text, not as a failed call."""
    import functools

    @functools.wraps(fn)
    def call(*args, **kwargs):
        from dbtriage.storage import StorageUnavailable

        try:
            return fn(*args, **kwargs)
        except LookupError as exc:
            return f"error: {exc.args[0] if exc.args else exc}"
        except StorageUnavailable as exc:  # say what it is, wherever in a tool the store went away
            return f"error: the snapshot store is unreachable ({exc}); try again shortly"
        except SystemExit as exc:  # a configuration problem deep inside must not end the server for every other call
            return f"error: {exc}"
        except Exception as exc:  # noqa: BLE001 - a broken changes file, a bad stored file ...: text, not a failed call
            return f"error: {type(exc).__name__}: {str(exc)[:300]}"

    return call


def main(cfg: Config, store: Storage) -> int:
    try:
        server = build(cfg, store)
    except ModuleNotFoundError as exc:
        print(f"dbtriage mcp needs the MCP SDK: uv sync --extra mcp ({exc})", file=sys.stderr)
        return 2
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    server.run("stdio")
    return 0
