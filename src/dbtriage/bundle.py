"""One snapshot as a zip a postmortem or an auditor can take away.

The bundle is the stored record, nothing more: the manifest (what triggered the run and what it was
compared with), the comparison, the review with its agreement marks and life, the model's tool
transcript, the change feed, the engine reports and the discussion threads, each byte for byte as the
store holds it, plus a README that reads the record for a person and a checksums.txt that lets the
recipient show nothing was altered since export. Nothing here opens a database or a network: build()
only reads the store.

Stored files are already redacted (dbtriage.redact masks literals before storage), and the manifest
carries the target's name, never its connection string. The bundle still checks: scrub() drops any
secret-named key from the manifest, and mask() walks every text member for credential-shaped values
(a user:password in a URL, a password= pair, a bearer token, a cloud access key) and replaces each
with ***. The README says exactly what was dropped and masked, and claims nothing beyond that.

The zip is deterministic for the same inputs: fixed timestamps, sorted members, one compression level,
one creating system. Two exports of an unchanged snapshot are byte-identical, which is what makes a
checksum of the bundle itself worth writing down.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Callable

from dbtriage.config import target_name_ok
from dbtriage.storage import Storage, StorageUnavailable, key_ok

# the stored files of a snapshot, in the order the README lists them; raw.json is the engine-native payload and
# can be large, so it is in the bundle only on request
KNOWN = {
    "manifest.json": "what triggered the run, its window, the baseline it was compared with and why, and the files it stored",
    "summary.json": "the engine-neutral snapshot: load profile, top SQL (literals masked), waits, settings",
    "raw.json": "the engine-native payload the summary was built from (literals masked)",
    "compare.json": "the comparison with the baseline and the rules findings",
    "triage.json": "the review: findings with agreement marks and life, heuristics, model, token usage, tool-call count, delivery",
    "triage.md": "the review as delivered",
    "tools.json": "every tool call the model made, with its arguments and the first 2000 characters of each result",
    "changes.json": "the deploys and changes read from the target's feed for the window",
    "awr.txt": "Oracle AWR report (literals masked)",
    "addm.txt": "Oracle ADDM report (literals masked)",
}
OWN = ("README.md", "checksums.txt")  # written by build(), never read from the store
MARK_LABEL = {"both": "both", "model_only": "model only", "rules_only": "rules only"}
LIFE_LABEL = {"new": "new", "still_open": "still open", "worse": "worse", "quiet": "quiet", "returned": "returned",
              "recurring": "recurring", "gone": "gone", "untracked": "untracked"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # the zip epoch: the bundle's date is in its README, not in its members
ZIP_SYSTEM = 3  # "Unix" in every member header whatever the exporting host, so Windows and Linux exports match

# A key is secret-named when its last segment is one of these words: `password`, `db_password`, `feed.api_token` and
# `x-api-key` are dropped, while `token_env`, `secret_name` or `password_file` name where a secret lives and are kept.
_SECRET_KEY = re.compile(r"(?:.*[._-])?(?:dsn|passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key(?:[_-]id)?|secret[_-]?key|"
                         r"credentials?|authorization|private_key)", re.I)
# credential-shaped values, each replaced by *** wherever it appears in a text member
_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s@\"'<>]+@", re.I)  # scheme://user:pass@host
# password=x, "token": "x", 'x-api-key': 'x'. The key may carry `_`- or `-`-joined segments before the secret word
# (db_password, x-api-key) but nothing after it, so token_env and input_tokens are not keys. A quoted value runs to
# the closing quote of its own kind, escapes included, so "two words" is masked whole; a bare value is a run that
# stops at whitespace and at the delimiters a DSN, URL query or JSON string puts after it, so the member stays valid.
_KEYWORD = re.compile(r"(?P<key>\b(?:[a-z0-9]+[_-])*(?:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key(?:[_-]id)?|secret[_-]?key)"
                      r"\b[\"']?\s*[=:]\s*)(?:\"(?P<dq>(?:[^\"\\]|\\.)+)\"|'(?P<sq>(?:[^'\\]|\\.)+)'|(?P<bare>[^\s;&,\"'<>}\]]+))", re.I)
_BEARER = re.compile(r"(?P<key>\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
_GH_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")  # GitHub personal, app and fine-grained tokens


@dataclass
class Record:
    """What members() read and build() changed, so the README states exactly what the bundle holds."""
    files: dict[str, bytes]
    listing_failed: bool = False  # the store refused to list: files only the listing names may be missing
    degraded: str | None = None  # the store was partly reachable (a spool stood in for it): the listing may be partial
    unchecked: list[str] = field(default_factory=list)  # known names nothing evidenced, unreadable while the store was degraded
    hidden: list[str] = field(default_factory=list)  # stored, but the caller's allowed() said no
    dropped: list[str] = field(default_factory=list)  # secret-named manifest keys, by path
    masked: dict[str, int] = field(default_factory=dict)  # credential-shaped values replaced, per member


def scrub(obj, dropped: list[str] | None = None, path: str = ""):
    """A copy of a JSON object with secret-named keys removed. `dropped` collects the paths taken out, so the
    README can say what was withheld rather than hide it."""
    if dropped is None:
        dropped = []
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if _SECRET_KEY.fullmatch(str(k)):
                dropped.append(here)
                continue
            out[k] = scrub(v, dropped, here)
        return out
    if isinstance(obj, list):
        return [scrub(v, dropped, f"{path}[{i}]") for i, v in enumerate(obj)]
    return obj


def mask(text: str) -> tuple[str, int]:
    """The text with every credential-shaped value replaced by ***, and how many were replaced."""
    n = 0

    def sub(pattern: re.Pattern, repl, s: str) -> str:
        nonlocal n
        s, k = pattern.subn(repl, s)
        n += k
        return s

    def keyword(m: re.Match) -> str:  # keep the quotes the value had, so a JSON member stays JSON
        q = '"' if m.group("dq") is not None else "'" if m.group("sq") is not None else ""
        return f"{m.group('key')}{q}***{q}"

    text = sub(_USERINFO, r"\g<scheme>***@", text)
    text = sub(_KEYWORD, keyword, text)
    text = sub(_BEARER, r"\g<key>***", text)
    text = sub(_AWS_KEY, "***", text)
    text = sub(_SK_TOKEN, "***", text)
    text = sub(_GH_TOKEN, "***", text)
    return text, n


def _segment(name, fallback: str) -> str:
    # the storage-key alphabet: nothing in it is special to a path or a Content-Disposition header
    return name if target_name_ok(name) else fallback


def filename(manifest: dict, sid: str) -> str:
    """<engine>-<target>-<id>.zip, the default name on disk and in the download header. Each part is kept to
    the storage-key alphabet (letters, digits, '.', '_', '-'), so no quote, slash or newline can reach either."""
    return (f"{_segment(manifest.get('engine'), 'snapshot')}-{_segment(manifest.get('target'), 'snapshot')}"
            f"-{_segment(sid.rsplit('/', 1)[-1], 'snapshot')}.zip")


def members(store: Storage, sid: str, include_raw: bool = False, allowed: Callable[[str], bool] = lambda name: True) -> Record:
    """Every stored file of the snapshot by its name relative to the snapshot (chat/<key>.json for threads).
    The listing is the source of truth; the known names and the manifest's files and reports are tried as well,
    so an engine report never goes missing from a store that cannot list. Each member is read with get(), never
    gated on exists(): a store whose exists() answers False during trouble must not quietly thin the record.
    Raises FileNotFoundError for an unknown snapshot. StorageUnavailable passes through for a file the listing or
    the manifest names, since a record without it would be wrong; for a known name nothing names (a Postgres
    snapshot has no awr.txt) it only means the name could not be checked, which the record says instead."""
    if not key_ok(sid):
        raise FileNotFoundError(sid)
    manifest = store.get_json(f"{sid}/manifest.json")  # FileNotFoundError when the snapshot is not there
    evidenced = {"manifest.json"} | set(manifest.get("reports") or []) | set(manifest.get("files") or [])
    rec = Record(files={})
    try:
        evidenced |= {k[len(sid) + 1:] for k in store.list_keys(f"{sid}/") if k.startswith(f"{sid}/")}
    except StorageUnavailable:
        raise
    except Exception:  # noqa: BLE001 - a store that refuses to list (ListBucket denied, say) still yields the named files
        rec.listing_failed = True
    # a resilient store lists its spool alone while the remote is down, and says so in `degraded` rather than raising
    rec.degraded = getattr(store, "degraded", None) or None
    for name in sorted(evidenced | set(KNOWN)):
        if not name or name.startswith(".") or name in OWN or not key_ok(f"{sid}/{name}"):
            continue
        if name == "raw.json" and not include_raw:
            continue
        if not allowed(name):
            if name in evidenced:  # the listing or the manifest says it exists; a mere probe name is not worth naming
                rec.hidden.append(name)
            continue
        try:
            rec.files[name] = store.get(f"{sid}/{name}")
        except FileNotFoundError:
            continue
        except StorageUnavailable as exc:
            if name in evidenced:  # the record names it: exporting without it would be a wrong record, not a thinner one
                raise
            rec.unchecked.append(name)  # a probe only: the store is down and this name could not be checked
            rec.degraded = rec.degraded or str(exc)
    return rec


def _json(data: bytes | None):
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _finding_rows(tr: dict) -> list[str]:
    marks = {r["model"]: MARK_LABEL.get(r.get("mark"), r.get("mark", "")) for r in tr.get("agreement") or [] if isinstance(r, dict) and r.get("model") is not None}
    lifes = tr.get("life") if isinstance(tr.get("life"), list) else []
    rows = ["| # | Severity | Agreement | Life | Where | Finding | Tickets |", "|---|---|---|---|---|---|---|"]
    for i, f in enumerate(tr.get("findings") or [], 1):
        life = lifes[i - 1] if i <= len(lifes) and isinstance(lifes[i - 1], dict) else {}
        state = LIFE_LABEL.get(life.get("state"), life.get("state") or "")
        if state and life.get("first_seen_at") and life.get("state") != "new":
            state += f" since {str(life['first_seen_at'])[:10]}"
        tickets = ", ".join(f"{name} {t['key']}" + (f" ({t['url']})" if t.get("url") else "")
                            for name, t in sorted((life.get("tickets") or {}).items()) if isinstance(t, dict) and t.get("key"))
        where = f.get("where") or " ".join(x for x in (f.get("kind"), f.get("subject")) if x)
        rows.append(f"| {i} | {f.get('severity', '')} | {marks.get(i - 1, '') if tr.get('source') == 'agent' else 'rules'} | {state} "
                    f"| {_cell(where)} | {_cell(f.get('summary') or f.get('title') or '')} | {_cell(tickets)} |")
    return rows


def _cell(text) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _tokens(tr: dict) -> str:
    u = tr.get("usage") or {}
    if u.get("input_tokens") is None and u.get("output_tokens") is None:
        return "token usage not recorded"
    total = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
    out = f"{total:,} tokens ({u.get('input_tokens') or 0:,} in, {u.get('output_tokens') or 0:,} out)"
    if u.get("budget_reached"):
        out += f"; the token budget of {u.get('token_budget', 0):,} was reached"
    return out


def readme(sid: str, rec: Record, include_raw: bool) -> str:
    """The index a person reads first: what this is, what triggered it, what it was compared with, the review's
    findings with their marks and life, the model and its cost, where it was delivered, the discussion, and the
    file list with checksums. Written from the stored files only, so it says exactly what the bundle holds."""
    files = rec.files
    m = _json(files.get("manifest.json")) or {}
    tr = _json(files.get("triage.json")) or {}
    cmp_ = _json(files.get("compare.json")) or {}
    tools = _json(files.get("tools.json")) or {}
    changes = _json(files.get("changes.json"))
    threads = {n[5:-5]: _json(d) or {} for n, d in files.items() if n.startswith("chat/") and n.endswith(".json")}
    win = m.get("window") or {}
    trig = m.get("trigger") or {}
    base = m.get("baseline") or {}
    L = [f"# Second Look snapshot {sid}", "",
         f"Target `{m.get('target', '?')}` ({m.get('engine', '?')}), captured {m.get('captured_at', '?')}.",
         "This bundle is the stored record of one review, exported from the snapshot store. Nothing in it was read "
         "from a live database or from the network at export time, and SQL literals were masked before anything was stored.", ""]

    L += ["## What triggered it", ""]
    L.append(f"- Mode: {win.get('mode', '?')}" + (f" from {win['start']} to {win['end']}" if win.get("start") and win.get("end") else ""))
    L.append(f"- Source: {trig.get('source') or 'unknown'}" + (f", by {trig['actor']}" if trig.get("actor") else ""))
    L.append(f"- Reason: {trig.get('reason') or 'none given'}")
    if m.get("changes_sources"):
        L.append(f"- Change feed read at capture: {', '.join(m['changes_sources'])}"
                 + (f" ({len(changes)} entr{'y' if len(changes) == 1 else 'ies'} in changes.json)" if isinstance(changes, list) else ""))
    elif isinstance(changes, list):
        L.append(f"- Change feed: {len(changes)} entr{'y' if len(changes) == 1 else 'ies'} in changes.json")
    if (ds := m.get("db_snapshot")) and isinstance(ds, dict):
        L.append(f"- Snapshot on the database: {ds.get('status', '?')}" + (f" ({ds['reason']})" if ds.get("reason") else ""))
    L.append("")

    L += ["## What it was compared with", ""]
    if base.get("snapshot"):
        L.append(f"- Baseline `{base['snapshot']}`, policy {base.get('policy') or 'previous'}: {base.get('reason') or ''}".rstrip(": "))
    else:
        L.append("- No baseline: " + (base.get("reason") or "nothing earlier to compare with"))
    if cmp_:
        rules = cmp_.get("findings") or []
        L.append(f"- Rules findings in compare.json: {len(rules)}" + (" (" + ", ".join(sorted({f.get('kind') or f.get('title') or '?' for f in rules})) + ")" if rules else ""))
        if (sc := cmp_.get("settings_changed")) is not None:
            L.append(f"- Parameters changed since the baseline: {len(sc)}" + (" (" + ", ".join(str(s.get('name', s) if isinstance(s, dict) else s) for s in sc[:12]) + ")" if sc else ""))
    else:
        L.append("- No compare.json: the snapshot was not compared")
    L.append("")

    L += ["## The review", ""]
    if not tr:
        L.append("Not reviewed: there is no triage.json.")
    else:
        L.append(f"**{tr.get('severity', '?')}** · {tr.get('title') or 'Review'}" + (f" · confidence {tr['confidence']}" if tr.get("confidence") else ""))
        if tr.get("summary"):
            L += ["", tr["summary"]]
        L.append("")
        if tr.get("source") == "agent":
            calls = tools.get("calls") if isinstance(tools.get("calls"), list) else None
            n_calls = len(calls) if calls is not None else tr.get("tool_calls")
            L.append(f"- Reviewer: model `{tr.get('model') or '?'}` via {tr.get('provider') or '?'}; {_tokens(tr)}"
                     + (f"; {n_calls} tool call{'s' if n_calls != 1 else ''}" if n_calls is not None else "")
                     + (" (the transcript is tools.json)" if calls is not None else "")
                     + (f"; {tr['duration_s']}s" if tr.get("duration_s") is not None else ""))
            if calls:
                names = sorted({c.get("tool") or "?" for c in calls if isinstance(c, dict)})
                L.append(f"- Tools used: {', '.join(names)}")
        else:
            L.append("- Reviewer: the rules (no model review)" + (f"; the model failed: {str(tr['agent_error'])[:200]}" if tr.get("agent_error") else ""))
        if tr.get("checked"):
            L.append(f"- Checked: {tr['checked']}")
        L.append("")
        if tr.get("findings"):
            L += ["### Findings", ""]
            L += _finding_rows(tr)
            L.append("")
            L.append("Agreement: *both* means a rule found the same thing, *model only* that the model added it. "
                     "Life is the finding's state in the target's ledger at the time of the review.")
        else:
            L.append("No findings.")
        L.append("")
        rows = tr.get("agreement") if isinstance(tr.get("agreement"), list) else None
        if rows is not None:
            n = {k: sum(1 for r in rows if r.get("mark") == k) for k in MARK_LABEL}
            L.append(f"Agreement with the rules: {n['both']} both, {n['model_only']} model only, {n['rules_only']} rules only.")
            heur = tr.get("heuristics") or []
            only = [r for r in rows if r.get("mark") == "rules_only"]
            if only:
                L += ["", "Rules found, not in the review:", ""]
                for r in only:
                    h = heur[r["rules"]] if r.get("rules") is not None and r["rules"] < len(heur) else {}
                    L.append(f"- {r.get('severity', '')} `{r.get('kind', '')}` {h.get('title') or r.get('subject') or ''}".rstrip())
            L.append("")
        still, gone = tr.get("still_open") or [], tr.get("gone") or []
        if still or gone:
            L += ["### Still open and cleared", ""]
            for e in still:
                L.append(f"- Still open: {e.get('severity', '')} `{e.get('kind', '')} {e.get('subject', '')}`".rstrip("` ") + "`" + (f" {e.get('title')}" if e.get("title") else "")
                         + (f", since {str(e['first_seen_at'])[:10]}" if e.get("first_seen_at") else ""))
            for e in gone:
                L.append(f"- Cleared this run: `{e.get('kind', '')} {e.get('subject', '')}`".rstrip("` ") + "`" + (f" {e.get('title')}" if e.get("title") else ""))
            L.append("")
        L += ["### Delivery and tickets", ""]
        delivered = tr.get("delivered")
        if isinstance(delivered, list) and delivered:
            L.append(f"- Delivered to: {', '.join(str(d) for d in delivered)}")
        elif delivered is not None:
            L.append("- Delivered to: no channel")
        else:
            L.append("- Delivery: not recorded (the review was not delivered from this run)")
        tickets = []
        for i, life in enumerate(tr.get("life") or [], 1):
            for name, t in sorted(((life or {}).get("tickets") or {}).items()):
                if isinstance(t, dict) and t.get("key"):
                    tickets.append(f"- Finding {i}: {name} {t['key']}" + (f" ({t['url']})" if t.get("url") else ""))
        L += tickets or ["- Tickets: none"]
        L.append("")

    L += ["## Discussion", ""]
    for key in sorted(threads):
        msgs = threads[key].get("messages") or []
        qs = [x for x in msgs if x.get("role") == "user"]
        last = max((x.get("at") or "" for x in msgs), default="")
        about = "the whole review" if key == "review" else f"finding {key[8:]}" if key.startswith("finding-") else f"SQL id {key[4:]}"
        L.append(f"- `chat/{key}.json`: {len(qs)} question{'s' if len(qs) != 1 else ''} about {about}" + (f", last at {last}" if last else ""))
    if rec.listing_failed:  # threads are found only by listing, so without one their absence means nothing
        L.append("The store could not be listed at export time, so discussion threads" + (" beyond those above" if threads else "")
                 + " and any other file not named in the manifest may be missing from this bundle.")
    elif rec.degraded:
        L.append(f"The store was degraded at export time ({rec.degraded}), so the listing may be partial: discussion threads"
                 + (" beyond those above" if threads else "") + " and any other file not named in the manifest may be missing from this bundle.")
    elif not threads:
        L.append("No discussion threads were stored with this snapshot.")
    L.append("")

    L += ["## Files", ""]
    for name in sorted(files):
        if name.startswith("chat/"):
            n = len((threads.get(name[5:-5]) or {}).get("messages") or [])
            desc = f"discussion thread ({n} message{'s' if n != 1 else ''})"
        elif name in KNOWN:
            desc = KNOWN[name] + ("; included on request" if name == "raw.json" else "")
        elif name in (m.get("reports") or []):
            desc = "engine report (literals masked)"
        else:
            desc = "stored with the snapshot"
        L.append(f"- `{name}`: {desc}")
    L.append("- `checksums.txt`: SHA-256 of every file above; `sha256sum -c checksums.txt` shows nothing was altered")
    L.append("")
    L += ["## What is not here", ""]
    if "raw.json" not in files and "raw.json" not in rec.hidden:
        L.append("- `raw.json`, the engine-native payload: " + ("not stored with this snapshot" if include_raw else
                 "it can be large and the summary is built from it; export with `--raw` to include it"))
    if rec.hidden:
        L.append(f"- Hidden by the server's `[web] files` setting, so not exported from the web: {', '.join(f'`{n}`' for n in rec.hidden)}")
    if rec.listing_failed:
        L.append("- Whatever only a listing of the store would have named: the store could not be listed at export time")
    elif rec.degraded:
        L.append(f"- Whatever only a full listing of the store would have named: the store was degraded at export time ({rec.degraded})")
    if rec.unchecked:
        L.append(f"- Not checked: {', '.join(f'`{n}`' for n in sorted(rec.unchecked))}: neither the listing nor the manifest names them, "
                 "and the store was degraded at export time, so whether they exist could not be read")
    if rec.dropped:
        L.append(f"- Secret-named keys withheld from manifest.json: {', '.join(sorted(rec.dropped))}")
    if rec.masked:
        L.append("- Credential-shaped values were masked (replaced by `***`): "
                 + ", ".join(f"{n} in {name}" for name, n in sorted(rec.masked.items())))
    elif not rec.dropped:
        L.append("- Credentials: the scan for secret-named manifest keys and credential-shaped values found nothing to mask")
    L.append("- SQL literals: masked before storage, so the SQL text here is the shape of each statement, not its values")
    L.append("- Anything from the live database at export time: the bundle is the stored record only")
    L.append("")
    return "\n".join(L)


def build(store: Storage, sid: str, include_raw: bool = False, allowed: Callable[[str], bool] = lambda name: True) -> bytes:
    """The zip for one snapshot. `allowed(name)` is the caller's gate on members (the web passes the file drawer's
    rule; the CLI allows everything). Raises FileNotFoundError for an unknown snapshot; StorageUnavailable passes
    through, so an outage is never exported as a thinner record."""
    rec = members(store, sid, include_raw, allowed)
    files = rec.files
    manifest = _json(files.get("manifest.json"))
    if isinstance(manifest, dict):
        clean = scrub(manifest, rec.dropped)
        if rec.dropped:  # rewrite only when something was taken out, so an honest manifest stays byte for byte
            files["manifest.json"] = json.dumps(clean, indent=1, sort_keys=True, default=str).encode()
    for name in sorted(files):
        try:
            text = files[name].decode()
        except UnicodeDecodeError:  # not text: nothing to scan
            continue
        text, n = mask(text)
        if n:
            files[name], rec.masked[name] = text.encode(), n
    files["README.md"] = readme(sid, rec, include_raw).encode()
    sums = "".join(f"{hashlib.sha256(files[n]).hexdigest()}  {n}\n" for n in sorted(files))
    files["checksums.txt"] = sums.encode()
    order = list(OWN) + sorted(n for n in files if n not in OWN)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in order:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.create_system = ZIP_SYSTEM
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name], compresslevel=9)
    return buf.getvalue()
