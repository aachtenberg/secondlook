"""Delivery channels. Each one is a few lines over urllib/smtplib; add a new one by
writing a function `send_<name>(cfg, triage, markdown)` and listing it in CHANNELS.

targets.toml:
    [delivery]
    channels = ["stdout", "ntfy"]      # which to use
    min_severity = "low"               # below this only stdout fires

    [delivery.ntfy]     url = "https://ntfy.example.com/dbtriage"            # token_env optional
    [delivery.slack]    webhook_url_env = "SLACK_WEBHOOK_URL"
    [delivery.teams]    webhook_url_env = "TEAMS_WEBHOOK_URL"                # Incoming Webhook / Workflows URL
    [delivery.email]    smtp_host = "smtp.example.com"  smtp_port = 587  from = "dbtriage@example.com"
                        to = ["dba@example.com"]  user_env = "SMTP_USER"  password_env = "SMTP_PASSWORD"
    [delivery.jira]     base_url = "https://x.atlassian.net"  project_key = "DBA"  issue_type = "Task"
                        user_env = "JIRA_USER"  token_env = "JIRA_TOKEN"  min_severity = "high"
    [delivery.plane]    base_url = "https://plane.example.com"  workspace = "acme"  project_id = "<uuid>"
                        api_key_env = "PLANE_API_KEY"  min_severity = "high"
    [delivery.webhook]  url = "https://..."   # generic JSON POST {triage, markdown}; Datadog/Dynatrace events go here
    [delivery.pushgateway] url = "http://pushgateway:9091"   # Prometheus: dbtriage_severity{target=...} gauge
"""
from __future__ import annotations

import base64
import json
import os
import smtplib
import sys
import urllib.request
from email.message import EmailMessage

RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}
DEFAULT_MIN = {"jira": "high", "plane": "high"}  # tickets are for real problems unless a channel says otherwise


def _env(cfg: dict, key: str) -> str | None:
    name = cfg.get(key)
    return os.environ.get(name) if name else None


def _post(url: str, body, headers: dict | None = None, json_body: bool = True) -> str:
    data = json.dumps(body).encode() if json_body else body
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json" if json_body else "text/plain", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode(errors="replace")


def _subject(t: dict) -> str:
    return f"[{t['severity'].upper()}] db triage {t['target']}: {t['title']}"


def send_stdout(cfg, t, md):
    print(md)


def send_ntfy(cfg, t, md):
    headers = {"Title": _subject(t), "Priority": {"high": "5", "medium": "4", "low": "3"}.get(t["severity"], "2"),
               "Markdown": "yes", "Tags": {"high": "rotating_light", "medium": "warning"}.get(t["severity"], "white_check_mark")}
    if tok := _env(cfg, "token_env"):
        headers["Authorization"] = f"Bearer {tok}"
    url = (cfg.get("url") or "https://ntfy.sh").rstrip("/") + ("/" + cfg["topic"].strip("/") if cfg.get("topic") else "")  # url may already include the topic
    _post(url, md.encode(), headers, json_body=False)


def send_slack(cfg, t, md):
    _post(_env(cfg, "webhook_url_env") or cfg["webhook_url"], {"text": md})


def send_teams(cfg, t, md):
    # Adaptive Card works for both classic Incoming Webhooks and Power Automate "Workflows" URLs.
    card = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
        "msteams": {"width": "Full"},
        "body": [{"type": "TextBlock", "text": _subject(t), "weight": "Bolder", "size": "Medium", "wrap": True},
                 {"type": "TextBlock", "text": md, "wrap": True}]}}]}
    _post(_env(cfg, "webhook_url_env") or cfg["webhook_url"], card)


def send_email(cfg, t, md):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = _subject(t), cfg["from"], ", ".join(cfg["to"])
    msg.set_content(md)
    with smtplib.SMTP(cfg["smtp_host"], int(cfg.get("smtp_port") or 587), timeout=20) as s:
        if cfg.get("starttls", True):
            s.starttls()
        if user := _env(cfg, "user_env"):
            s.login(user, _env(cfg, "password_env") or "")
        s.send_message(msg)


def send_jira(cfg, t, md):
    auth = base64.b64encode(f"{_env(cfg, 'user_env')}:{_env(cfg, 'token_env')}".encode()).decode()
    body = {"fields": {"project": {"key": cfg["project_key"]}, "issuetype": {"name": cfg.get("issue_type") or "Task"},
                       "summary": _subject(t)[:250], "description": md}}
    _post(cfg["base_url"].rstrip("/") + "/rest/api/2/issue", body, {"Authorization": f"Basic {auth}"})


def send_plane(cfg, t, md):
    url = f"{cfg['base_url'].rstrip('/')}/api/v1/workspaces/{cfg['workspace']}/projects/{cfg['project_id']}/issues/"
    pri = {"high": "urgent", "medium": "high", "low": "medium"}.get(t["severity"], "low")
    _post(url, {"name": _subject(t)[:250], "description_html": "<pre>" + md + "</pre>", "priority": pri},
          {"X-API-Key": _env(cfg, "api_key_env") or ""})


def send_webhook(cfg, t, md):
    headers = {k: os.path.expandvars(v) for k, v in cfg.get("headers", {}).items()}
    _post(cfg["url"], {"triage": t, "markdown": md}, headers)


def send_pushgateway(cfg, t, md):
    job = cfg.get("job") or "dbtriage"
    labels = f'target="{t["target"]}",engine="{t["engine"]}"'
    body = (f"# TYPE dbtriage_severity gauge\ndbtriage_severity{{{labels}}} {RANK.get(t['severity'], 0)}\n"
            f"# TYPE dbtriage_last_run_timestamp_seconds gauge\ndbtriage_last_run_timestamp_seconds{{{labels}}} {t.get('ts', 0)}\n")
    url = f"{cfg['url'].rstrip('/')}/metrics/job/{job}/target/{t['target']}"
    req = urllib.request.Request(url, data=body.encode(), method="PUT", headers={"Content-Type": "text/plain"})
    urllib.request.urlopen(req, timeout=20).read()


CHANNELS = {"stdout": send_stdout, "ntfy": send_ntfy, "slack": send_slack, "teams": send_teams, "email": send_email,
            "jira": send_jira, "plane": send_plane, "webhook": send_webhook, "pushgateway": send_pushgateway}


def test_channel(delivery_cfg: dict, name: str, triage: dict, markdown: str) -> tuple[bool, str]:
    """Send one message through one channel regardless of min_severity; used by the UI's 'Send test' button."""
    fn = CHANNELS.get(name)
    if fn is None:
        return False, f"unknown channel {name!r}"
    try:
        fn(delivery_cfg.get(name, {}), triage, markdown)
        return True, f"sent via {name}"
    except KeyError as exc:
        return False, f"{name}: missing setting {exc}"
    except Exception as exc:  # noqa: BLE001 - the message is the point
        return False, f"{name}: {type(exc).__name__}: {str(exc)[:300]}"


def deliver(delivery_cfg: dict, triage: dict, markdown: str, channels: list[str] | None = None) -> list[str]:
    """Send to every configured channel whose min_severity is met. Returns the channels that succeeded;
    failures are reported on stderr and never abort the run."""
    names = channels or delivery_cfg.get("channels", ["stdout"])
    global_min = RANK.get(delivery_cfg.get("min_severity") or "info", 0)
    sev = RANK.get(triage.get("severity", "info"), 0)
    ok = []
    for name in names:
        fn = CHANNELS.get(name)
        if fn is None:
            print(f"deliver: unknown channel {name!r}", file=sys.stderr)
            continue
        cfg = delivery_cfg.get(name, {})
        # a channel's own gate: set ("" = "(global)", the global gate), else the channel's default (tickets: high)
        own = cfg["min_severity"] if cfg.get("min_severity") is not None else DEFAULT_MIN.get(name)
        min_sev = RANK.get(own, global_min) if name != "stdout" else 0
        if sev < min_sev:
            continue
        try:
            fn(cfg, triage, markdown)
            ok.append(name)
        except Exception as exc:  # noqa: BLE001 - one broken channel must not hide the others
            print(f"deliver: {name} failed: {exc}", file=sys.stderr)
    return ok
