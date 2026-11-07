"""Command line: collect | compare | triage | run | list | show | serve."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dbtriage import compare as cmp
from dbtriage import dbsnap
from dbtriage.collectors import get_collector
from dbtriage.collectors.base import Window
from dbtriage.config import Config, load_config
from dbtriage.deliver import deliver
from dbtriage.report import render_markdown
from dbtriage.storage import Storage, open_storage
from dbtriage.baseline import POLICIES
from dbtriage.triggers import MODES, TriageRequest


# ---------------------------------------------------------------- pieces
def collect(cfg: Config, store: Storage, req: TriageRequest) -> str:
    target = cfg.target(req.target)
    if not target.dsn:
        env = target.extra.get("dsn_env")
        raise SystemExit(f"target {target.name!r}: no connection string" + (f": environment variable {env} is not set" if env else ""))
    window = Window(start=req.start, end=req.end, mode=req.mode)
    db_snapshot = dbsnap.take(cfg.collect, target, window)  # opt-in, before collecting, so the window ends with it
    if db_snapshot and db_snapshot["status"] in ("skipped", "failed"):
        print(f"{target.name}: no snapshot taken on the database: {db_snapshot['reason']}", file=sys.stderr)
    if db_snapshot and db_snapshot["status"] == "taken":
        # the window was set when the request came in, before the snapshot: stretch its end past it, so it is in the
        # window even if another snapshot landed in between (that one's interval would begin after the old end)
        window = Window(start=window.start, end=max(window.end, datetime.now(timezone.utc)), mode=window.mode)
    snap = get_collector(target).collect(window)
    captured = datetime.fromisoformat(snap.summary["captured_at"])
    sid = f"{target.engine}/{target.name}/{captured:%Y-%m-%d}/{captured:%Y%m%dT%H%M%SZ}"
    n = 1
    while store.exists(f"{sid}/manifest.json"):  # two runs in the same second must not overwrite each other
        n += 1
        sid = f"{sid.rsplit('-', 1)[0] if n > 2 else sid}-{n}"
    store.put_json(f"{sid}/summary.json", snap.summary)
    store.put_json(f"{sid}/raw.json", snap.raw)
    for name, text in snap.reports.items():
        store.put_text(f"{sid}/{name}", text)
    store.put_json(f"{sid}/manifest.json", {
        "id": sid, "engine": target.engine, "target": target.name, "captured_at": snap.summary["captured_at"],
        "window": window.as_dict(), "metrics_kind": snap.summary.get("metrics_kind", "cumulative"),
        "trigger": {"source": req.source, "reason": req.reason},
        "files": ["summary.json", "raw.json"], "reports": sorted(snap.reports),
        "changes_file": target.changes_file,
        **({"db_snapshot": db_snapshot} if db_snapshot else {}),
    })
    return sid


def summary_of(store: Storage, sid: str) -> dict:
    s = store.get_json(f"{sid}/summary.json")
    s["id"] = sid
    return s


def pick_baseline(cfg: Config, store: Storage, sid: str, policy: str | None = None, explicit: str | None = None) -> tuple[str | None, str, str]:
    """Resolve the baseline for a snapshot: explicit id > requested policy > target's baseline_policy > previous."""
    manifest = store.get_json(f"{sid}/manifest.json")
    engine, name = manifest["engine"], manifest["target"]
    t = cfg.targets.get(name)
    extra = t.extra if t else {}
    return store.pick_baseline(engine, name, sid, policy or extra.get("baseline_policy"), extra.get("baseline"), explicit)


def compare_ids(store: Storage, engine: str, target: str, sid: str, baseline: str | None, how: tuple[str, str] | None = None) -> dict | None:
    """compare() for a snapshot id, resolving the baseline and the one before it. None if nothing to compare.
    `how` = (policy, reason) is recorded under reference so the review can say why this baseline."""
    cur = summary_of(store, sid)
    if cur.get("metrics_kind") == "interval":
        out = cmp.compare(cur, summary_of(store, baseline) if baseline else None)
    elif not baseline:
        return None
    else:
        before = store.previous(engine, target, baseline)
        out = cmp.compare(cur, summary_of(store, baseline), summary_of(store, before) if before else None)
    if out.get("reference") is not None and how:
        out["reference"]["policy"], out["reference"]["reason"] = how
    return out


def triage(cfg: Config, store: Storage, sid: str, baseline: str | None, mode: str, use_llm: bool,
           how: tuple[str, str] | None = None, source: str | None = None) -> tuple[dict, dict | None]:
    manifest = store.get_json(f"{sid}/manifest.json")
    engine, target = manifest["engine"], manifest["target"]
    comparison = compare_ids(store, engine, target, sid, baseline, how)
    if how:
        manifest["baseline"] = {"snapshot": baseline, "policy": how[0], "reason": how[1]}
        store.put_json(f"{sid}/manifest.json", manifest)
    from dbtriage.agent import TriageAgent, rule_based_triage

    result = None
    if use_llm:
        try:
            from dbtriage.llm import agent_for, make_backend

            t = cfg.targets.get(target)
            agent = agent_for(cfg.agent, source)  # a scheduled run may use [agent] scheduled_model
            result = TriageAgent(store, make_backend(agent)).run(
                engine, target, sid, baseline, mode, max_iterations=int(agent.get("max_iterations") or 25),
                notes=(t.extra.get("notes") if t else None))
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - an outage or a missing key must not lose the run: use the rules
            print(f"agent failed ({type(exc).__name__}: {str(exc)[:300]}); using rules-based triage", file=sys.stderr)
            agent_error = f"{type(exc).__name__}: {str(exc)[:300]}"
    if result is None:
        result = rule_based_triage(comparison or {"findings": [], "severity": "info"})
        if use_llm:
            result["agent_error"] = agent_error
        if comparison is None:
            result["summary"] = "First snapshot for this target; nothing to compare against yet."
    result.update({"snapshot": sid, "baseline": baseline, "target": target, "engine": engine, "mode": mode,
                   "baseline_policy": how[0] if how else None, "baseline_reason": how[1] if how else None,
                   "ts": int(time.time()), "heuristics": (comparison or {}).get("findings", [])})
    md = render_markdown(result, comparison)
    store.put_json(f"{sid}/triage.json", result)
    store.put_text(f"{sid}/triage.md", md)
    if comparison is not None:
        store.put_json(f"{sid}/compare.json", comparison)
    return result, comparison


def run_request(req: TriageRequest, cfg: Config | None = None, use_llm: bool | None = None, baseline: str | None = None) -> dict:
    """The whole pipeline for one request: collect -> compare with the baseline -> triage -> deliver."""
    cfg = cfg or load_config()
    store = open_storage(cfg.storage_url, Path(cfg.base_dir) / "spool")
    cfg.target(req.target)  # fail early on an unknown target
    if use_llm is None:
        use_llm = os.environ.get("DBTRIAGE_NO_LLM", "") == ""
    sid = collect(cfg, store, req)
    baseline, policy, reason = pick_baseline(cfg, store, sid, req.baseline_policy, baseline or req.baseline)
    result, comparison = triage(cfg, store, sid, baseline, req.mode, use_llm, (policy, reason), source=req.source)
    result["delivered"] = deliver(cfg.delivery, result, render_markdown(result, comparison), req.channels)
    return result


# ---------------------------------------------------------------- CLI
def _req(args) -> TriageRequest:
    end = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)  # TriageRequest makes it UTC
    return TriageRequest(target=args.target, mode=args.mode, window_minutes=args.window, end=end, source="cli",
                         reason=args.reason or "", channels=args.channels.split(",") if args.channels else None,
                         baseline_policy=getattr(args, "baseline_policy", None), baseline=getattr(args, "baseline", None))


def main(argv=None):
    p = argparse.ArgumentParser(prog="dbtriage", description=__doc__)
    p.add_argument("--config", "-c", help="targets.toml (default: $DBTRIAGE_CONFIG or ./targets.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_req(sp):
        sp.add_argument("--target", "-t", required=True)
        sp.add_argument("--mode", choices=MODES, default="adhoc")
        sp.add_argument("--window", type=int, default=60, help="minutes before --end (default 60)")
        sp.add_argument("--end", help="ISO timestamp; without an offset it is UTC, like every time dbtriage keeps (default now)")
        sp.add_argument("--reason", help="why this run happened (alert name, ticket)")
        sp.add_argument("--channels", help="comma-separated delivery channels; default from config")

    add_req(sub.add_parser("collect", help="take a snapshot and store it"))
    sp = sub.add_parser("compare", help="compare a snapshot with a baseline (JSON)")
    sp.add_argument("--target", "-t", required=True)
    sp.add_argument("--snapshot", help="default: latest")
    sp.add_argument("--baseline", help="default: per the target's baseline_policy (previous snapshot)")
    sp.add_argument("--baseline-policy", choices=POLICIES, help="override the target's baseline_policy for this run")
    sp = sub.add_parser("triage", help="triage a stored snapshot (agent, or --no-llm rules)")
    sp.add_argument("--target", "-t", required=True)
    sp.add_argument("--snapshot")
    sp.add_argument("--baseline")
    sp.add_argument("--baseline-policy", choices=POLICIES)
    sp.add_argument("--mode", choices=MODES, default="adhoc")
    sp.add_argument("--no-llm", action="store_true")
    sp.add_argument("--deliver", action="store_true", help="also send to the configured channels")
    sp = sub.add_parser("run", help="collect + compare with the baseline + triage + deliver")
    add_req(sp)
    sp.add_argument("--no-llm", action="store_true")
    sp.add_argument("--baseline", help="compare against this snapshot id (wins over any policy)")
    sp.add_argument("--baseline-policy", choices=POLICIES, help="previous | same-time-yesterday | same-time-last-week | pinned (default: target's baseline_policy)")
    sp = sub.add_parser("deliver", help="re-send a stored review to delivery channels")
    sp.add_argument("snapshot")
    sp.add_argument("--channel", help="comma-separated channel names (default: configured channels)")
    sp = sub.add_parser("list", help="list stored snapshots")
    sp.add_argument("--target", "-t", required=True)
    sp = sub.add_parser("show", help="print a stored file (summary.json, compare.json, triage.md, awr.txt ...)")
    sp.add_argument("snapshot")
    sp.add_argument("file", nargs="?", default="triage.md")
    sp = sub.add_parser("serve", help="webhook receiver for alert-driven runs")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--no-llm", action="store_true")
    sp = sub.add_parser("web", help="web view of the store (--allow-run adds a run button, --allow-edit lets the UI edit targets and branding, "
                                    "--allow-chat lets you discuss a finding with the model)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8081)
    sp.add_argument("--allow-run", action="store_true")
    sp.add_argument("--allow-edit", action="store_true")
    sp.add_argument("--allow-chat", action="store_true")
    sp.add_argument("--schedule", action="store_true", help="run the built-in scheduler in this process (needs --allow-run)")
    sub.add_parser("mcp", help="MCP server over stdio for MCP clients (Claude Code, Claude Desktop ...): read the stored "
                               "snapshots and reviews; off unless [mcp] enabled = true in targets.toml (uv sync --extra mcp)")
    sp = sub.add_parser("flush-spool", help="push snapshots spooled during a store outage to the store now")
    sp = sub.add_parser("copy-store", help="copy every object from one store URL to another (e.g. file://./data -> postgres://...)")
    sp.add_argument("--from", dest="src", required=True)
    sp.add_argument("--to", dest="dst", required=True)
    sp.add_argument("--prefix", default="", help="only keys under this prefix, e.g. oracle/ora-lab/")
    sp.add_argument("--overwrite", action="store_true", help="replace keys that already exist in the destination")
    sp = sub.add_parser("copy-settings", help="copy the UI settings overlay between a JSON file and a postgres:// URL")
    sp.add_argument("--from", dest="src", required=True)
    sp.add_argument("--to", dest="dst", required=True)
    sp = sub.add_parser("scheduler", help="run scheduled targets from this host without the web view (re-reads the config every tick)")
    sp.add_argument("--no-llm", action="store_true")
    sp = sub.add_parser("ask", help="discuss a stored review with the model (answers come from the snapshot, never a live database)")
    sp.add_argument("snapshot")
    sp.add_argument("question", nargs="?", help="omit to print the thread so far")
    sp.add_argument("--finding", "-f", type=int, help="1-based finding number; omit to discuss the whole review")

    args = p.parse_args(argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)  # log lines show up at once under nohup, systemd or docker
    except AttributeError:
        pass
    from dbtriage import envfile

    envfile.init(args.config)  # .env next to targets.toml; the web view and scheduler re-read it when it changes
    cfg = load_config(args.config)
    store = open_storage(cfg.storage_url, Path(cfg.base_dir) / "spool")

    if args.cmd == "collect":
        print(collect(cfg, store, _req(args)))
    elif args.cmd == "list":
        t = cfg.target(args.target)
        for sid in store.list_snapshots(t.engine, t.name):
            m = store.get_json(f"{sid}/manifest.json")
            flag = "triaged" if store.exists(f"{sid}/triage.json") else ""
            print(f"{sid}  {m['window']['mode']:9} {m['trigger'].get('source', ''):10} {flag}")
    elif args.cmd == "show":
        sys.stdout.write(store.get(f"{args.snapshot}/{args.file}").decode())
    elif args.cmd == "deliver":
        if not store.exists(f"{args.snapshot}/triage.json"):
            sys.exit(f"{args.snapshot}: not triaged yet")
        tr, md = store.get_json(f"{args.snapshot}/triage.json"), store.get(f"{args.snapshot}/triage.md").decode()
        done = deliver(cfg.delivery, tr, md, args.channel.split(",") if args.channel else None)
        print(f"delivered to: {', '.join(done) or 'nothing'}", file=sys.stderr)
    elif args.cmd == "compare":
        t = cfg.target(args.target)
        sid = args.snapshot or store.latest(t.engine, t.name) or sys.exit("no snapshots yet")
        base, policy, reason = pick_baseline(cfg, store, sid, args.baseline_policy, args.baseline)
        result = compare_ids(store, t.engine, t.name, sid, base, (policy, reason))
        print(json.dumps(result if result is not None else {"error": "no baseline: need two snapshots"}, indent=1, default=str))
    elif args.cmd == "triage":
        t = cfg.target(args.target)
        sid = args.snapshot or store.latest(t.engine, t.name) or sys.exit("no snapshots yet")
        base, policy, reason = pick_baseline(cfg, store, sid, args.baseline_policy, args.baseline)
        result, comparison = triage(cfg, store, sid, base, args.mode, not args.no_llm, (policy, reason))
        md = render_markdown(result, comparison)
        if args.deliver:
            result["delivered"] = deliver(cfg.delivery, result, md)
        if not args.deliver or "stdout" not in result["delivered"]:
            print(md)
    elif args.cmd == "run":
        result = run_request(_req(args), cfg, use_llm=not args.no_llm, baseline=args.baseline)
        if "stdout" not in result.get("delivered", []):
            print(f"{result['severity']}: {result['title']}  ({result['snapshot']})")
    elif args.cmd == "serve":
        from dbtriage.triggers import serve

        if args.no_llm:
            os.environ["DBTRIAGE_NO_LLM"] = "1"
        serve(args.host, args.port, os.environ.get("DBTRIAGE_WEBHOOK_TOKEN"))
    elif args.cmd == "ask":
        from dbtriage import chat
        from dbtriage.llm import make_backend

        key = chat.thread_key(args.finding)
        if args.question:
            ans = chat.Chat(store, lambda: make_backend(cfg.agent)).ask(args.snapshot, args.finding, args.question)
            print(ans["text"])
            print(f"\n_{chat.footer(ans)}_", file=sys.stderr)
        else:
            print(chat.thread_markdown(chat.load_thread(store, args.snapshot, key)))
    elif args.cmd == "web":
        from dbtriage.web import serve as web_serve

        web_serve(cfg, store, args.host, args.port, args.allow_run, os.environ.get("DBTRIAGE_WEB_TOKEN"), args.allow_edit, args.allow_chat, args.schedule)
    elif args.cmd == "mcp":
        from dbtriage import mcp_server

        raise SystemExit(mcp_server.main(cfg, store))
    elif args.cmd == "flush-spool":
        if not hasattr(store, "flush"):
            print("this store is local; nothing is ever spooled")
        else:
            pushed, left = store.flush()
            print(f"pushed {pushed}, still spooled {left}" + (f" (store unreachable: {store.degraded})" if left else ""))
            sys.exit(1 if left else 0)
    elif args.cmd == "copy-store":
        from dbtriage.storage import describe

        src, dst = open_storage(args.src), open_storage(args.dst)
        if hasattr(dst, "primary"):
            dst = dst.primary  # a copy must reach the real store or fail, never land in the spool
        if hasattr(src, "primary"):
            src = src.primary
        from dbtriage.storage import LocalStorage, key_ok

        keys = src.list_keys(args.prefix)
        if isinstance(dst, LocalStorage):  # a disk store refuses keys a filesystem would treat specially: say which
            refused = [k for k in keys if not key_ok(k)]
            for k in refused[:20]:
                print(f"  skipped {k!r}: not a key a local store takes (see storage.key_ok)", file=sys.stderr)
            keys = [k for k in keys if key_ok(k)]
        have = set() if args.overwrite else set(dst.list_keys(args.prefix))
        todo = [k for k in keys if k not in have]
        for i, k in enumerate(sorted(todo, key=lambda k: (k.endswith("/manifest.json"), k)), 1):
            dst.put(k, src.get(k))
            if i % 200 == 0:
                print(f"  {i}/{len(todo)}", file=sys.stderr)
        print(f"copied {len(todo)} of {len(keys)} object(s) from {describe(src)} to {describe(dst)} ({len(keys) - len(todo)} already there)")
    elif args.cmd == "copy-settings":
        from dbtriage.settings import Settings

        src = Settings(args.src)
        if src.degraded:
            sys.exit(f"source unreachable: {src.degraded}")
        dst = Settings(args.dst, cache=Path(cfg.base_dir) / "dbtriage-settings.cache.json")
        dst.data = src.data
        dst.save()
        print(f"copied settings ({', '.join(f'{k}: {len(v)}' for k, v in src.data.items())}) from {src.where} to {dst.where}")
    elif args.cmd == "scheduler":
        from dbtriage.scheduler import run_standalone

        run_standalone(args.config, use_llm=not args.no_llm)


def entry():
    try:
        main()
    except BrokenPipeError:  # `dbtriage show ... | head`
        sys.exit(0)


if __name__ == "__main__":
    entry()
