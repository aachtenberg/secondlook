"""Every trigger produces the same request. Supported entry points:

  * CLI / cron / Ansible / AWX:      dbtriage run --target X --mode scheduled
  * Webhook receiver (dbtriage serve): Alertmanager, Grafana, Datadog, Dynatrace, PagerDuty
    or a plain JSON {"target": ..., "mode": "incident", "window_minutes": 60,
                                        "baseline_policy": "same-time-last-week" | "baseline": "<snapshot id>"}
    The same receiver takes POST /changes, {"target", "time", "kind", "description", "link"} or a list, which
    starts no run: it records a deploy in the target's change feed inbox (dbtriage.inbox).
  * AWS Lambda / EventBridge:         dbtriage.triggers.lambda_handler
  * Azure Functions / GCP Cloud Run:  call handle_payload() the same way

Alert payloads are mapped by looking for a target name in, in order: top-level "target",
alert labels (Alertmanager/Grafana: labels.target or labels.db), Datadog tags / body,
Dynatrace ProblemDetails tags. Unknown payloads are rejected rather than guessed.
"""
from __future__ import annotations

import json
import sys
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

MODES = ("scheduled", "adhoc", "incident")


@dataclass
class TriageRequest:
    target: str
    mode: str = "adhoc"
    window_minutes: int = 60
    end: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "cli"
    reason: str = ""
    channels: list[str] | None = None
    baseline_policy: str | None = None   # previous | same-time-yesterday | same-time-last-week | pinned; None = target default
    baseline: str | None = None          # explicit snapshot id, wins over the policy
    actor: str = ""                      # who started a web run, when the proxy or basic auth names them

    def __post_init__(self):
        # a payload's end without an offset ("2026-11-06T10:00:00") means UTC: every time dbtriage keeps is UTC, and
        # a naive one cannot be compared with them (the window is stretched past a snapshot it takes, for one)
        if self.end.tzinfo is None:
            self.end = self.end.replace(tzinfo=timezone.utc)
        else:
            self.end = self.end.astimezone(timezone.utc)

    @property
    def start(self) -> datetime:
        return self.end - timedelta(minutes=self.window_minutes)


def _find_target(payload: dict) -> tuple[str | None, str]:
    if t := payload.get("target"):
        src = str(payload.get("source") or "webhook")
        return str(t), ("webhook" if src == "schedule" else src)  # "schedule" picks the scheduled model: the scheduler's alone
    alerts = payload.get("alerts")
    if isinstance(alerts, list) and alerts:  # Alertmanager and Grafana unified alerting
        labels = alerts[0].get("labels", {})
        t = labels.get("target") or labels.get("db") or labels.get("database") or labels.get("instance")
        return (str(t) if t else None), ("grafana" if "orgId" in payload or "title" in payload else "alertmanager")
    tags = payload.get("tags")
    if isinstance(tags, (list, str)):  # Datadog monitor webhook with $TAGS, or Dynatrace {Tags}
        tags = tags if isinstance(tags, list) else re.split(r"[,\s]+", tags)
        for tag in tags:
            if tag.startswith(("target:", "db:", "dbtriage_target:")):
                return tag.split(":", 1)[1], "datadog" if "alert_id" in payload or "event_msg" in payload else "dynatrace"
    pd = payload.get("ProblemDetails") or payload.get("problemDetails")
    if isinstance(pd, dict):  # Dynatrace problem notification (custom JSON)
        for tag in pd.get("tags", []) or []:
            k, v = (tag.get("key"), tag.get("value")) if isinstance(tag, dict) else (None, None)
            if k in ("target", "db"):
                return str(v), "dynatrace"
    if "event" in payload and isinstance(payload["event"], dict):  # PagerDuty v3 webhook
        cd = payload["event"].get("data", {}).get("custom_details") or {}
        if cd.get("target"):
            return str(cd["target"]), "pagerduty"
    return None, "unknown"


def parse_payload(payload: dict) -> TriageRequest:
    target, source = _find_target(payload)
    if not target:
        raise ValueError("could not find a target name in the payload (expected 'target', a 'target'/'db' label, or a target: tag)")
    mode = payload.get("mode") or ("incident" if source not in ("webhook", "cli") else "adhoc")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    reason = payload.get("reason") or payload.get("title") or payload.get("event_title") or payload.get("ProblemTitle") or ""
    if isinstance(payload.get("alerts"), list) and payload["alerts"]:
        a = payload["alerts"][0]
        reason = reason or a.get("annotations", {}).get("summary") or a.get("labels", {}).get("alertname", "")
    end = payload.get("end") or payload.get("window_end")
    return TriageRequest(target=target, mode=mode, window_minutes=int(payload.get("window_minutes", 60)),
                         end=datetime.fromisoformat(end) if end else datetime.now(timezone.utc),
                         source=source, reason=str(reason)[:200], channels=payload.get("channels"),
                         baseline_policy=payload.get("baseline_policy"), baseline=payload.get("baseline"))


def lambda_handler(event, context=None):
    """AWS Lambda entry point. Works for EventBridge Scheduler (the event *is* the payload),
    SNS (Records[].Sns.Message) and API Gateway (body)."""
    from dbtriage.cli import run_request

    if "Records" in event:
        event = json.loads(event["Records"][0]["Sns"]["Message"])
    elif "body" in event:
        event = json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]
    result = run_request(parse_payload(event))
    return {"statusCode": 200, "body": json.dumps({"severity": result["severity"], "snapshot": result["snapshot"]})}


def serve(host: str, port: int, token: str | None, store=None, config_path: str | None = None):
    """Minimal webhook receiver: POST / with a JSON payload runs the triage in a thread; POST /changes records a deploy."""
    from dbtriage.config import load_config

    if not token:
        print(f"serve: WARNING no DBTRIAGE_WEBHOOK_TOKEN: anyone who can reach {host}:{port} can start runs and spend model credit",
              file=sys.stderr)
    srv = make_server(host, port, token, cfg_loader=lambda: load_config(config_path), store=store)
    print(f"dbtriage webhook receiver on http://{host}:{port}/ (POST JSON = run; POST /changes = record a deploy; GET = health)")
    srv.serve_forever()


def make_server(host: str, port: int, token: str | None, run=None, cfg_loader=None, store=None) -> HTTPServer:
    """The receiver without serve_forever, so a test can drive it on a free port. The config is loaded again on
    every /changes (like a run loads its own), so a database added on the Settings page can be posted to at once;
    `store` is the process's store, else one is opened from that config."""
    from dbtriage import inbox
    from dbtriage.cli import run_request
    from dbtriage.config import load_config
    from dbtriage.storage import open_storage

    run = run or run_request
    cfg_loader = cfg_loader or load_config

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            if token and self.headers.get("Authorization") != f"Bearer {token}":
                return self._reply(401, {"error": "unauthorized"})
            try:
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or b"{}"))
                if urllib.parse.urlsplit(self.path).path.rstrip("/") == "/changes":
                    return self._changes(payload)
                req = parse_payload(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._reply(400, {"error": str(exc)})
            threading.Thread(target=run, args=(req,), daemon=True).start()
            self._reply(202, {"accepted": True, "target": req.target, "mode": req.mode, "source": req.source})

        def _changes(self, payload):
            """Record what a pipeline says it changed. Nothing is written until every item and target is good, so a
            list is taken whole or not at all and the pipeline's log shows one reason."""
            parsed = inbox.parse(payload)  # ValueError -> 400 in do_POST
            cfg = cfg_loader()
            unknown = sorted({t for t, _ in parsed if t not in cfg.targets})
            if unknown:
                return self._reply(404, {"error": f"unknown target {unknown[0]!r}; known: {', '.join(sorted(cfg.targets)) or '(none)'}"})
            st = store if store is not None else open_storage(cfg.storage_url, Path(cfg.base_dir) / "spool")
            recorded = []
            for name in dict.fromkeys(t for t, _ in parsed):  # one write per target, in the order posted
                try:
                    items = inbox.record(st, cfg.targets[name], [it for t, it in parsed if t == name])
                except inbox.InboxContended as exc:  # the pipeline can post again; what landed so far is reported
                    return self._reply(503, {"error": str(exc), "recorded": recorded})
                recorded += [{"target": name, **it} for it in items]
            self._reply(202, {"accepted": True, "recorded": recorded})

        def do_GET(self):  # noqa: N802 - health check
            self._reply(200, {"ok": True})

        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            print(f"serve: {self.address_string()} {fmt % args}")

    return HTTPServer((host, port), H)
