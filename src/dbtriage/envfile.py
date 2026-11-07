"""Read a .env file and keep the process environment in step with it, so adding or changing a key (an API key,
a DSN) takes effect without restarting the web view or the scheduler.

Which file: DBTRIAGE_ENV_FILE, else .env next to targets.toml. DBTRIAGE_NO_ENV_FILE=1 turns this off.

Precedence: a variable that was already in the environment at startup with a different value than the file
(set by systemd, a container, a secret store) always wins and is never touched. Every other key follows the
file: added, changed or removed as the file is. Values are never logged, only key names.

Format: KEY=value, optional `export `, '#' comments, single or double quotes around a value.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = _LINE.match(raw)
        if not m:
            continue
        key, val = m[1], m[2]
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
            if raw.rstrip().endswith('"'):
                val = val.replace("\\n", "\n").replace('\\"', '"')
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0]
        out[key] = val
    return out


class EnvFile:
    CHECK_EVERY_S = 1.0

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.mtime: float | None = None
        self.owned: dict[str, str] = {}   # keys this file set, with the value it set
        self.external: set[str] = set()   # keys the real environment set differently: hands off
        self.started = dict(os.environ)
        self._checked = 0.0
        self.refresh(force=True)

    def _read(self) -> dict[str, str] | None:
        try:
            return parse(self.path.read_text())
        except FileNotFoundError:
            return {}
        except OSError:
            return None

    def refresh(self, force: bool = False) -> list[str]:
        """Re-read the file if it changed; returns the names of keys whose value changed (empty if none)."""
        now = time.monotonic()
        if not force and now - self._checked < self.CHECK_EVERY_S:
            return []
        with self.lock:
            self._checked = now
            try:
                mtime = self.path.stat().st_mtime
            except FileNotFoundError:
                mtime = None
            if not force and mtime == self.mtime:
                return []
            self.mtime = mtime
            values = self._read()
            if values is None:
                return []
            changed = []
            for key in list(self.owned):
                if key not in values and os.environ.get(key) == self.owned[key]:
                    del os.environ[key]
                    del self.owned[key]
                    changed.append(key)
            for key, val in values.items():
                if key in self.external:
                    continue
                if key not in self.owned and key in os.environ and os.environ[key] != val:
                    if key in self.started:  # set by the real environment: it wins
                        self.external.add(key)
                        continue
                if os.environ.get(key) != val:
                    changed.append(key)
                os.environ[key] = val
                self.owned[key] = val
            return changed


_active: EnvFile | None = None


def init(config_path: str | os.PathLike | None) -> EnvFile | None:
    """Load the .env that belongs to this config (call before load_config, so dsn_env lookups see it)."""
    global _active
    if os.environ.get("DBTRIAGE_NO_ENV_FILE"):
        return None
    path = os.environ.get("DBTRIAGE_ENV_FILE") or Path(config_path or os.environ.get("DBTRIAGE_CONFIG") or "targets.toml").resolve().parent / ".env"
    _active = EnvFile(path)
    try:
        mode = _active.path.stat().st_mode
        if mode & 0o077:
            import sys

            print(f"env: {_active.path} can be read by other users (mode {oct(mode & 0o777)[2:]}); it holds secrets: chmod 600 {_active.path}",
                  file=sys.stderr)
    except FileNotFoundError:
        pass
    return _active


def refresh() -> list[str]:
    """Pick up edits to the .env; cheap (one stat per second at most). Logs changed key names, never values."""
    if _active is None:
        return []
    changed = _active.refresh()
    if changed:
        print(f"env: picked up {', '.join(sorted(changed))} from {_active.path}")
    return changed
