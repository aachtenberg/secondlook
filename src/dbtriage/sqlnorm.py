"""A statement's shape, independent of its literals and bind style, and a short hash of it.

Replicas, shards and read copies run the same SQL, but each engine gives it a different id: pg_stat_statements'
queryid depends on object OIDs and differs between two databases, the MySQL digest is stable only within one
server version, Oracle's sql_id is stable across instances only for identical text. The normalized text hash is the
one id the fleet digest can join on: `normalize()` lowercases, strips comments, collapses whitespace and replaces
every literal and bind marker ($1, :1, :name, ?, @p1, quoted strings, numbers) with one placeholder, so the same
statement sent with literals, with binds, or already redacted (redact.py turns literals into '?') normalizes the
same. The hash is stored as `text_hash` next to `sql_id` on every top_sql and sessions row (cli.collect).

Same-engine matching only. Oracle and PostgreSQL rarely run the same text (quoting, functions, casts, LIMIT
against ROWNUM), and when they do, the planner, the stats and the fix differ, so a cross-engine match would read as
one cause when it is two. The digest joins hashes within one engine and says nothing across engines.
"""
from __future__ import annotations

import hashlib
import re

PLACEHOLDER = "?"
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_STRING = re.compile(r"'(?:[^']|'')*'")
# $1 (postgres), :1 and :name (oracle; not the :: cast or :=), @p1 and @name (sql server, mysql; not @@version)
_BIND = re.compile(r"\$\d+|(?<![:\w]):(?!:)[A-Za-z_]\w*|(?<![:\w]):\d+|(?<![@\w])@[A-Za-z_]\w*")
_NUMBER = re.compile(r"(?<![\w$?])[-+]?\d+(?:\.\d+)?(?![\w$])")
_WS = re.compile(r"\s+")
_AROUND_PUNCT = re.compile(r"\s*([(),;=<>])\s*")  # "count ( * )" and "count(*)" are one statement
_LIST = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")  # in (?, ?, ?) -> (?): the list's length is a literal too


def normalize(text: str | None) -> str:
    """The statement with its literals, binds, comments, case and spacing taken out; "" for no text."""
    if not text:
        return ""
    out = _BLOCK_COMMENT.sub(" ", text)
    out = _LINE_COMMENT.sub(" ", out)
    out = _STRING.sub(PLACEHOLDER, out)
    out = _BIND.sub(PLACEHOLDER, out)
    out = _NUMBER.sub(PLACEHOLDER, out)
    out = _WS.sub(" ", out).strip().lower()
    out = _AROUND_PUNCT.sub(r"\1", out)
    out = _LIST.sub("(?)", out)
    return out.strip(" ;")


def text_hash(text: str | None) -> str:
    """16 hex characters of sha256 over the normalized text; "" for no text. Short because it is read by people in
    a digest line and compared only within one store."""
    norm = normalize(text)
    return hashlib.sha256(norm.encode()).hexdigest()[:16] if norm else ""


def annotate(summary: dict) -> dict:
    """Add `text_hash` to every top_sql and sessions row that carries text, in place. Rows without text get none,
    so a reader can tell "no text" from "no hash"."""
    for section in ("top_sql", "sessions"):
        for row in summary.get(section) or []:
            if isinstance(row, dict) and row.get("text"):
                row["text_hash"] = text_hash(row["text"])
    return summary
