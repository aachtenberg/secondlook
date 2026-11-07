"""Configuration: storage location plus one entry per database target."""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ENGINES = ("postgres", "mysql", "oracle", "sqlserver")
# A target's name becomes part of every storage key and URL for it, so it is kept to plain characters
# use fullmatch ('$' would let a trailing newline in); no trailing '.': Windows would make "demo." the same as "demo"
TARGET_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9_-])?")
# Windows opens these as devices, whatever the extension ("nul.json" is NUL): never a file or directory name
WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})


NAME_RULE = ("a name is letters, digits, '.', '_' and '-', up to 64, starting with a letter or digit and not ending in "
             "'.', and not a Windows device name such as CON or NUL; it is part of every stored snapshot's path")


def windows_reserved(part: str) -> bool:
    return part.split(".", 1)[0].lower() in WINDOWS_RESERVED


def top_n_of(v) -> int:
    """A target's top_n: 25 when left blank (missing, null or ""), the number otherwise, 0 included."""
    return 25 if v in (None, "") else int(v)


def case_clash(names) -> tuple[str, str] | None:
    """Two names that differ only in case (Demo, demo): case-insensitive filesystems give them one directory."""
    seen: dict[str, str] = {}
    for n in names:
        if n.lower() in seen and seen[n.lower()] != n:
            return seen[n.lower()], n
        seen.setdefault(n.lower(), n)
    return None


def target_name_ok(name) -> bool:
    """The one rule for a target's name, from targets.toml, the web view or the settings overlay."""
    return isinstance(name, str) and TARGET_NAME.fullmatch(name) is not None and not windows_reserved(name)


@dataclass
class Target:
    name: str
    engine: str
    dsn: str
    top_n: int = 25
    diagnostics_pack: bool = False  # Oracle only: AWR/ASH licensing gate
    changes_file: str | None = None  # optional JSON list of deploys/changes for get_recent_changes
    extra: dict = field(default_factory=dict)


@dataclass
class Config:
    storage_url: str
    targets: dict[str, Target]
    agent: dict = field(default_factory=dict)     # see dbtriage.llm
    delivery: dict = field(default_factory=dict)  # see dbtriage.deliver
    web: dict = field(default_factory=dict)       # see dbtriage.web
    collect: dict = field(default_factory=dict)   # [collect]: allow_db_writes (dbtriage.dbsnap); targets.toml only
    mcp: dict = field(default_factory=dict)       # [mcp]: enabled, allow_run, tools (dbtriage.mcp_server); targets.toml only
    base_dir: str = "."                           # directory of the config file; relative paths resolve against it
    file_only: "Config | None" = field(default=None, repr=False, compare=False)  # targets.toml alone, before the UI overlay

    def target(self, name: str) -> Target:
        try:
            return self.targets[name]
        except KeyError:
            raise SystemExit(f"unknown target {name!r}; known: {', '.join(sorted(self.targets)) or '(none)'}")


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("DBTRIAGE_CONFIG", "targets.toml"))
    if not path.exists():
        raise SystemExit(f"config file not found: {path}")
    data = tomllib.loads(path.read_text())
    for section in ("storage", "targets", "agent", "delivery", "web", "collect", "mcp"):
        if not isinstance(data.get(section, {}), dict):  # "mcp = true" instead of an [mcp] table, say
            raise SystemExit(f"{path}: {section} must be a table ([{section}] with settings under it), not {data[section]!r}")
    from dbtriage.llm import scheduled_problem

    if problem := scheduled_problem(data.get("agent", {})):  # else every scheduled review would quietly be the rules'
        raise SystemExit(f"{path}: [agent] {problem}")

    storage_url = os.environ.get("DBTRIAGE_STORAGE") or data.get("storage", {}).get("url") or "file://./data"
    base = path.resolve().parent
    if storage_url.startswith("file://") and not storage_url.startswith("file:///"):
        storage_url = "file://" + str((base / storage_url[len("file://"):]).resolve())  # relative to this file, not the cwd

    targets: dict[str, Target] = {}
    for name, t in data.get("targets", {}).items():
        if not target_name_ok(name):
            raise SystemExit(f"target {name!r}: {NAME_RULE}")
        if t.get("engine", "postgres") not in ENGINES:
            raise SystemExit(f"target {name!r}: engine must be one of {', '.join(ENGINES)}")
        dsn = t.get("dsn")
        if not dsn and t.get("dsn_env"):
            dsn = os.environ.get(t["dsn_env"]) or ""
            if not dsn:  # only this target is affected: the others, the web view and the scheduler keep working
                print(f"config: target {name!r}: environment variable {t['dsn_env']} is not set; it cannot be collected until it is",
                      file=sys.stderr)
        elif not dsn:
            raise SystemExit(f"target {name!r}: set either dsn or dsn_env")
        known = {"engine", "dsn", "dsn_env", "top_n", "diagnostics_pack", "changes_file"}
        targets[name] = Target(
            name=name,
            engine=t.get("engine", "postgres"),
            dsn=dsn,
            top_n=top_n_of(t.get("top_n")),
            diagnostics_pack=bool(t.get("diagnostics_pack", False)),
            changes_file=str(base / t["changes_file"]) if t.get("changes_file") else None,  # relative to this file
            extra={k: v for k, v in t.items() if k not in known} | ({"dsn_env": t["dsn_env"]} if t.get("dsn_env") else {})
                  # both set: the inline dsn wins (above); keep it so the Settings page's view of the file has it too
                  | ({"dsn": t["dsn"]} if t.get("dsn") and t.get("dsn_env") else {}),
        )

    if clash := case_clash(targets):
        raise SystemExit(f"targets {clash[0]!r} and {clash[1]!r} differ only in case: on Windows and macOS they would "
                         "share one folder of snapshots; rename one")
    cfg = Config(storage_url=storage_url, targets=targets, agent=data.get("agent", {}), delivery=data.get("delivery", {}),
                 web=data.get("web", {}), collect=data.get("collect", {}), mcp=data.get("mcp", {}), base_dir=str(path.resolve().parent))
    if os.environ.get("DBTRIAGE_NO_OVERLAY"):
        return cfg
    # Targets, agent, delivery and branding edited in the web UI live in a JSON overlay next to this file;
    # apply it here too so cron / webhook / CLI runs see the same configuration as the UI.
    from dbtriage.settings import Settings

    st = Settings.for_config(cfg)
    if not st.exists():
        return cfg
    out = st.apply(cfg)
    out.file_only = cfg  # the web view edits on top of the file, never on top of an earlier overlay
    return out
