"""UI-editable configuration, stored as one JSON file next to the config (default
<config dir>/dbtriage-settings.json, override with DBTRIAGE_SETTINGS or [web].settings_file).

targets.toml stays the seed that ops can commit; this file is the overlay the web UI writes:
targets added or edited in the UI, a "deleted" marker to hide a seeded target, and [web] branding
overrides. The overlay wins over the file. JSON rather than SQLite on purpose: stdlib, diffable,
trivially backed up, and the data is a few dozen records.

Shared settings for several hosts: point DBTRIAGE_SETTINGS (or [web].settings_file) at a postgres:// URL and the
overlay lives in table dbtriage_settings instead. The database is then never a single point of failure: every
successful read refreshes a last-good copy next to the config (dbtriage-settings.cache.json, 0600) which the app
runs on while the database is down, and a save during an outage is refused with SettingsUnavailable rather than
written somewhere the other hosts cannot see.

Secrets: prefer dsn_env (name of an environment variable) over dsn. A dsn typed into the UI is
stored in this file with mode 0600 and never shown again in full (masked in pages and the API).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from dbtriage import dbsnap, scheduler
from dbtriage.baseline import POLICIES
from dbtriage.changes import SOURCES
from dbtriage.config import ENGINES, NAME_RULE, Config, Target, target_name_ok, top_n_of  # one rule for file, UI and overlay
from dbtriage.llm import DEFAULT_TOKEN_BUDGET, scheduled_problem

TARGET_FIELDS = ("engine", "dsn", "dsn_env", "top_n", "diagnostics_pack", "changes_file", "changes", "create_snapshot", "user", "password", "links", "notes",
                 "baseline_policy", "baseline", "schedule", "schedule_window", "schedule_paused",
                 "call_timeout_ms", "connect_timeout_s", "min_snapshot_age_min", "mask_report_literals", "band_weeks")
SCHEDULE_FIELDS = ("schedule", "schedule_window", "schedule_paused")
# per-target numbers the edit page offers: (blank-means default, smallest, largest a save accepts, unit)
TARGET_NUMBERS = {"top_n": (25, 0, 10_000, ""), "schedule_window": (60, 5, 1440, "minutes"), "call_timeout_ms": (None, 1000, 3_600_000, "ms"),
                  "connect_timeout_s": (None, 1, 300, "seconds"), "min_snapshot_age_min": (None, 0, 1440, "minutes"),
                  "band_weeks": (6, 0, 52, "weeks")}
# the [targets.X.changes] table: the fields each source reads (the two numbers apply to every source)
CHANGES_FIELDS = {"file": ("path",), "github": ("repo", "environment", "token_env", "base_url", "releases"),
                  "gitlab": ("project", "environment", "token_env", "base_url")}
CHANGES_REQUIRED = {"file": "path", "github": "repo", "gitlab": "project"}
CHANGES_NUMBERS = {"lookback_hours": (1, 8760), "correlate_minutes": (0, 1440)}
WEB_FIELDS = ("title", "brand", "logo", "accent", "theme", "refresh_seconds", "page_size", "footer", "files")
AGENT_FIELDS = ("provider", "model", "region", "aws_profile", "project_id", "resource", "base_url", "api_key_env", "effort", "max_tokens", "max_iterations",
                "token_budget", "fallback_models", "scheduled_provider", "scheduled_model", "scheduled_base_url", "scheduled_api_key_env")
# api_key_cmd runs a command on the server, so it can only be set in targets.toml, never from the web view or its API
AGENT_PROVIDERS = ("anthropic", "bedrock", "vertex", "foundry", "bedrock-converse",
                   "openai", "openrouter", "groq", "ollama", "ollama-cloud", "vllm", "azure-openai", "vertex-gemini")
SEVERITIES = ("info", "low", "medium", "high")
# (field, kind, default) per channel; kind: text | secret | int | list | sev | bool. Secrets are masked in pages and the API.
# Every channel but stdout has its own severity gate, as deliver() reads it: "" is the global gate, tickets default to high.
GATE = ("min_severity", "sev", "")
CHANNEL_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "stdout": [],
    "ntfy": [("url", "text", "https://ntfy.sh"), ("topic", "text", ""), ("token_env", "text", ""), GATE],
    "slack": [("webhook_url", "secret", ""), ("webhook_url_env", "text", ""), GATE],
    "teams": [("webhook_url", "secret", ""), ("webhook_url_env", "text", ""), GATE],
    "email": [("smtp_host", "text", ""), ("smtp_port", "int", "587"), ("starttls", "bool", "true"), ("from", "text", ""), ("to", "list", ""),
              ("user_env", "text", ""), ("password_env", "text", ""), GATE],
    "jira": [("base_url", "text", ""), ("project_key", "text", ""), ("issue_type", "text", "Task"), ("user_env", "text", ""),
             ("token_env", "text", ""), ("min_severity", "sev", "high")],
    "plane": [("base_url", "text", ""), ("workspace", "text", ""), ("project_id", "text", ""), ("project_identifier", "text", ""),
              ("api_key_env", "text", ""), ("min_severity", "sev", "high")],
    "webhook": [("url", "secret", ""), GATE],
    "pushgateway": [("url", "text", ""), ("job", "text", "dbtriage"), GATE],
}
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")  # what an environment variable's name looks like, for every *_env field


SECTIONS = ("targets", "web", "agent", "delivery")


class SettingsUnavailable(RuntimeError):
    """The settings database cannot be reached; the change was not saved."""


def _write_private(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _shape(d: dict) -> dict:
    return {k: d.get(k) or {} for k in SECTIONS}


class _PgSettings:
    """The overlay as one jsonb row per section in table dbtriage_settings."""

    def __init__(self, url: str):
        from dbtriage.pgstore import PgClient

        self.client = PgClient(url, ["CREATE TABLE IF NOT EXISTS dbtriage_settings (section text PRIMARY KEY, data jsonb NOT NULL,"
                                     " updated_at timestamptz NOT NULL DEFAULT now())"])
        self.where = self.client.where

    def read(self) -> dict:
        return _shape({r[0]: r[1] for r in self.client.run("SELECT section, data FROM dbtriage_settings", (), "all")})

    def write(self, data: dict):
        for k in SECTIONS:
            self.client.run("INSERT INTO dbtriage_settings (section, data) VALUES (%s, %s::jsonb) ON CONFLICT (section) "
                            "DO UPDATE SET data = EXCLUDED.data, updated_at = now()", (k, json.dumps(data.get(k) or {})))



def _entry_problem(name: str, t) -> str | None:
    """Why an overlay entry cannot be used (saved by an older version or by hand), or None: anything apply() would
    otherwise fail on, so one bad entry is skipped instead of stopping every target."""
    if not target_name_ok(name):
        return NAME_RULE
    if not isinstance(t, dict):
        return "an entry must be a set of fields"
    if t.get("engine") not in (None, *ENGINES):
        return f"engine must be one of {', '.join(ENGINES)}"
    problems = _field_problems(t)
    return problems[0] if problems else None


def _field_problems(t: dict) -> list[str]:
    """Field types a stored target must have: the same check for what a save accepts and what apply() uses, so a
    save can never store an entry that is then skipped."""
    out = []
    for k, (_default, lo, hi, unit) in TARGET_NUMBERS.items():  # blank means the default
        out += _number_problem(k, t.get(k), lo, hi, unit, ranged=k != "top_n")
    for k in ("dsn", "dsn_env", "changes_file", "notes", "baseline", "baseline_policy", "schedule", "user", "password"):
        if t.get(k) is not None and not isinstance(t[k], str):
            out.append(f"{k} must be text")
    if t.get("mask_report_literals") not in (None, "", True, False):
        out.append("mask_report_literals must be true or false")
    links = t.get("links")
    if links is not None and not (isinstance(links, list) and all(
            isinstance(l, dict) and isinstance(l.get("url"), str) and isinstance(l.get("label"), str) for l in links)):
        out.append("links must be a list of {label, url}, both text")
    if t.get("changes") is not None:
        out += _changes_problems(t["changes"])
    return out


def _number_problem(k: str, v, lo: int, hi: int, unit: str = "", ranged: bool = True) -> list[str]:
    """A whole number between lo and hi, or blank; JSON true, Infinity and text are refused rather than coerced."""
    if v in (None, ""):
        return []
    try:
        if isinstance(v, bool):  # JSON true is not 1
            raise TypeError
        n = int(v)
    except (TypeError, ValueError, OverflowError):  # OverflowError: JSON's Infinity
        return [f"{k} must be a whole number"]
    return [f"{k}: {lo} to {hi} {unit}".rstrip()] if ranged and not lo <= n <= hi else []


def _changes_problems(c) -> list[str]:
    """Why a [targets.X.changes] table cannot be saved: one check for the edit page, the API and stored entries. A
    table without a source is the cleared feed and is fine."""
    if not isinstance(c, dict):
        return ["changes must be a table"]
    source = c.get("source")
    if not source:
        return []
    if source not in SOURCES:
        return [f"changes.source: one of {', '.join(SOURCES)}"]
    out = []
    for k in ("path", "repo", "project", "environment", "token_env", "base_url"):
        if c.get(k) is not None and not isinstance(c[k], str):
            out.append(f"changes.{k} must be text")
    if c.get("releases") not in (None, "", True, False):
        out.append("changes.releases must be true or false")
    for k, (lo, hi) in CHANGES_NUMBERS.items():
        out += _number_problem(f"changes.{k}", c.get(k), lo, hi, k.rsplit("_", 1)[1])
    if out:
        return out
    if not c.get(CHANGES_REQUIRED[source]):
        out.append(f"changes: a {source} feed needs {CHANGES_REQUIRED[source]}")
    if c.get("token_env") and not ENV_NAME.match(c["token_env"]):
        out.append("changes.token_env: an environment variable name like GITHUB_TOKEN")
    if c.get("base_url") and not re.match(r"^https?://", c["base_url"]):
        out.append("changes.base_url must start with http(s)://")
    return out

_WARNED: set = set()  # overlay entries already reported as skipped, so a reload does not repeat it


class Settings:
    def __init__(self, path: str | os.PathLike, cache: str | os.PathLike | None = None):
        from dbtriage.pgstore import is_pg_url

        self.lock = threading.Lock()
        self.data: dict = {"targets": {}, "web": {}, "agent": {}, "delivery": {}}
        self.degraded: str | None = None
        if is_pg_url(str(path)):
            self.db: _PgSettings | None = _PgSettings(str(path))
            self.path = Path(cache) if cache else Path("dbtriage-settings.cache.json")
            self.where = self.db.where
        else:
            self.db, self.path = None, Path(path)
            self.where = str(self.path)
        self.load()

    @classmethod
    def for_config(cls, cfg: Config) -> "Settings":
        from dbtriage.pgstore import is_pg_url

        p = os.environ.get("DBTRIAGE_SETTINGS") or cfg.web.get("settings_file") or "dbtriage-settings.json"
        if is_pg_url(p):
            return cls(p, Path(cfg.base_dir) / "dbtriage-settings.cache.json")
        p = Path(p)
        return cls(p if p.is_absolute() else Path(cfg.base_dir) / p)

    def exists(self) -> bool:
        """Is there an overlay to apply? (database: always; it may simply be empty)."""
        return self.db is not None or self.path.exists()

    def load(self):
        if self.db is not None:
            try:
                self.data = self.db.read()
                self.degraded = None
                _write_private(self.path, self.data)  # last-good copy for outages
                return
            except Exception as exc:  # noqa: BLE001 - any failure: run on the last good copy
                from dbtriage.storage import is_unavailable

                if not is_unavailable(exc):
                    raise
                self.degraded = f"settings database {self.where} unreachable ({type(exc).__name__}); using the last good copy"
        if self.path.exists():
            self.data = _shape(json.loads(self.path.read_text() or "{}"))

    def save(self):
        with self.lock:
            if self.db is not None:
                try:
                    self.db.write(self.data)
                except Exception as exc:  # noqa: BLE001
                    from dbtriage.storage import is_unavailable

                    if not is_unavailable(exc):
                        raise
                    self.degraded = f"settings database {self.where} unreachable ({type(exc).__name__})"
                    self.load()  # throw away the unsaved change so memory matches what everyone else sees
                    raise SettingsUnavailable(f"{self.degraded}; the change was not saved") from exc
                self.degraded = None
            _write_private(self.path, self.data)

    # ---------------------------------------------------------------- targets
    def put_target(self, name: str, fields: dict, base: dict | None = None) -> list[str]:
        """Validate and store a target; returns a list of problems (empty = saved).

        Like every save, the overlay keeps only what differs from targets.toml: `base` is the file's version of this
        target (never copied in). A given value equal to the file's, or empty where the file has nothing, is not
        stored, so later edits to the file still apply; a different value is, an explicit clear of something the file
        sets included. Fields not given (None) keep what is stored, so a partial PUT changes only what it sends."""
        file = base or {}
        stored = self._entry(name) or {}  # an entry apply() skips is replaced by this save, not patched
        given = {k: v for k, v in fields.items() if k in TARGET_FIELDS and v is not None}
        if type_problems := _field_problems(given):  # before anything below reads a field (a dsn_env of 5, say)
            return type_problems
        if stored.get("deleted"):
            # only a full definition (as the add-target form sends) brings a deleted target back; a partial update
            # must not resurrect it, the same way the schedule card refuses deleted targets
            if not (given.get("engine") and (given.get("dsn") or given.get("dsn_env"))):
                return [f"target {name!r} is deleted; re-create it with an engine and a dsn or dsn_env"]
            stored = {}
            # re-creating is an un-delete: the add-target form comes up blank, so only what is filled in overrides
            # targets.toml, as an API re-create does; switching something off is a normal edit afterwards
            given = {k: v for k, v in given.items() if not _empty(_target_value(k, v))}  # "off" is empty too
        fields = {**file, **stored, **given}  # what the target will look like: validate this
        problems = []
        if not target_name_ok(name):
            problems.append("name: letters, digits, '.', '_' and '-' only")
        if fields.get("engine") not in ENGINES:
            problems.append(f"engine: one of {', '.join(ENGINES)}")
        if not fields.get("dsn") and not fields.get("dsn_env"):
            problems.append("set a dsn or a dsn_env")
        if fields.get("dsn_env") and not ENV_NAME.match(fields["dsn_env"]):
            problems.append("dsn_env: an environment variable name like ORACLE_DSN")
        try:
            dbsnap.mode_of(fields.get("create_snapshot"))
        except ValueError as exc:
            problems.append(str(exc))
        if fields.get("baseline_policy") and fields["baseline_policy"] not in POLICIES:
            problems.append(f"baseline_policy: one of {', '.join(POLICIES)}")
        for l in fields.get("links") or []:
            if not re.match(r"^https?://", l.get("url", "")):
                problems.append(f"link {l.get('label')!r}: url must start with http(s)://")
        problems += self._check_schedule(fields)
        if problems:
            return problems
        clean = dict(stored)
        unschedule = "schedule" in given and not given["schedule"]
        for k, v in given.items():
            if unschedule and k in ("schedule_window", "schedule_paused"):
                continue  # unscheduling drops the old window and pause, as the schedule card does
            _record(clean, k, _target_value(k, v), _target_value(k, file.get(k)))
        if unschedule:
            clean.pop("schedule_window", None)
            clean.pop("schedule_paused", None)
        self.data["targets"][name] = clean
        self.save()
        return []

    @staticmethod
    def _check_schedule(fields: dict) -> list[str]:
        problems = []
        if fields.get("schedule"):
            try:
                scheduler.normalize(fields["schedule"])
            except (ValueError, TypeError, AttributeError) as exc:  # a non-text schedule, from the API
                problems.append(f"schedule: {exc}" if isinstance(exc, ValueError) else "schedule must be text")
        if fields.get("schedule_window") not in (None, ""):
            try:
                if not 5 <= int(fields["schedule_window"]) <= 1440:
                    problems.append("schedule_window: 5 to 1440 minutes")
            except (TypeError, ValueError, OverflowError):
                problems.append("schedule_window: a number of minutes")
        return problems

    def put_schedule(self, name: str, seed: Config, fields: dict) -> list[str]:
        """Change only the schedule fields that are given (None = not given); like every save, store only what
        differs from targets.toml. Unscheduling (an empty schedule) also drops the window and pause, including the
        ones the schedule card resends with every save, so a later schedule starts unpaused."""
        given = {k: v for k, v in fields.items() if v is not None}
        problems = _field_problems(given) or self._check_schedule(given)  # types first: {"schedule": 5} is a 400
        if problems:
            return problems
        cur = dict(self._entry(name) or {})  # an entry apply() skips is replaced, so the new schedule applies
        if cur.get("deleted"):
            return [f"target {name!r} is deleted"]
        file = self._seed_fields(name, seed)
        unschedule = fields.get("schedule") is not None and not fields["schedule"]
        if fields.get("schedule") is not None:
            _record(cur, "schedule", _target_value("schedule", fields["schedule"]), _target_value("schedule", file.get("schedule")))
            if unschedule:
                cur.pop("schedule_window", None)
                cur.pop("schedule_paused", None)
        if not unschedule:
            for k in ("schedule_window", "schedule_paused"):
                if fields.get(k) is not None:
                    _record(cur, k, _target_value(k, fields[k]), _target_value(k, file.get(k)))
        self.data["targets"][name] = cur
        self.save()
        return []

    def delete_target(self, name: str, seeded: bool):
        if seeded:
            self.data["targets"][name] = {"deleted": True}
        else:
            self.data["targets"].pop(name, None)
        self.save()

    def put_web(self, fields: dict, base: dict | None = None):
        """Merge the given look-and-feel fields, storing only what differs from `base` (targets.toml's [web] over the
        built-in defaults, as the web view passes it). A stored "" is a cleared field: the built-in default."""
        file = base or {}
        web = dict(self.data["web"])
        for k, v in fields.items():
            if k in WEB_FIELDS and v is not None:
                _record(web, k, _web_value(k, v), _web_value(k, file.get(k)))
        self.data["web"] = web
        self.save()

    def put_agent(self, fields: dict, base: dict | None = None) -> list[str]:
        """Merge the given agent fields, storing only what differs from `base` (targets.toml's [agent]) or, where the
        file is silent, from what the Agent card shows by default. Provider, model, resource and the numbers cannot
        be blank: clearing one drops the stored value, so targets.toml's applies. A cleared optional field
        (base_url, region, effort ...) that the file sets is stored as "" and beats it."""
        for k in ("provider", "scheduled_provider"):
            if fields.get(k) and fields[k] not in AGENT_PROVIDERS:
                return [f"{k}: one of {', '.join(AGENT_PROVIDERS)}"]
        file = {**AGENT_DEFAULTS, **(base or {})}
        agent = dict(self.data["agent"])
        problems = []
        for k, v in fields.items():
            if k not in AGENT_FIELDS or v is None:
                continue
            if v == "" and k in AGENT_REQUIRED:
                agent.pop(k, None)
                continue
            if k in AGENT_NUMBERS:
                try:
                    v = int(v)
                    if isinstance(fields[k], bool) or v < AGENT_NUMBERS[k]:
                        raise ValueError
                except (TypeError, ValueError):  # an API body can carry anything: refuse it, never crash
                    problems.append(f"{k}: a whole number, at least {AGENT_NUMBERS[k]}"); continue
            if k == "fallback_models":
                if not isinstance(v, (str, list)):
                    problems.append("fallback_models: model ids separated by commas"); continue
                v = agent_value(k, v)
                if v == [] and file.get(k) != []:  # "none" is a real choice: unset means the provider's own chain, not an empty one
                    agent[k] = []; continue
            _record(agent, k, v, file.get(k))
        if not problems and (p := scheduled_problem({**file, **agent})):
            problems.append(p)
        if problems:
            return problems
        self.data["agent"] = agent
        self.save()
        return []

    def put_delivery(self, data: dict, base: dict | None = None) -> list[str]:
        """data = {"channels": [...], "min_severity": "...", "<channel>": {field: value}}, merged into what is stored.

        Like every save, only what differs from `base` (targets.toml's [delivery]) or, where the file is silent,
        from the built-in default the form shows is stored, so later edits to the file still apply. A cleared field
        the file sets is stored as "" or [] and beats it. A blank secret keeps the stored one (the form never shows
        it back). [] for channels means "no channels", not "back to targets.toml"."""
        file = base or {}
        problems = []
        clean: dict = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in self.data["delivery"].items()}
        if "channels" in data:
            chans = data.get("channels") or []
            bad = [c for c in chans if c not in CHANNEL_FIELDS]
            if bad:
                problems.append(f"unknown channel(s): {', '.join(bad)}")
            chans = [c for c in chans if c in CHANNEL_FIELDS]
            if sorted(chans) == sorted(file.get("channels", ["stdout"])):
                clean.pop("channels", None)
            else:
                clean["channels"] = chans
        if "min_severity" in data:
            v = data["min_severity"] or ""
            if v and v not in SEVERITIES:
                problems.append("min_severity: one of " + ", ".join(SEVERITIES))
            if (v or "info") == (file.get("min_severity") or "info"):
                clean.pop("min_severity", None)
            else:
                clean["min_severity"] = v
        if "gone_after_runs" in data:  # the finding-life threshold (dbtriage.ledger); blank = the default, 3
            v = data["gone_after_runs"]
            if bad := _number_problem("gone_after_runs", v, 1, 1000):
                problems += bad
            else:
                v = int(v) if v not in (None, "") else ""
                if (v or 3) == (file.get("gone_after_runs") or 3):
                    clean.pop("gone_after_runs", None)
                else:
                    clean["gone_after_runs"] = v
        for ch, spec in CHANNEL_FIELDS.items():
            if ch not in data:
                continue  # not mentioned: keep what is stored
            given = data.get(ch) or {}
            stored = self.data["delivery"].get(ch, {})
            file_ch = file.get(ch) or {}
            out = {k: v for k, v in stored.items() if k not in given}
            for name, kind, default in spec:
                if name not in given:
                    continue
                v = given.get(name)
                if kind == "secret" and not v:  # blank secret: keep the stored one
                    if stored.get(name):
                        out[name] = stored[name]
                    continue
                if kind == "sev" and v and v not in SEVERITIES:
                    problems.append(f"{ch}.{name}: one of " + ", ".join(SEVERITIES)); continue
                if kind in ("text", "secret", "sev") and not isinstance(v, str):
                    problems.append(f"{ch}.{name}: must be text"); continue
                if kind == "bool" and v not in (None, "", True, False, "true", "false"):
                    problems.append(f"{ch}.{name}: true or false"); continue
                try:
                    typed, cmp_given = _channel_value(kind, v, ""), _channel_value(kind, v, default)
                    cmp_file = _channel_value(kind, file_ch.get(name), default)
                except (TypeError, ValueError):  # an API body can carry any JSON type: refuse it, do not crash
                    problems.append(f"{ch}.{name}: must be " + ("a list" if kind == "list" else "a number")); continue
                if cmp_given == cmp_file:
                    out.pop(name, None)
                else:
                    out[name] = typed
            if out:
                clean[ch] = out
            else:
                clean.pop(ch, None)
        if problems:
            return problems
        self.data["delivery"] = clean
        self.save()
        return []

    def delivery_view(self, seed: Config, masked: bool = True) -> dict:
        """Effective delivery config (seed + overlay), secrets masked for display."""
        eff = merged_delivery(seed.delivery, self.data["delivery"])
        if not masked:
            return eff
        out = dict(eff)
        for ch, spec in CHANNEL_FIELDS.items():
            if ch in out:
                out[ch] = {k: (mask_secret(v) if any(n == k and kind == "secret" for n, kind, _ in spec) else v) for k, v in out[ch].items()}
        return out

    # ---------------------------------------------------------------- apply
    def apply(self, seed: Config) -> Config:
        """Seed config + overlay -> the effective Config the app runs with."""
        targets = dict(seed.targets)
        for name, t in list(targets.items()):  # a seeded dsn_env follows the environment (.env edits included)
            env = t.extra.get("dsn_env")
            if env and "dsn" not in t.extra and os.environ.get(env) and os.environ[env] != t.dsn:  # an inline dsn wins
                targets[name] = replace(t, dsn=os.environ[env])
        skipped = self._skipped(seed)
        for name, t in self.data["targets"].items():
            bad = skipped.get(name)
            if bad:  # saved by an older version or by hand: skip the entry (a targets.toml target keeps the file's
                # settings), so one bad entry cannot stop every other target, and say once how to fix it
                if (name, bad) not in _WARNED:
                    _WARNED.add((name, bad))
                    print(f"settings: target {name!r} in {self.where} is skipped: {bad}; fix or remove it there", file=sys.stderr)
                continue
            if t.get("deleted"):
                targets.pop(name, None)
                continue
            t = {**self._seed_fields(name, seed), **t}  # an overlay entry patches the seeded target
            dsn = t.get("dsn") or (os.environ.get(t["dsn_env"], "") if t.get("dsn_env") else "")
            known = {"engine", "dsn", "dsn_env", "top_n", "diagnostics_pack", "changes_file", "deleted"}
            targets[name] = Target(name=name, engine=t.get("engine", "postgres"), dsn=dsn, top_n=top_n_of(t.get("top_n")),
                                   diagnostics_pack=bool(t.get("diagnostics_pack", False)), changes_file=t.get("changes_file"),
                                   extra={k: v for k, v in t.items() if k not in known})
        return replace(seed, targets=targets, web={**seed.web, **self.data["web"]}, agent={**seed.agent, **self.data["agent"]},
                       delivery=merged_delivery(seed.delivery, self.data["delivery"]))

    def _skipped(self, seed: Config) -> dict[str, str]:
        """The overlay entries apply() skips, with why: walked in apply()'s own order, with its own set of names (the
        file's targets, plus each entry it keeps, less each it deletes), so this and apply() can never disagree."""
        names, out = set(seed.targets), {}
        for name, t in self.data["targets"].items():
            bad = _entry_problem(name, t)
            if not bad and (other := next((n for n in names if n != name and n.lower() == name.lower()), None)):
                bad = f"it differs only in case from {other!r}, and they would share one folder of snapshots"
            if bad:
                out[name] = bad
            elif t.get("deleted"):
                names.discard(name)
            else:
                names.add(name)
        return out

    def _entry(self, name: str, seed: Config | None = None) -> dict | None:
        """The overlay's entry for a target, or None when there is none or apply() skips it, so the Settings page shows
        what runs. Without the seed, only the entry's own problems are known."""
        t = self.data["targets"].get(name)
        if t is None or _entry_problem(name, t):
            return None
        return None if seed is not None and name in self._skipped(seed) else t

    def origin(self, name: str, seed: Config) -> str:
        if self._entry(name, seed) is not None:
            return "ui (overrides file)" if name in seed.targets else "ui"
        return "file"

    def form_of(self, name: str, seed: Config) -> dict:
        """Current editable values for a target: the seed file's values patched by the overlay."""
        over = self._entry(name, seed) or {}
        if over.get("deleted"):
            return {}
        return {**self._seed_fields(name, seed), **over}

    @staticmethod
    def _seed_fields(name: str, seed: Config) -> dict:
        """A seeded target as form fields. A target that came from dsn_env keeps the variable name, not the resolved
        value, so editing it never copies the secret into the settings file."""
        t = seed.targets.get(name)
        if not t:
            return {}
        out = {"engine": t.engine, "top_n": t.top_n, "diagnostics_pack": t.diagnostics_pack, "changes_file": t.changes_file, **t.extra}
        if t.extra.get("dsn_env") and "dsn" not in t.extra:
            out.pop("dsn", None)  # resolved from the environment: never copied into a form or the overlay
        elif "dsn" not in out:
            out["dsn"] = t.dsn  # written in the file (load_config keeps it in extra when a dsn_env is named too)
        return out


# ---------------------------------------------------------------- what the overlay stores
AGENT_DEFAULTS = {"provider": "anthropic", "effort": "high", "max_iterations": 25,  # what the Agent card shows unset
                  "token_budget": DEFAULT_TOKEN_BUDGET}
AGENT_NUMBERS = {"max_tokens": 1, "max_iterations": 1, "token_budget": 0}  # smallest allowed; a token_budget of 0 = no limit
AGENT_REQUIRED = ("provider", "model", "resource", "max_tokens", "max_iterations", "token_budget", "fallback_models")  # blank = use targets.toml's


def _empty(v) -> bool:
    return v is None or v is False or (isinstance(v, (str, list, dict)) and len(v) == 0)


def _record(overlay: dict, key: str, value, file_value) -> None:
    """Store only what differs from targets.toml. A value equal to the file's, or empty where the file is empty too,
    is dropped so the file keeps feeding through; anything else is stored, an explicit clear included."""
    if value == file_value or (_empty(value) and _empty(file_value)):
        overlay.pop(key, None)
    else:
        overlay[key] = value


def _target_value(key: str, v):
    """A target field in the form it is stored and compared in (the form posts strings)."""
    if key in TARGET_NUMBERS:
        return int(v) if v not in (None, "") else TARGET_NUMBERS[key][0]
    if key == "mask_report_literals":  # on unless switched off: False must be stored even where the file is silent
        return True if v in (None, "") else bool(v)
    if key == "changes":
        return changes_value(v)
    if key == "schedule":
        if not v:
            return ""
        try:
            return scheduler.normalize(v)
        except ValueError:
            return v
    if key == "create_snapshot":  # off | incident | always; true (the old switch) means incident; off is the empty value
        try:
            mode = dbsnap.mode_of(v)
        except ValueError:
            return v
        return False if mode == "off" else mode
    if key in ("diagnostics_pack", "schedule_paused"):
        return bool(v)
    return v


def changes_value(v) -> dict:
    """A [targets.X.changes] table as it is stored and compared: only its source's fields, blanks dropped, numbers
    typed, releases kept only when on. Anything without a source is the empty table: no feed."""
    if not isinstance(v, dict) or not v.get("source"):
        return {}
    source = str(v["source"])
    out = {"source": source}
    for k in CHANGES_FIELDS.get(source, ()):
        val = v.get(k)
        if k == "releases":
            if val in (True, "true", "on", "1"):
                out[k] = True
        elif val not in (None, ""):
            out[k] = val
    for k in CHANGES_NUMBERS:
        if v.get(k) not in (None, ""):
            try:
                out[k] = int(v[k])
            except (TypeError, ValueError):
                out[k] = v[k]  # _changes_problems has already refused it
    return out


def _web_value(key: str, v):
    if key in ("refresh_seconds", "page_size") and v not in (None, ""):
        try:
            return int(v)
        except (TypeError, ValueError):
            return v
    if key == "files":  # the card posts one line; the API may send the list
        return list_value(v)
    return v


def list_value(v) -> list[str]:
    """A list field as typed on a card (names separated by commas or spaces) or as the API sends it (a list)."""
    if isinstance(v, str):
        return [x for x in re.split(r"[,\s]+", v) if x]
    return [str(x) for x in (v or [])]


def agent_value(key: str, v):
    """An [agent] field as it is stored and compared. fallback_models is a list: the card posts model ids separated
    by commas, "none" turns the chain off ([]) and blank means targets.toml's or the provider's default."""
    if key == "fallback_models" and v not in (None, ""):
        if isinstance(v, str) and v.strip().lower() == "none":
            return []
        return list_value(v)
    return v


def _channel_value(kind: str, v, default):
    """A delivery channel field, typed; empty means `default` (what the form shows when nothing is set), except
    for a severity gate, where "" is the "(global)" choice and only a missing value means the default."""
    if kind == "sev":
        return v if v is not None else default
    if _empty(v):
        v = default
    if kind == "bool":
        return v in (True, "true")
    if kind == "int":
        return int(v) if v not in (None, "") else ""
    if kind == "list":
        if isinstance(v, str):
            return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
        return list(v or [])
    return v if v is not None else ""


def merged_delivery(seed: dict, overlay: dict) -> dict:
    """Top-level keys from the overlay win; per-channel dicts merge field by field."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in seed.items()}
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def mask_secret(value: str | None) -> str:
    """Webhook URLs keep scheme and host so people can recognise them; everything after is hidden. Tokens are fully hidden."""
    if not value:
        return ""
    if m := re.match(r"^(https?://[^/]+)/.+", value):
        return m[1] + "/***"
    return "***"


def mask_dsn(dsn: str | None) -> str:
    """Hide the password part of a URL-style DSN; keep host and db so people can recognise it."""
    if not dsn:
        return ""
    out = re.sub(r"(://[^/@]*?:)[^@]*(@)", r"\1***\2", dsn, count=1)          # scheme://user:pass@host
    if out == dsn and re.search(r"(?i)(^|;)\s*(pwd|password)\s*=", dsn):  # a SQL Server / ODBC connection string
        return re.sub(r"(?i)\b(pwd|password)\s*=\s*(\{(?:[^}]|\}\})*\}|[^;]*)", r"\1=***", dsn)
    if out == dsn:
        out = re.sub(r"^([^/:@]+/)[^@]+(@)", r"\1***\2", dsn, count=1)          # user/pass@host:port/service (Oracle easy connect)
    return out


def probe(target: Target, timeout_s: int = 5) -> tuple[bool, str]:
    """Connect, run one trivial statement, report version. Used by the 'test connection' button."""
    try:
        if target.engine == "postgres":
            import psycopg

            with psycopg.connect(target.dsn, connect_timeout=timeout_s, application_name="dbtriage-probe") as c:
                return True, c.execute("select version()").fetchone()[0][:60]
        if target.engine == "mysql":
            import pymysql

            from dbtriage.collectors.mysql import _conn_kwargs

            c = pymysql.connect(**_conn_kwargs(target), connect_timeout=timeout_s)
            try:
                cur = c.cursor(); cur.execute("select version()"); return True, f"MySQL {cur.fetchone()[0]}"
            finally:
                c.close()
        if target.engine == "sqlserver":
            try:
                from dbtriage.collectors.sqlserver import connect
            except ModuleNotFoundError:
                return False, "SQL Server needs its driver: uv sync --extra sqlserver"

            c = connect(target, login_timeout_s=timeout_s)
            try:
                from dbtriage.collectors.sqlserver import TAG

                cur = c.cursor(); cur.execute(f"{TAG} select @@version")  # tagged: kept out of top SQL like the collector's
                return True, cur.fetchone()[0].splitlines()[0][:60]
            finally:
                c.close()
        if target.engine == "oracle":
            import oracledb

            with oracledb.connect(dsn=target.dsn, user=target.extra.get("user") or None, password=target.extra.get("password") or None, tcp_connect_timeout=timeout_s) as c:
                return True, f"Oracle {c.version}"
        return False, f"unsupported engine {target.engine!r}"
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - the message is the point (a bad or empty dsn is SystemExit)
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"
