"""Discuss a review with the model: a question-and-answer thread about one finding (or the
whole review) of one snapshot.

The model gets the same read-only tools the triage agent had (snapshot sections, raw
reports, compare, SQL history, change feed) so answers stay grounded in the stored
evidence. It never touches a live database: there is no tool for that, and there will not
be one here. A thread is context for itself only; nothing from it feeds the next review.

Threads live next to the snapshot as chat/<key>.json, where key is "finding-<n>" (1-based,
the order of the review), "sql-<sql id>" (one statement of the window's top SQL) or "review". A backend only takes one task string, so earlier
turns are replayed as a transcript inside the task; tool calls from earlier turns are not
replayed, only their answers.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from dbtriage.agent import TriageAgent
from dbtriage.llm import Backend
from dbtriage.storage import Storage

SYSTEM = """You are the senior database performance engineer who wrote a review of one database snapshot, and an
on-call engineer is now asking you about it. Answer from the stored evidence: use the tools to read the snapshot,
the comparison, raw reports and SQL history whenever a number or a plan matters; never estimate what a tool can
tell you. You have no access to the live database and cannot run anything against it: if a question needs that,
say what to run and what to look for. Be direct and short: a few sentences, or a short list when there are steps.
Numbers get their units. Say so when the data does not support an answer. Plain Markdown, no headings."""

MAX_TURNS = 20          # questions per thread; the drawer is for a discussion, not a log
MAX_QUESTION = 2000     # characters
REPLAY_TURNS = 8        # earlier question/answer pairs replayed to the model
KEY_RE = re.compile(r"^(review|finding-\d{1,2}|sql-[A-Za-z0-9_]{1,80})$")  # sql-<id>: a thread about one statement

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def thread_key(finding: int | None) -> str:
    return "review" if finding is None else f"finding-{int(finding)}"


def thread_path(snapshot_id: str, key: str) -> str:
    return f"{snapshot_id}/chat/{key}.json"


def load_thread(store: Storage, snapshot_id: str, key: str) -> dict:
    if not KEY_RE.match(key):
        raise ValueError(f"bad thread key {key!r}")
    path = thread_path(snapshot_id, key)
    if store.exists(path):
        return store.get_json(path)
    return {"snapshot": snapshot_id, "key": key, "messages": []}


def list_threads(store: Storage, snapshot_id: str) -> dict[str, int]:
    """{key: number of messages} for every thread stored with a snapshot."""
    out = {}
    for k in store.list_keys(f"{snapshot_id}/chat/"):
        name = k.rsplit("/", 1)[-1]
        if name.endswith(".json") and KEY_RE.match(name[:-5]):
            try:
                out[name[:-5]] = len(store.get_json(k).get("messages", []))
            except (ValueError, KeyError):
                continue
    return out


class Chat:
    """ask() appends one question and the model's answer to a thread and stores it."""

    def __init__(self, store: Storage, backend_factory: Callable[[], Backend]):
        self.store = store
        self.backend_factory = backend_factory

    def context(self, snapshot_id: str, finding: int | str | None) -> str:
        """What the model is told before the first question: the review and, if asked, the one finding or statement.
        `finding` is a 1-based finding number, a thread key ("review", "finding-2", "sql-<id>") or None."""
        sql_id = None
        if isinstance(finding, str):
            if not KEY_RE.match(finding):
                raise ValueError(f"bad thread key {finding!r}")
            if finding.startswith("sql-"):
                sql_id, finding = finding[4:], None
            else:
                finding = int(finding.split("-", 1)[1]) if finding.startswith("finding-") else None
        m = self.store.get_json(f"{snapshot_id}/manifest.json")
        if not self.store.exists(f"{snapshot_id}/triage.json"):
            raise FileNotFoundError(f"{snapshot_id} has no review to discuss")
        tr = self.store.get_json(f"{snapshot_id}/triage.json")
        md = self.store.get(f"{snapshot_id}/triage.md").decode() if self.store.exists(f"{snapshot_id}/triage.md") else ""
        findings = tr.get("findings", [])
        parts = [
            f"Target {m['target']!r} (engine {m['engine']}), snapshot {snapshot_id}, captured {m.get('captured_at', '?')}.",
            f"Baseline: {(m.get('baseline') or {}).get('snapshot') or tr.get('baseline') or 'none'}.",
            f"Stored reports: {m.get('reports', [])}.",
            "", "The review as delivered:", md.strip() or json.dumps(tr, default=str),
        ]
        if finding is not None:
            if not 1 <= finding <= len(findings):
                raise ValueError(f"finding must be 1..{len(findings)}")
            f = findings[finding - 1]
            parts += ["", f"The question is about finding {finding}:", json.dumps(f, indent=1, default=str)]
        if sql_id:
            row = self._sql_row(snapshot_id, sql_id)
            if row is None:
                raise ValueError(f"sql id {sql_id!r} is not in this snapshot's top SQL")
            parts += ["", f"The question is about SQL id {sql_id} (use get_sql_history for its past):", json.dumps(row, indent=1, default=str)]
        return "\n".join(parts)

    def _sql_row(self, snapshot_id: str, sql_id: str) -> dict | None:
        rows = []
        if self.store.exists(f"{snapshot_id}/compare.json"):
            rows = (self.store.get_json(f"{snapshot_id}/compare.json").get("sql") or {}).get("top") or []
        if not rows and self.store.exists(f"{snapshot_id}/summary.json"):
            rows = self.store.get_json(f"{snapshot_id}/summary.json").get("top_sql") or []
        return next((r for r in rows if str(r.get("sql_id")) == sql_id), None)

    def ask(self, snapshot_id: str, finding: int | str | None, question: str) -> dict:
        """finding: a 1-based finding number, None for the whole review, or a thread key string."""
        question = (question or "").strip()
        if not question:
            raise ValueError("empty question")
        if len(question) > MAX_QUESTION:
            raise ValueError(f"question longer than {MAX_QUESTION} characters")
        key = finding if isinstance(finding, str) else thread_key(finding)
        with _lock(f"{snapshot_id}/{key}"):
            thread = load_thread(self.store, snapshot_id, key)
            if sum(1 for x in thread["messages"] if x["role"] == "user") >= MAX_TURNS:
                raise ValueError(f"thread is full ({MAX_TURNS} questions); start a new snapshot review")
            ctx = self.context(snapshot_id, key)
            m = self.store.get_json(f"{snapshot_id}/manifest.json")
            task = self._task(ctx, thread["messages"], question)
            tools = [t for t in TriageAgent(self.store, None).tool_specs(m["engine"], m["target"]) if t.name != "submit_review"]
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            thread["messages"].append({"role": "user", "text": question, "at": now})
            t0 = time.monotonic()
            backend = self.backend_factory()
            out = backend.run(SYSTEM, task, tools, max_iterations=12)
            text = out.text.strip() if out.text else ""
            if out.refused:
                text = "_The model declined to answer this question._"
            elif not text and out.stop_reason == "token_budget":
                text = (f"_The token budget ({out.usage.get('token_budget', 0):,} tokens) ran out before an answer. Ask a narrower "
                        "question, or raise `token_budget` in the Agent settings._")
            elif not text:
                text = f"_No answer (stop reason {out.stop_reason})._"
            answer = {"role": "assistant", "text": text, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "provider": backend.name, "model": out.model, "usage": out.usage, "tool_calls": out.tool_calls,
                      "duration_s": round(time.monotonic() - t0, 1)}
            thread["messages"].append(answer)
            self.store.put_json(thread_path(snapshot_id, key), thread)
        return answer

    @staticmethod
    def _task(context: str, history: list[dict], question: str) -> str:
        """The question comes first and is repeated last: small models lose a question buried under a long review."""
        parts = [f"The engineer asks: {question}", "", "Context (the review being discussed):", context, ""]
        pairs = [x for x in history if x["role"] in ("user", "assistant")][-2 * REPLAY_TURNS:]
        if pairs:
            parts.append("Earlier in this discussion:")
            for x in pairs:
                parts.append(f"{'Engineer' if x['role'] == 'user' else 'You'}: {x['text']}")
            parts.append("")
        parts.append(f"Answer this question now, using tools for any number you cite: {question}")
        return "\n".join(parts)


def thread_markdown(thread: dict) -> str:
    """A thread as text, for the CLI and the drawer's raw view."""
    lines = [f"# Discussion · {thread.get('snapshot')} · {thread.get('key')}", ""]
    for x in thread.get("messages", []):
        who = "**Engineer**" if x["role"] == "user" else f"**Model** ({x.get('provider', '')}:{x.get('model', '')})"
        lines += [f"{who} · {x.get('at', '')}", "", x["text"], ""]
    return "\n".join(lines)


def footer(msg: dict[str, Any]) -> str:
    u = msg.get("usage") or {}
    bits = [f"{msg.get('provider', '')}:{msg.get('model', '')}".strip(":")]
    if msg.get("tool_calls") is not None:
        bits.append(f"{msg['tool_calls']} tool calls")
    if u.get("input_tokens") is not None:
        bits.append(f"{u.get('input_tokens', 0):,} in / {u.get('output_tokens', 0):,} out tokens")
    if u.get("budget_reached"):
        bits.append("token budget reached")
    if msg.get("duration_s") is not None:
        bits.append(f"{msg['duration_s']}s")
    return " · ".join(bits)
