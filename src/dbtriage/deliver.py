"""Delivery channels. Each one is a few lines over urllib/smtplib; add a new one by
writing a function `send_<name>(cfg, triage, markdown)` and listing it in CHANNELS.

targets.toml:
    [delivery]
    channels = ["stdout", "ntfy"]      # which to use
    min_severity = "low"               # below this only stdout fires (per finding once a target has a ledger)
    gone_after_runs = 3                # a finding absent this many runs in a row is reported gone (dbtriage.ledger)

    [delivery.ntfy]     url = "https://ntfy.example.com/dbtriage"            # token_env optional
    [delivery.slack]    webhook_url_env = "SLACK_WEBHOOK_URL"
    [delivery.teams]    webhook_url_env = "TEAMS_WEBHOOK_URL"                # Incoming Webhook / Workflows URL
    [delivery.email]    smtp_host = "smtp.example.com"  smtp_port = 587  from = "dbtriage@example.com"
                        to = ["dba@example.com"]  user_env = "SMTP_USER"  password_env = "SMTP_PASSWORD"
    [delivery.jira]     base_url = "https://x.atlassian.net"  project_key = "DBA"  issue_type = "Task"
                        user_env = "JIRA_USER"  token_env = "JIRA_TOKEN"  min_severity = "high"
    [delivery.plane]    base_url = "https://plane.example.com"  workspace = "acme"  project_id = "<uuid>"
                        api_key_env = "PLANE_API_KEY"  min_severity = "high"  project_identifier = "ACME"  # for ACME-12 keys
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


def _post(url: str, body, headers: dict | None = None, json_body: bool = True):
    """POST and return the response body: parsed when it is JSON (Jira and Plane answer with the issue they
    created), the text otherwise."""
    data = json.dumps(body).encode() if json_body else body
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json" if json_body else "text/plain", **(headers or {})})
    from dbtriage.egress import urlopen

    with urlopen(req, 20) as r:  # AWS_CA_BUNDLE when set; the process proxy env still applies
        text = r.read().decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


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


def _jira_auth(cfg) -> dict:
    auth = base64.b64encode(f"{_env(cfg, 'user_env')}:{_env(cfg, 'token_env')}".encode()).decode()
    return {"Authorization": f"Basic {auth}"}


def send_jira(cfg, t, md):
    """Open an issue; returns the created ticket as {"key", "id", "url"} (None when the response names none)."""
    base = cfg["base_url"].rstrip("/")
    body = {"fields": {"project": {"key": cfg["project_key"]}, "issuetype": {"name": cfg.get("issue_type") or "Task"},
                       "summary": _subject(t)[:250], "description": md}}
    resp = _post(base + "/rest/api/2/issue", body, _jira_auth(cfg))
    if isinstance(resp, dict) and resp.get("key"):
        return {"key": str(resp["key"]), "id": str(resp.get("id") or resp["key"]), "url": f"{base}/browse/{resp['key']}"}
    return None


def comment_jira(cfg, ticket: dict, text: str):
    _post(f"{cfg['base_url'].rstrip('/')}/rest/api/2/issue/{ticket['key']}/comment", {"body": text}, _jira_auth(cfg))


def _plane_issues(cfg) -> str:
    return f"{cfg['base_url'].rstrip('/')}/api/v1/workspaces/{cfg['workspace']}/projects/{cfg['project_id']}/issues/"


def send_plane(cfg, t, md):
    """Open a work item; returns {"key", "id", "url"}. The key is `<project_identifier>-<sequence_id>` when the
    channel config names the project's identifier (the API answer does not carry it), else the item's id."""
    pri = {"high": "urgent", "medium": "high", "low": "medium"}.get(t["severity"], "low")
    resp = _post(_plane_issues(cfg), {"name": _subject(t)[:250], "description_html": "<pre>" + md + "</pre>", "priority": pri},
                 {"X-API-Key": _env(cfg, "api_key_env") or ""})
    if not isinstance(resp, dict) or not resp.get("id"):
        return None
    base, ws, ident, seq = cfg["base_url"].rstrip("/"), cfg["workspace"], cfg.get("project_identifier"), resp.get("sequence_id")
    if ident and seq is not None:
        return {"key": f"{ident}-{seq}", "id": str(resp["id"]), "url": f"{base}/{ws}/browse/{ident}-{seq}/"}
    return {"key": str(resp["id"]), "id": str(resp["id"]), "url": f"{base}/{ws}/projects/{cfg['project_id']}/issues/{resp['id']}"}


def comment_plane(cfg, ticket: dict, text: str):
    import html

    _post(f"{_plane_issues(cfg)}{ticket['id']}/comments/", {"comment_html": "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"},
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
    from dbtriage.egress import urlopen

    req = urllib.request.Request(url, data=body.encode(), method="PUT", headers={"Content-Type": "text/plain"})
    urlopen(req, 20).read()


CHANNELS = {"stdout": send_stdout, "ntfy": send_ntfy, "slack": send_slack, "teams": send_teams, "email": send_email,
            "jira": send_jira, "plane": send_plane, "webhook": send_webhook, "pushgateway": send_pushgateway}
COMMENTS = {"jira": comment_jira, "plane": comment_plane}  # ticket channels: a finding with a ticket gets a comment, not a new issue
RUN_GATED = {"pushgateway"}  # describes the run, not a finding: keeps the whole-run gate so "runs stopped" alerts keep working


def _fires(triage: dict, min_sev: int) -> list[int]:
    """Indexes of the findings that justify a post: new or worse, at or above the gate (quiet-unless-worse).
    A still-open medium does not repost; a finding that got worse does."""
    out = []
    for i, (f, l) in enumerate(zip(triage.get("findings") or [], triage.get("life") or [])):
        if l.get("state") in ("new", "worse") and RANK.get(f.get("severity"), 0) >= min_sev:
            out.append(i)
    return out


def _comment_text(f: dict, l: dict, triage: dict, markdown: str) -> str:
    since = (l.get("first_seen_at") or l.get("first_seen") or "")[:10]
    head = f"{l.get('state', 'open').replace('_', ' ')}: {f.get('severity', '')} · {f.get('where', '')}: {f.get('summary', '')}"
    return f"{head}\nopen since {since} · snapshot {triage.get('snapshot', '')}\n\n{markdown}"


def _ticket_run(name: str, cfg: dict, triage: dict, markdown: str, min_sev: int) -> bool:
    """One run against a ticket channel for a review with finding life. New findings share one issue for the run
    (the review is one text); a finding that already has a ticket gets a comment on it; a gone finding's ticket
    gets a "cleared" comment. Tickets made here are put on triage["life"] so the caller can record them in the
    ledger. Returns whether anything was sent."""
    findings, life = triage.get("findings") or [], triage.get("life") or []
    fires = _fires(triage, min_sev)
    create = [i for i in fires if not (life[i].get("tickets") or {}).get(name)]
    comment = [i for i in fires if (life[i].get("tickets") or {}).get(name)]
    sent = False
    if create:
        ticket = CHANNELS[name](cfg, triage, markdown)
        sent = True
        if ticket:
            for i in create:
                life[i].setdefault("tickets", {})[name] = ticket
    for i in comment:
        COMMENTS[name](cfg, life[i]["tickets"][name], _comment_text(findings[i], life[i], triage, markdown))
        sent = True
    for e in triage.get("gone") or []:
        if t := (e.get("tickets") or {}).get(name):
            COMMENTS[name](cfg, t, f"cleared: {e.get('kind', '')} {e.get('subject', '')}: {e.get('title', '')}\n"
                                   f"not seen in the last {e.get('misses', '')} runs · open since {(e.get('first_seen_at') or '')[:10]} "
                                   f"· snapshot {triage.get('snapshot', '')}")
            sent = True
    return sent


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
    failures are reported on stderr and never abort the run.

    The gate is on the finding, not the run, when the review carries `life` (dbtriage.ledger): a channel fires
    when some finding is new or worse at or above its min_severity, so a still-open medium does not repost.
    A review without `life` (a first snapshot, an older store) keeps the whole-run gate. stdout always fires.
    Ticket channels also comment on the tickets of findings that got worse or cleared; the tickets they create
    are written to triage["life"] for the caller to keep (ledger.record_tickets)."""
    names = channels or delivery_cfg.get("channels", ["stdout"])
    global_min = RANK.get(delivery_cfg.get("min_severity") or "info", 0)
    sev = RANK.get(triage.get("severity", "info"), 0)
    per_finding = isinstance(triage.get("life"), list)
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
        try:
            if per_finding and name in COMMENTS:
                if not _ticket_run(name, cfg, triage, markdown, min_sev):
                    continue
            else:
                whole_run = not per_finding or name == "stdout" or name in RUN_GATED
                if (sev < min_sev) if whole_run else not _fires(triage, min_sev):
                    continue
                fn(cfg, triage, markdown)
            ok.append(name)
        except Exception as exc:  # noqa: BLE001 - one broken channel must not hide the others
            print(f"deliver: {name} failed: {exc}", file=sys.stderr)
    return ok
