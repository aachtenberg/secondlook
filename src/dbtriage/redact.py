"""Mask literals in SQL text before it leaves the database host or reaches the model."""
from __future__ import annotations

import re

_STRING = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"(?<![\w$])[-+]?\d+(?:\.\d+)?(?![\w$])")
_WS = re.compile(r"\s+")


def redact_sql(text: str | None, max_len: int = 2000) -> str:
    """Replace string and numeric literals with '?' and collapse whitespace.

    pg_stat_statements already normalizes to $1, $2 ...; this additionally covers
    pg_stat_activity, slow logs, and any engine that keeps literals in the text.
    """
    if not text:
        return ""
    out = _STRING.sub("'?'", text)
    out = _NUMBER.sub("?", out)
    out = _WS.sub(" ", out).strip()
    if len(out) > max_len:
        out = out[:max_len] + " ..."
    return out


_STRING_ON_LINE = re.compile(r"'(?:[^'\n]|'')*'")


def mask_report_literals(text: str | None) -> str:
    """Mask quoted literals in an engine's text report (AWR, ADDM) without touching its layout: numbers there are
    mostly statistics, so only quoted strings within one line become '?'. SQL in the report's SQL sections can carry
    customer values when an application does not use bind variables; this keeps them out of storage and the model."""
    if not text:
        return text or ""
    return _STRING_ON_LINE.sub("'?'", text)
