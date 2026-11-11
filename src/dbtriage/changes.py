"""The change feed: deploys, parameter changes, jobs and failovers around a snapshot.

The feed is fetched once, at capture time, and stored as <snapshot>/changes.json, so the review
and every tool read only the store. A target names its source in a [targets.X.changes] table
(file | github | gitlab); the older `changes_file` key is the file source. Each item is
{time, kind, description, link, source}, oldest first, with kind one of KINDS.

fetch() never raises: a source that fails leaves one `other` item saying so, so the review can
say the feed was unreachable instead of silently having no changes.

webhook: items that arrive through `dbtriage serve` are not a source yet.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dbtriage.egress import urlopen

KINDS = ("deploy", "parameter", "job", "failover", "other")
# what a hand-written changes.json or another tool may call a kind; anything else is "other"
KIND_ALIASES = {"deployment": "deploy", "release": "deploy", "rollout": "deploy", "config": "parameter", "setting": "parameter",
                "settings": "parameter", "maintenance": "job", "stats": "job", "batch": "job", "backup": "job",
                "switchover": "failover", "restart": "failover"}
SOURCES = ("file", "github", "gitlab")
DEFAULT_LOOKBACK_HOURS = 48    # how far before the window the feed is read
DEFAULT_CORRELATE_MINUTES = 30  # a change this long before the window still sits beside its regression
TIMEOUT = 10                   # seconds per request: a slow forge must not hold up the capture
MAX_DETAIL_CALLS = 20          # github needs one call per deployment for its statuses


def spec_of(target) -> dict | None:
    """The target's feed: its [targets.X.changes] table, else its changes_file as a file source, else None."""
    spec = target.extra.get("changes")
    if isinstance(spec, dict) and spec.get("source"):
        return spec
    if target.changes_file:
        return {"source": "file", "path": target.changes_file}
    return None


def lookback(spec: dict | None) -> timedelta:
    return timedelta(hours=float((spec or {}).get("lookback_hours") or DEFAULT_LOOKBACK_HOURS))


def correlate_minutes(spec: dict | None) -> int:
    v = (spec or {}).get("correlate_minutes")
    return DEFAULT_CORRELATE_MINUTES if v in (None, "") else int(v)


def fetch(target, window_start: datetime, window_end: datetime, base_dir: str | None = None) -> list[dict]:
    """Every change between window_start and window_end from the target's feed, oldest first. Never raises."""
    spec = spec_of(target)
    if not spec:
        return []
    source = str(spec.get("source"))
    fn = {"file": _file, "github": _github, "gitlab": _gitlab}.get(source)
    try:
        if fn is None:
            raise ValueError(f"unknown source {source!r}; choose from {', '.join(SOURCES)}")
        items = fn(spec, window_start, window_end, base_dir)
    except Exception as exc:  # noqa: BLE001 - the capture goes on without the feed
        err = f"{type(exc).__name__}: {str(exc)[:200]}" if not isinstance(exc, (ValueError, FileNotFoundError)) else str(exc)[:200]
        print(f"changes feed {source} for {target.name}: {err}", file=sys.stderr)
        return [error_item(source, err, window_end)]
    return within(items, window_start, window_end, source)


def error_item(source: str, err: str, at: datetime) -> dict:
    """The one item a failed source leaves: dated at the window's end so filtering keeps it."""
    return {"time": at.isoformat(), "kind": "other", "description": f"{source}: {err}", "link": None, "source": source, "error": True}


def within(items: list[dict], start: datetime, end: datetime, source: str | None = None) -> list[dict]:
    """Items dated inside [start, end], normalized and oldest first. Items without a readable time are dropped."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        t = parse_time(it.get("time"))
        if t is None or not start <= t <= end:
            continue
        norm = {"time": t.isoformat(), "kind": kind_of(it.get("kind")), "description": str(it.get("description") or "")[:300],
                "link": it.get("link") or None, "source": it.get("source") or source or "file"}
        if it.get("error"):
            norm["error"] = True
        out.append(norm)
    return sorted(out, key=lambda i: i["time"])


def parse_time(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def kind_of(v) -> str:
    k = str(v or "other").strip().lower()
    return k if k in KINDS else KIND_ALIASES.get(k, "other")


def read_file(path: str | Path) -> list[dict]:
    """A hand-kept changes.json: a list of {time, kind, description, link}."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of changes")
    return data


# --------------------------------------------------------------------------- sources
def _file(spec: dict, start: datetime, end: datetime, base_dir: str | None) -> list[dict]:
    path = Path(spec.get("path") or "")
    if not str(path):
        raise ValueError("file source needs path")
    if not path.is_absolute() and base_dir:
        path = Path(base_dir) / path  # relative to targets.toml, like changes_file
    return read_file(path)


def _get(url: str, headers: dict) -> object:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "dbtriage", **headers})
    with urlopen(req, TIMEOUT) as r:  # AWS_CA_BUNDLE when set; the process proxy env still applies
        return json.loads(r.read().decode(errors="replace") or "null")


def _token(spec: dict) -> str | None:
    env = spec.get("token_env")
    if not env:
        return None
    tok = os.environ.get(env)
    if not tok:
        raise ValueError(f"environment variable {env} is not set")
    return tok


def _github(spec: dict, start: datetime, end: datetime, base_dir: str | None) -> list[dict]:
    """GitHub deployments (with their statuses) for one repository; releases instead when asked and there are none."""
    repo = str(spec.get("repo") or "").strip("/")
    if repo.count("/") != 1:
        raise ValueError("github source needs repo = \"owner/name\"")
    api = str(spec.get("base_url") or "https://api.github.com").rstrip("/")
    web = "https://github.com" if api == "https://api.github.com" else api.removesuffix("/api/v3")
    headers = {"Accept": "application/vnd.github+json"}
    if tok := _token(spec):
        headers["Authorization"] = f"Bearer {tok}"
    name = repo.split("/")[1]
    env = spec.get("environment")
    q = {"per_page": 100, **({"environment": env} if env else {})}
    out = []
    for d in _get(f"{api}/repos/{repo}/deployments?{urllib.parse.urlencode(q)}", headers) or []:
        created = parse_time(d.get("created_at"))
        if created is None or created > end:
            continue
        if created < start:
            break  # newest first: everything after this is older still
        ref = d.get("ref") or (d.get("sha") or "")[:8]
        at, state, link = created, None, None
        if len(out) < MAX_DETAIL_CALLS:
            for s in _get(f"{api}/repos/{repo}/deployments/{d['id']}/statuses?per_page=20", headers) or []:
                if state is None:  # newest first: the first one is where the deployment ended up, and when
                    state, link = s.get("state"), s.get("environment_url") or s.get("log_url") or s.get("target_url")
                    at = parse_time(s.get("created_at")) or created
                if s.get("state") == "success":
                    at = parse_time(s.get("created_at")) or created  # when it landed, not when it was requested
                    break
        env_name = d.get("environment") or env or ""
        text = f"{name}@{ref}" + (f" to {env_name}" if env_name else "") + (f" ({state})" if state and state != "success" else "")
        out.append({"time": at.isoformat(), "kind": "deploy", "description": text,
                    "link": link or (f"{web}/{repo}/commit/{d['sha']}" if d.get("sha") else None), "source": "github"})
    if out or not spec.get("releases"):
        return out
    for r in _get(f"{api}/repos/{repo}/releases?per_page=50", headers) or []:  # cheaper, and all a repo without deployments has
        at = parse_time(r.get("published_at") or r.get("created_at"))
        if at is None or r.get("draft"):
            continue
        out.append({"time": at.isoformat(), "kind": "deploy", "description": f"{name}@{r.get('tag_name') or r.get('name')} released",
                    "link": r.get("html_url"), "source": "github"})
    return out


def _gitlab(spec: dict, start: datetime, end: datetime, base_dir: str | None) -> list[dict]:
    """GitLab deployments for one project (an id or a group/project path)."""
    project = str(spec.get("project") or "").strip("/")
    if not project:
        raise ValueError("gitlab source needs project = \"group/name\" or an id")
    base = str(spec.get("base_url") or "https://gitlab.com").rstrip("/")
    headers = {}
    if tok := _token(spec):
        headers["PRIVATE-TOKEN"] = tok
    name = project.rsplit("/", 1)[-1] if not project.isdigit() else f"project {project}"
    q = {"updated_after": start.isoformat(), "updated_before": end.isoformat(), "order_by": "updated_at", "sort": "asc", "per_page": 100,
         **({"environment": spec["environment"]} if spec.get("environment") else {})}
    out = []
    for d in _get(f"{base}/api/v4/projects/{urllib.parse.quote(project, safe='')}/deployments?{urllib.parse.urlencode(q)}", headers) or []:
        job = d.get("deployable") or {}
        at = parse_time(job.get("finished_at")) or parse_time(d.get("updated_at")) or parse_time(d.get("created_at"))
        if at is None:
            continue
        status = d.get("status")
        env_name = (d.get("environment") or {}).get("name") or spec.get("environment") or ""
        text = f"{name}@{d.get('ref') or (d.get('sha') or '')[:8]}" + (f" to {env_name}" if env_name else "") \
            + (f" ({status})" if status and status != "success" else "")
        link = job.get("web_url") or (job.get("pipeline") or {}).get("web_url") or (d.get("environment") or {}).get("external_url")
        out.append({"time": at.isoformat(), "kind": "deploy", "description": text, "link": link, "source": "gitlab"})
    return out
