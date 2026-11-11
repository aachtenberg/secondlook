"""Who opened the web view, when a reverse proxy says so.

This is not the bank access portal and it is not AWS. The portal issues cloud keys.
dbtriage does not collect MFA and does not read ~/.aws/credentials here.

Headers are ignored unless DBTRIAGE_TRUST_PROXY is set, because a browser can send
X-Forwarded-User itself. The proxy in front of the web view must overwrite those headers.
Group lists, comma-separated, narrow view / run / edit / chat. The process flags
(--allow-run, --allow-edit, --allow-chat) stay the ceiling: a group cannot turn on
something the process was started without. Unset group lists change nothing.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field

_local = threading.local()
_SPLIT = re.compile(r"[,;]")


@dataclass(frozen=True)
class Actor:
    name: str = ""
    groups: frozenset[str] = field(default_factory=frozenset)


def trust_proxy() -> bool:
    return os.environ.get("DBTRIAGE_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:128]


def _groups(value: str) -> frozenset[str]:
    return frozenset(p.strip() for p in _SPLIT.split(value) if p.strip())


def groups_required(cap: str) -> frozenset[str]:
    if not trust_proxy():
        return frozenset()
    raw = os.environ.get(f"DBTRIAGE_GROUP_{cap.upper()}", "")
    return _groups(raw)


def actor_from(headers, basic_user: str = "") -> Actor:
    """The person this request is acting as. Trusted-proxy headers only when the operator
    turned them on; otherwise the HTTP Basic username, which is empty when there is no token."""
    if trust_proxy():
        user_h = os.environ.get("DBTRIAGE_PROXY_USER_HEADER") or "X-Forwarded-User"
        groups_h = os.environ.get("DBTRIAGE_PROXY_GROUPS_HEADER") or "X-Forwarded-Groups"
        return Actor(_clean(headers.get(user_h) or ""), _groups(headers.get(groups_h) or ""))
    return Actor(_clean(basic_user), frozenset())


def set_current(actor: Actor) -> None:
    _local.actor = actor


def clear_current() -> None:
    _local.actor = Actor()


def current() -> Actor:
    return getattr(_local, "actor", Actor())


def permits(actor: Actor, cap: str, enabled: bool) -> bool:
    """enabled is the process flag (view is always enabled). A configured group list then
    requires membership. No list means the process flag is the whole rule."""
    if cap != "view" and not enabled:
        return False
    needed = groups_required(cap)
    if not needed:
        return True if cap == "view" else enabled
    return bool(actor.groups & needed)


def audit(action: str, detail: str) -> None:
    """One line when a person is known. No line for anonymous loopback use, and never a secret."""
    actor = current()
    if not actor.name:
        return
    who = actor.name.replace("\n", " ")
    detail = detail.replace("\n", " ")[:180]
    print(f"audit: {who} {action} {detail}")
