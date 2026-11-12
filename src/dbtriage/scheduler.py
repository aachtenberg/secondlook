"""Built-in scheduler: runs `dbtriage run --mode scheduled` for every target that has a schedule.

A schedule is a five-field cron expression in the server's local time ("0 * * * *"), or the
shorthand "every 15m" / "every 2h" (converted to cron). The settings page offers presets so
nobody has to type cron. Per target the overlay (or targets.toml) holds:

    schedule = "0 * * * *"      # cron, local time; empty/missing = not scheduled
    schedule_window = 60        # minutes collected per run (default 60)
    schedule_paused = true      # keep the schedule but do not fire

The loop is a daemon thread (`dbtriage web --schedule`, or `dbtriage scheduler` standalone on a host
that can reach the databases). It re-reads the configuration every tick, so a schedule saved in
the UI takes effect without a restart. Missed slots are not caught up after a restart. One run
per target at a time: a slot is skipped (and recorded as such) if the previous run is still going.
External timers (cron, EventBridge, Cloud Scheduler) keep working: they call the same pipeline.

The fleet digest (dbtriage.fleet) rides the same loop: `[delivery] digest` names its schedule, and the tick fires
it when its slot has passed. Its last send is kept in the store, so a restart does not resend it, and a slot that
passed while the process was down is sent once it is back (the note says "since the last digest", so nothing is lost).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

PRESETS: list[tuple[str, str]] = [  # (cron, label) in the order the UI shows them
    ("", "not scheduled"),
    ("*/15 * * * *", "every 15 minutes"),
    ("*/30 * * * *", "every 30 minutes"),
    ("0 * * * *", "hourly"),
    ("0 */6 * * *", "every 6 hours"),
    ("0 3 * * *", "daily at 03:00"),
    ("0 8 * * 1-5", "weekdays at 08:00"),
]
LABELS = dict(PRESETS)
RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # minute, hour, day of month, month, day of week (0 and 7 = Sunday)


def normalize(expr: str | None) -> str:
    """'every 15m' / 'every 2h' / 'hourly' / 'daily' -> cron; cron passes through. Raises ValueError."""
    e = (expr or "").strip().lower()
    if not e:
        return ""
    words = {"hourly": "0 * * * *", "daily": "0 3 * * *", "weekly": "0 3 * * 1", "monthly": "0 3 1 * *"}
    if e in words:
        return words[e]
    if m := re.match(r"^every\s+(\d+)\s*(m|min|minutes?|h|hours?)$", e):
        n, unit = int(m[1]), m[2][0]
        if unit == "m":
            if n < 1 or n > 59 or 60 % n:
                raise ValueError("'every Nm' needs N dividing 60 (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)")
            return f"*/{n} * * * *" if n > 1 else "* * * * *"
        if n < 1 or n > 23 or 24 % n:
            raise ValueError("'every Nh' needs N dividing 24 (1, 2, 3, 4, 6, 8, 12)")
        return f"0 */{n} * * *" if n > 1 else "0 * * * *"
    parse(e)
    return " ".join(e.split())


def describe(expr: str | None) -> str:
    e = normalize(expr) if expr else ""
    return LABELS.get(e) or (f"cron {e}" if e else "not scheduled")


def _field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ValueError(f"bad step in {spec!r}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError(f"bad field {spec!r}")
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"{spec!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


@dataclass
class Cron:
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]
    any_day: bool = True      # day-of-month field was '*'
    any_weekday: bool = True  # day-of-week field was '*'

    def matches(self, t: datetime) -> bool:
        if t.minute not in self.minutes or t.hour not in self.hours or t.month not in self.months:
            return False
        dom, dow = t.day in self.days, (t.isoweekday() % 7) in self.weekdays
        if not self.any_day and not self.any_weekday:
            return dom or dow  # vixie cron: both restricted = either matches
        return dom and dow

    def day_ok(self, t: datetime) -> bool:
        return self.matches(t.replace(minute=min(self.minutes), hour=min(self.hours))) if t.month in self.months else False

    def next_after(self, now: datetime) -> datetime:
        """First matching minute strictly after `now` (same tzinfo as `now`)."""
        t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60):
            if t.month not in self.months:
                t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self.day_ok(t):
                t = t.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if t.hour not in self.hours:
                t = t.replace(minute=0) + timedelta(hours=1)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return t
        raise ValueError("schedule never matches")


def parse(expr: str) -> Cron:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("a schedule has five fields: minute hour day month weekday (e.g. '0 * * * *'), or 'every 15m'")
    sets = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, RANGES)]
    wd = {d % 7 for d in sets[4]}
    return Cron(sets[0], sets[1], sets[2], sets[3], wd, any_day=parts[2] == "*", any_weekday=parts[4] == "*")


def schedule_of(extra: dict) -> tuple[str, int, bool]:
    """(cron, window minutes, paused) from a target's extra fields; ('', 60, False) when unscheduled."""
    try:
        cron = normalize(extra.get("schedule"))
    except (ValueError, TypeError, AttributeError):  # schedule = 5 in targets.toml: unscheduled, not a crash
        cron = ""
    try:
        window = max(5, int(extra.get("schedule_window") or 60))
    except (TypeError, ValueError, OverflowError):  # a bad value (Infinity included): the default, not a crashed tick
        window = 60
    return cron, window, bool(extra.get("schedule_paused"))


@dataclass
class State:
    expr: str = ""
    next: datetime | None = None
    last: datetime | None = None
    last_status: str = ""


def _now() -> datetime:
    return datetime.now().astimezone()


class Scheduler(threading.Thread):
    """fire(target, window_minutes, reason) -> (ok, message). targets() -> {name: extra} read every tick."""

    def __init__(self, targets: Callable[[], dict[str, dict]], fire: Callable[[str, int, str], tuple[bool, str]], tick_s: float = 20,
                 on_tick: Callable[[], None] | None = None, digest=None):
        super().__init__(daemon=True, name="dbtriage-scheduler")
        self.targets, self.fire, self.tick_s, self.on_tick = targets, fire, tick_s, on_tick
        self.digest = digest  # fleet.Digest, or anything with schedule() -> cron, last_sent() -> datetime | None, fire(slot, now, reason)
        self.digest_state = State()
        self.state: dict[str, State] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started_at = _now()

    def run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the loop must survive a bad config
                print(f"scheduler: {type(exc).__name__}: {exc}")
            if self.on_tick:
                try:
                    self.on_tick()  # e.g. push spooled snapshots once the store is back
                except Exception as exc:  # noqa: BLE001
                    print(f"scheduler: housekeeping: {type(exc).__name__}: {exc}")
            self.stop.wait(self.tick_s)

    def tick(self, now: datetime | None = None):
        now = now or _now()
        cfg = self.targets()
        with self.lock:
            for name in list(self.state):
                if name not in cfg:
                    del self.state[name]
            for name, extra in cfg.items():
                cron, window, paused = schedule_of(extra)
                st = self.state.setdefault(name, State())
                if cron != st.expr:
                    st.expr, st.next = cron, (parse(cron).next_after(now) if cron else None)
                if not cron or paused or st.next is None or now < st.next:
                    continue
                ok, msg = self.fire(name, window, f"schedule {describe(cron)}")
                st.last, st.last_status = now, ("started" if ok else f"skipped: {msg}")
                st.next = parse(cron).next_after(now)
            if self.digest is not None:
                self._tick_digest(now)

    def _tick_digest(self, now: datetime):
        """The fleet digest's slot. Unlike a target, its next slot is counted from the last send kept in the store
        when the schedule is (re)read, so a restart neither resends nor skips the slot it was down for."""
        try:
            cron = normalize(self.digest.schedule())
        except (ValueError, TypeError, AttributeError):
            cron = ""
        st = self.digest_state
        if cron != st.expr:
            st.expr, st.next = cron, None
            if cron:
                last = self.digest.last_sent()
                st.next = parse(cron).next_after(last.astimezone(now.tzinfo) if last else now)
        if not cron or st.next is None or now < st.next:
            return
        ok, msg = self.digest.fire(st.next, now, f"schedule {describe(cron)}")
        st.last, st.last_status = now, ("sent" if ok else f"skipped: {msg}")
        st.next = parse(cron).next_after(now)

    def rows(self, cfg: dict[str, dict]) -> list[dict]:
        """One row per target for the settings page and /api/schedule."""
        out = []
        with self.lock:
            for name, extra in sorted(cfg.items()):
                cron, window, paused = schedule_of(extra)
                st = self.state.get(name, State())
                nxt = st.next if st.expr == cron else (parse(cron).next_after(_now()) if cron else None)
                out.append({"target": name, "schedule": cron, "label": describe(cron), "window": window, "paused": paused,
                            "next": None if (paused or not cron) else nxt, "last": st.last, "last_status": st.last_status})
        return out


def rows_without_scheduler(cfg: dict[str, dict]) -> list[dict]:
    """Same shape as Scheduler.rows when no scheduler thread runs in this process (next is still computed)."""
    out = []
    for name, extra in sorted(cfg.items()):
        cron, window, paused = schedule_of(extra)
        out.append({"target": name, "schedule": cron, "label": describe(cron), "window": window, "paused": paused,
                    "next": parse(cron).next_after(_now()) if cron and not paused else None, "last": None, "last_status": ""})
    return out


def run_standalone(config_path: str | None, use_llm: bool = True):
    """`dbtriage scheduler`: the loop without the web UI, re-reading targets.toml + overlay every tick."""
    from dbtriage.cli import run_request
    from dbtriage.config import load_config
    from dbtriage.triggers import TriageRequest

    running: dict[str, threading.Thread] = {}

    def targets() -> dict[str, dict]:
        from dbtriage import envfile

        envfile.refresh()  # .env edits apply to the next run without a restart
        return {n: t.extra for n, t in load_config(config_path).targets.items()}

    def fire(name: str, window: int, reason: str) -> tuple[bool, str]:
        th = running.get(name)
        if th and th.is_alive():
            return False, "previous run still going"

        def go():
            try:
                r = run_request(TriageRequest(target=name, mode="scheduled", window_minutes=window, source="schedule", reason=reason), use_llm=use_llm)
                print(f"scheduler: {name}: {r['severity']}: {r['title']} ({r['snapshot']})")
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - a bad target must not stop the scheduler
                print(f"scheduler: {name} failed: {type(exc).__name__}: {exc}")

        th = threading.Thread(target=go, daemon=True, name=f"run-{name}")
        running[name] = th
        th.start()
        return True, "started"

    def housekeeping():
        from dbtriage.storage import open_storage

        store = open_storage(load_config(config_path).storage_url, Path(load_config(config_path).base_dir) / "spool")
        if hasattr(store, "flush"):
            pushed, left = store.flush()
            if pushed:
                print(f"scheduler: synced {pushed} spooled object(s) to the store, {left} left")

    from dbtriage.fleet import Digest, schedule_of as digest_schedule
    from dbtriage.storage import open_storage

    cfg0 = load_config(config_path)
    s = Scheduler(targets, fire, on_tick=housekeeping,
                  digest=Digest(lambda: load_config(config_path), open_storage(cfg0.storage_url, Path(cfg0.base_dir) / "spool")))
    if cron := digest_schedule(cfg0.delivery)[0]:
        print(f"scheduler: fleet digest: {describe(cron)}")
    for r in rows_without_scheduler(targets()):
        if r["schedule"]:
            print(f"scheduler: {r['target']}: {r['label']}{' (paused)' if r['paused'] else ''}, next {r['next']:%Y-%m-%d %H:%M}" if r["next"] else
                  f"scheduler: {r['target']}: {r['label']} (paused)")
    s.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        s.stop.set()
