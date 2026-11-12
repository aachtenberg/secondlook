"""One snapshot as a zip a postmortem or an auditor can take away.

The bundle is the stored record, nothing more: the manifest (what triggered the run and what it was
compared with), the comparison, the review with its agreement marks and life, the model's tool
transcript, the change feed, the engine reports and the discussion threads, each byte for byte as the
store holds it, plus a README that reads the record for a person and a checksums.txt that lets the
recipient show nothing was altered since export. Nothing here opens a database or a network: build()
only reads the store.

Stored files are already redacted (dbtriage.redact masks literals before storage), and the manifest
carries the target's name, never its connection string. scrub() still walks the manifest and drops any
secret-looking key or credentialed URL, so a manifest from a future or patched collector cannot leak.

The zip is deterministic for the same inputs: fixed timestamps, sorted members, one compression level.
Two exports of an unchanged snapshot are byte-identical, which is what makes a checksum of the bundle
itself worth writing down.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile

from dbtriage.storage import Storage, key_ok

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
MARK_LABEL = {"both": "both", "model_only": "model only", "rules_only": "rules only"}
LIFE_LABEL = {"new": "new", "still_open": "still open", "worse": "worse", "quiet": "quiet", "returned": "returned",
              "recurring": "recurring", "gone": "gone", "untracked": "untracked"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # the zip epoch: the bundle's date is in its README, not in its members

_SECRET_KEY = re.compile(r"dsn|passw|secret|token|api_?key|credential|authorization|private_key", re.I)
_CREDENTIALED_URL = re.compile(r"^[a-z][a-z0-9+.-]*://[^/\s@]+@", re.I)  # scheme://user:pass@host


def scrub(obj, dropped: list[str] | None = None, path: str = ""):
    """A copy of a JSON object with secret-looking keys and credentialed URLs removed. `dropped` collects
    the paths taken out, so the README can say what was withheld rather than hide it."""
    if dropped is None:
        dropped = []
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if _SECRET_KEY.search(str(k)):
                dropped.append(here)
                continue
            out[k] = scrub(v, dropped, here)
        return out
    if isinstance(obj, list):
        return [scrub(v, dropped, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, str) and _CREDENTIALED_URL.match(obj):
        dropped.append(path or "value")
        return "[credentialed url dropped]"
    return obj


def filename(manifest: dict, sid: str) -> str:
    """<engine>-<target>-<id>.zip, the default name on disk and in the download header."""
    return f"{manifest.get('engine', 'db')}-{manifest.get('target', 'target')}-{sid.rsplit('/', 1)[-1]}.zip"


def members(store: Storage, sid: str, include_raw: bool = False) -> dict[str, bytes]:
    """Every stored file of the snapshot by its name relative to the snapshot (chat/<key>.json for threads).
    The listing is the source of truth; the known names and the manifest's reports are added for a store that
    cannot list, so an engine report never goes missing from the record."""
    if not key_ok(sid) or not store.exists(f"{sid}/manifest.json"):
        raise FileNotFoundError(sid)
    manifest = store.get_json(f"{sid}/manifest.json")
    names = set(KNOWN) | set(manifest.get("reports") or []) | set(manifest.get("files") or [])
    try:
        names |= {k[len(sid) + 1:] for k in store.list_keys(f"{sid}/")}
    except Exception:  # noqa: BLE001 - a store that cannot list still yields the known files
        pass
    out = {}
    for name in sorted(names):
        if not name or name.startswith(".") or name in ("README.md", "checksums.txt"):
            continue
        if name == "raw.json" and not include_raw:
            continue
        if not key_ok(f"{sid}/{name}") or not store.exists(f"{sid}/{name}"):
            continue
        out[name] = store.get(f"{sid}/{name}")
    return out


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


def readme(sid: str, files: dict[str, bytes], dropped: list[str], include_raw: bool) -> str:
    """The index a person reads first: what this is, what triggered it, what it was compared with, the review's
    findings with their marks and life, the model and its cost, where it was delivered, the discussion, and the
    file list with checksums. Written from the stored files only, so it says exactly what the bundle holds."""
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
         "from a live database or from the network at export time, SQL literals were masked before anything was stored, "
         "and no connection string, key or password is included.", ""]

    L += ["## What triggered it", ""]
    L.append(f"- Mode: {win.get('mode', '?')}" + (f", a {win['minutes']}-minute window" if win.get("minutes") else "")
             + (f" from {win['start']} to {win['end']}" if win.get("start") and win.get("end") else ""))
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
                names = sorted({c.get("name") or c.get("tool") or "?" for c in calls if isinstance(c, dict)})
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
    if threads:
        for key in sorted(threads):
            msgs = threads[key].get("messages") or []
            qs = [x for x in msgs if x.get("role") == "user"]
            last = max((x.get("at") or "" for x in msgs), default="")
            about = "the whole review" if key == "review" else f"finding {key[8:]}" if key.startswith("finding-") else f"SQL id {key[4:]}"
            L.append(f"- `chat/{key}.json`: {len(qs)} question{'s' if len(qs) != 1 else ''} about {about}" + (f", last at {last}" if last else ""))
    else:
        L.append("No discussion threads were stored with this snapshot.")
    L.append("")

    L += ["## Files", ""]
    for name in sorted(files):
        if name.startswith("chat/"):
            n = len((threads.get(name[5:-5]) or {}).get("messages") or [])
            desc = f"discussion thread ({n} message{'s' if n != 1 else ''})"
        elif name in KNOWN:
            desc = KNOWN[name]
        elif name in (m.get("reports") or []):
            desc = "engine report (literals masked)"
        else:
            desc = "stored with the snapshot"
        L.append(f"- `{name}`: {desc}")
    L.append("- `checksums.txt`: SHA-256 of every file above; `sha256sum -c checksums.txt` shows nothing was altered")
    L.append("")
    L += ["## What is not here", ""]
    L.append("- `raw.json`, the engine-native payload" + (" (included on request)" if include_raw else ": it can be large and the summary is built from it; export with `--raw` to include it"))
    L.append("- Connection strings, keys and passwords: the store never holds them" + (f"; also withheld from manifest.json: {', '.join(dropped)}" if dropped else ""))
    L.append("- SQL literals: masked before storage, so the SQL text here is the shape of each statement, not its values")
    L.append("- Anything from the live database at export time: the bundle is the stored record only")
    L.append("")
    return "\n".join(L)


def build(store: Storage, sid: str, include_raw: bool = False) -> bytes:
    """The zip for one snapshot. Raises FileNotFoundError for an unknown snapshot."""
    files = members(store, sid, include_raw)
    dropped: list[str] = []
    manifest = _json(files.get("manifest.json"))
    if isinstance(manifest, dict):
        clean = scrub(manifest, dropped)
        if dropped:  # rewrite only when something was taken out, so an honest manifest stays byte for byte
            files["manifest.json"] = json.dumps(clean, indent=1, sort_keys=True, default=str).encode()
    files["README.md"] = readme(sid, files, dropped, include_raw).encode()
    sums = "".join(f"{hashlib.sha256(files[n]).hexdigest()}  {n}\n" for n in sorted(files))
    files["checksums.txt"] = sums.encode()
    order = ["README.md", "checksums.txt"] + sorted(n for n in files if n not in ("README.md", "checksums.txt"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in order:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name], compresslevel=9)
    return buf.getvalue()
