"""Web view over the snapshot store, stdlib only (no assets, no CDN, works in locked-down
networks). HTML pages for people, a JSON API for everything else (Grafana, portals, scripts).

    dbtriage web --port 8081 [--allow-run] [--allow-edit] [--allow-chat]

Branding and behaviour come from [web] in targets.toml; look and feel can also be overridden
file by file with templates_dir and custom_css (see examples/branding). Auth: put it behind the
SSO proxy you already have, or set DBTRIAGE_WEB_TOKEN for HTTP Basic (any user, password = token).

API (same auth):
  GET  /api/targets                         targets with their latest triage
  GET  /api/targets/<name>/snapshots        ?limit=50&offset=0
  GET  /api/targets/<name>/trend            ?metric=db_time_ms&limit=200   one load metric per snapshot, for charts
  GET  /api/targets/<name>/sql/<sql_id>     per-snapshot history of one statement (calls, time, CPU time, plan hash)
  GET  /api/snapshots/<id>                  manifest + triage + findings + top SQL
  GET  /api/snapshots/<id>/files/<name>     a stored file (summary.json, awr.txt ...)
  POST /api/run                             {"target", "mode", "reason", "no_llm", "baseline_policy", "baseline"}  (needs --allow-run)
  GET  /api/settings                        effective settings + targets (DSNs masked) + allow_db_writes (read-only)
  PUT  /api/settings                        {"web": {...}, "agent": {...}, "delivery": {...}} (a flat body = web)  (needs --allow-edit);
                                            refuses allow_db_writes / [collect]: targets.toml only
  PUT  /api/targets/<name>                  {"engine", "dsn"|"dsn_env", "top_n", "links": [...], ...}  (needs --allow-edit)
  DELETE /api/targets/<name>                                                                        (needs --allow-edit)
  POST /api/targets/<name>/probe            test the connection to a configured target (read-only)
  POST /api/settings/agent/test             one cheap model round trip -> {"ok", "message"}  (needs --allow-edit)
  GET  /api/settings/agent/models           ?provider=  the models a provider offers                (needs --allow-edit)
  POST /api/settings/delivery/<channel>/test  post the latest review to that channel -> {"ok", "message"}  (needs --allow-edit)
  GET  /api/schedule                        per-target schedule, next and last run
  POST /api/schedule/<name>                 {"schedule": "0 * * * *" | "every 15m", "schedule_window": 60, "schedule_paused": false}  (needs --allow-edit)
  POST /api/schedule/<name>/run             collect and review now with the scheduled window  (needs --allow-run)
  GET  /api/snapshots/<id>/chat/<key>       discussion thread; key = review | finding-<n> | sql-<sql id>
  POST /api/snapshots/<id>/chat/<key>       {"question"} -> the model's answer, grounded in the stored snapshot  (needs --allow-chat)
  GET  /health

Everything the UI edits is kept in one JSON overlay file (see dbtriage.settings); targets.toml stays the seed.
"""
from __future__ import annotations

import base64
import hmac
import os
import html
import json
import math
import mimetypes
import re
import string
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from dbtriage import access, charts, chat, dbsnap, envfile, llm, mcp_server, scheduler
from dbtriage.baseline import LABELS, POLICIES
from dbtriage.config import Config, top_n_of
from dbtriage.findings import SEVERITY_RANK, dropped, rules_dropped
from dbtriage.ledger import describe_period
from dbtriage.settings import AGENT_FIELDS, AGENT_PROVIDERS, CHANNEL_FIELDS, ENGINES, SCHEDULE_FIELDS, SEVERITIES, WEB_FIELDS, Settings, mask_dsn, probe
from dbtriage.settings import SettingsUnavailable
from dbtriage.storage import Storage, StorageUnavailable, describe as describe_store, key_ok

DEFAULTS = {
    "title": "dbtriage", "brand": "dbtriage", "logo": None, "accent": "#3dd68c", "theme": "auto",
    "refresh_seconds": 30, "page_size": 50, "footer": "", "custom_css": None, "templates_dir": None,
    "files": ["triage.md", "compare.json", "summary.json", "manifest.json", "raw.json", "tools.json", "changes.json", "awr.txt", "addm.txt"],
    "severity_colors": {"high": "#c42f2f", "medium": "#a8540a", "low": "#8a6a00", "info": "#17804a"},
}

CSS = """
:root{--accent:$accent;--on-accent:#0f1115;--bg:#f5f6f8;--card:#ffffff;--card2:#f0f2f5;--active:#e7eaf0;--line:#e6e8ee;--line2:#d5d9e1;
--fg:#1d2129;--fg-strong:#0f1115;--fg2:#3a404a;--mut:#5c636f;--link:#2457c5;--high:$c_high;--medium:$c_medium;--low:$c_low;--info:$c_info;--up:#c42f2f;--down:#2457c5;--ai:#4a57d6;--ai-bg:rgba(74,87,214,.12);color-scheme:light;
--sans:Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--r:10px}
@media(prefers-color-scheme:dark){:root[data-theme=auto]{--bg:#0f1115;--card:#13161c;--card2:#161920;--active:#1a1e27;--line:#1d2028;--line2:#262a33;--fg:#e6e8ec;--fg-strong:#f3f4f6;--fg2:#c3c7cf;--mut:#8b919c;--link:#8ab4ff;--high:#ff6b6b;--medium:#ffa33a;--low:#e8c547;--info:#3dd68c;--up:#ff6b6b;--down:#8ab4ff;--ai:#6d7cff;--ai-bg:rgba(109,124,255,.16);color-scheme:dark}}
:root[data-theme=dark]{--bg:#0f1115;--card:#13161c;--card2:#161920;--active:#1a1e27;--line:#1d2028;--line2:#262a33;--fg:#e6e8ec;--fg-strong:#f3f4f6;--fg2:#c3c7cf;--mut:#8b919c;--link:#8ab4ff;--high:#ff6b6b;--medium:#ffa33a;--low:#e8c547;--info:#3dd68c;--up:#ff6b6b;--down:#8ab4ff;--ai:#6d7cff;--ai-bg:rgba(109,124,255,.16);color-scheme:dark}
*{box-sizing:border-box}body{font:14px/1.45 var(--sans);background:var(--bg);color:var(--fg);margin:0;-webkit-font-smoothing:antialiased}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}code,pre,.mono,td.ts,td:first-child a.ts{font-family:var(--mono)}
h1,h2,h3{font-weight:600;margin:0}h1{font-size:24px;letter-spacing:-.02em;color:var(--fg-strong)}h2{font-size:14px}h3{font-size:13px}
.mut{color:var(--mut)}.err{color:var(--high)}.ok{color:var(--info)}
/* header */
header.top{height:56px;display:flex;align-items:center;justify-content:space-between;gap:12px;padding:0 20px;border-bottom:1px solid var(--line);background:var(--bg)}
header .left,header .right{display:flex;align-items:center;gap:10px}
a.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:15px;letter-spacing:-.01em;color:var(--fg-strong)}a.brand:hover{text-decoration:none}
a.brand img{height:26px;width:auto;border-radius:7px}.badge{width:26px;height:26px;border-radius:7px;background:var(--accent);color:var(--on-accent);display:inline-flex;align-items:center;justify-content:center}
button.icon,.navtoggle{width:36px;height:36px;display:inline-flex;align-items:center;justify-content:center;background:transparent;border:0;border-radius:8px;color:var(--mut);cursor:pointer;padding:0}
button.icon:hover,.navtoggle:hover{background:var(--card2);color:var(--fg)}
button.theme{width:34px;padding:0;justify-content:center;color:var(--fg2)}button.theme .moon{display:none}:root[data-theme=light] button.theme .sun{display:none}:root[data-theme=light] button.theme .moon{display:inline}@media(prefers-color-scheme:light){:root[data-theme=auto] button.theme .sun{display:none}:root[data-theme=auto] button.theme .moon{display:inline}}
form.search{position:relative;display:flex;align-items:center;margin:0}form.search .ico{position:absolute;left:10px;color:var(--mut);display:inline-flex;pointer-events:none}
form.search input{height:34px;width:260px;padding:0 12px 0 32px;border-radius:8px;border:1px solid var(--line2);background:var(--card2);color:var(--fg);font:inherit;font-size:13px}
form.search input:focus{outline:2px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:0}
.btn,button,input[type=submit]{height:34px;padding:0 12px;display:inline-flex;align-items:center;gap:6px;border-radius:8px;border:1px solid var(--line2);background:var(--card2);color:var(--fg2);font:inherit;font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap}
.btn:hover,button:hover{background:var(--active);color:var(--fg-strong);text-decoration:none}
button.primary,.btn.primary{background:var(--accent);border-color:transparent;color:var(--on-accent);font-weight:600}button.primary:hover,.btn.primary:hover{filter:brightness(1.08);color:var(--on-accent)}
button.danger{background:color-mix(in srgb,var(--high) 15%,transparent);border-color:transparent;color:var(--high)}button.quiet{background:transparent}
button.sm,.btn.sm{height:28px;padding:0 10px;font-size:12px;border-radius:6px}button:disabled{opacity:.5;cursor:default}
select,input,textarea{font:inherit;font-size:13px;height:34px;padding:0 10px;border:1px solid var(--line2);border-radius:8px;background:var(--card2);color:var(--fg)}textarea{height:auto;padding:8px 10px}
option,optgroup{background:var(--card2);color:var(--fg)}
label.pill{display:inline-flex;align-items:center;height:30px;padding:0 10px;border-radius:8px;border:1px solid var(--line2);background:var(--card2);font-family:var(--mono);font-size:13px}
label.pill select{height:auto;border:0;background:transparent;padding:0 16px 0 0;font:inherit;appearance:none;-webkit-appearance:none;color:var(--fg)}
label.pill::after{content:"";width:6px;height:6px;border-right:2px solid var(--mut);border-bottom:2px solid var(--mut);transform:rotate(45deg);margin:-4px 0 0 -10px}
/* layout */
.shell{display:flex;flex-wrap:wrap;min-height:calc(100vh - 56px)}
nav.side{flex:0 1 240px;min-width:200px;padding:20px 12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:14px}
nav.side .navhead{display:flex;align-items:center;justify-content:space-between;padding:0 8px}nav.side h3{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}
nav.side a.add{width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;border:1px solid var(--line2);border-radius:6px;color:var(--mut)}nav.side a.add:hover{color:var(--fg);text-decoration:none;background:var(--card2)}
nav.side .items{display:flex;flex-direction:column;gap:2px}
nav.side a.t{display:flex;align-items:center;gap:10px;height:40px;padding:0 10px;border-radius:8px;color:var(--fg2);font-weight:500}nav.side a.t:hover{background:var(--card2);text-decoration:none}
nav.side a.t.active{background:var(--active);color:var(--fg-strong)}nav.side a .cnt{margin-left:auto;font-size:11px;color:var(--mut);font-family:var(--mono)}
nav.side .sub{display:flex;flex-direction:column;gap:1px;padding:4px 0 6px 18px;border-left:1px solid var(--line2);margin-left:14px}
nav.side .sub a{display:flex;align-items:center;gap:10px;height:32px;padding:0 10px;border-radius:6px;color:var(--fg2);font-size:13px;font-family:var(--mono)}nav.side .sub a:hover{background:var(--card2);text-decoration:none}
nav.side .sub a.active{background:var(--active);color:var(--fg-strong)}nav.side .sub .dot{width:6px;height:6px}
.dot{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block;background:var(--mut)}
.dot.high{background:var(--high)}.dot.medium{background:var(--medium)}.dot.low{background:var(--low)}.dot.info{background:var(--info)}
nav.side a.t .dot{box-shadow:0 0 0 3px color-mix(in srgb,currentColor 0%,transparent)}
nav.side a.t .dot.high{box-shadow:0 0 0 3px color-mix(in srgb,var(--high) 15%,transparent)}nav.side a.t .dot.medium{box-shadow:0 0 0 3px color-mix(in srgb,var(--medium) 15%,transparent)}
nav.side a.t .dot.low{box-shadow:0 0 0 3px color-mix(in srgb,var(--low) 15%,transparent)}nav.side a.t .dot.info{box-shadow:0 0 0 3px color-mix(in srgb,var(--info) 15%,transparent)}
body.nav-collapsed nav.side{display:none}
main{flex:999 1 560px;min-width:0;padding:28px 32px 48px;display:flex;flex-direction:column;gap:20px;max-width:1200px}
a.brand span{white-space:nowrap}
@media(max-width:820px){nav.side{flex-basis:100%;border-right:0;border-bottom:1px solid var(--line)}main{padding:20px 16px 40px}header.top{padding:0 12px}header .right{flex:1;justify-content:flex-end;min-width:0}form.search{flex:1;min-width:0}form.search input{width:100%;min-width:0}}
footer{color:var(--mut);font-size:12px;padding:16px 32px;border-top:1px solid var(--line)}
/* page head */
.pagehead{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px}.pagehead .lead{display:flex;flex-direction:column;gap:6px;min-width:0;max-width:100%}.pagehead h1,.pagehead .crumbs{overflow-wrap:anywhere;min-width:0}
.crumbs{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:6px}.crumbs a{color:var(--mut)}
.titlerow{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.chip{font-size:12px;font-weight:500;padding:3px 8px;border-radius:999px;border:1px solid var(--line2);color:var(--fg2);font-family:var(--mono)}
.status{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:500}.status .dot{width:6px;height:6px}
.status.info{color:var(--info)}.status.low,.status.medium{color:var(--medium)}.status.high{color:var(--high)}
.actions{display:flex;gap:8px;flex-wrap:wrap}.meta{font-size:12px;color:var(--mut);display:flex;gap:10px;flex-wrap:wrap;align-items:center}
/* tiles */
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{padding:14px 16px;border-radius:var(--r);border:1px solid var(--line);background:var(--card);display:flex;flex-direction:column;gap:6px}
.tile .lbl{font-size:12px;color:var(--mut)}.tile .num{font-size:22px;font-weight:600;letter-spacing:-.02em;font-family:var(--mono);line-height:1.1;color:var(--fg-strong)}
.tile .num .delta{font-size:12px;font-weight:500;margin-left:6px;color:var(--mut)}.tile .num.high{color:var(--high)}.tile .num.medium{color:var(--medium)}.tile .num.low{color:var(--low)}.tile .num.info{color:var(--info)}
/* cards and tables */
.card{border-radius:12px;border:1px solid var(--line);background:var(--card);padding:16px 20px;min-width:0;overflow-wrap:anywhere}.card table{overflow-wrap:normal}.card.flush{padding:0;overflow:hidden}
.cardhead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}.card.flush .cardhead{padding:14px 16px;border-bottom:1px solid var(--line)}
.cardhead .left{display:flex;align-items:center;gap:12px}.cardhead .right{display:flex;align-items:center;gap:16px;font-size:12px;color:var(--mut)}
.chips{display:flex;gap:6px}.chips a{height:28px;padding:0 10px;display:inline-flex;align-items:center;border-radius:6px;border:1px solid transparent;color:var(--mut);font-size:12px;font-weight:500}
.chips a:hover{text-decoration:none;color:var(--fg)}.chips a.on{background:var(--active);border-color:var(--line2);color:var(--fg-strong)}
.scroll{overflow-x:auto}table.list{border-collapse:collapse;width:100%}
th{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mut);text-align:left;padding:10px 12px;border-bottom:1px solid var(--line2)}
.sortbtn{all:unset;cursor:pointer;white-space:nowrap}.sortbtn:hover,th[aria-sort] .sortbtn{color:var(--fg)}.sortbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}th[aria-sort=ascending] .sortbtn::after{content:" ↑";content:" ↑"/""}th[aria-sort=descending] .sortbtn::after{content:" ↓";content:" ↓"/""}
td{padding:12px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--card2)}
td:first-child,td.ts{white-space:nowrap}td.ts a,td:first-child a.ts{font-family:var(--mono);font-size:13px}td.r,th.r{text-align:right}td.dim{color:var(--fg2)}
.finding{display:flex;flex-direction:column;gap:4px}.finding b{font-weight:600;color:var(--fg-strong)}
.idchip{font-family:var(--mono);font-size:12px;color:var(--mut);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:2px 6px;width:fit-content}
.sev{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 8px;border-radius:999px;font-size:12px;font-weight:500;background:color-mix(in srgb,var(--mut) 12%,transparent);color:var(--mut)}
.sev .dot{width:6px;height:6px}.sev.high{background:color-mix(in srgb,var(--high) 12%,transparent);color:var(--high)}.sev.medium{background:color-mix(in srgb,var(--medium) 12%,transparent);color:var(--medium)}
.sev.low{background:color-mix(in srgb,var(--low) 14%,transparent);color:var(--low)}.sev.info{background:color-mix(in srgb,var(--info) 12%,transparent);color:var(--info)}
.pager{display:flex;gap:10px;padding:10px 16px;font-size:12px;color:var(--mut);border-top:1px solid var(--line)}
pre{white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);padding:10px 12px;border-radius:8px;overflow:auto;font-size:13px;margin:0}code{font-size:13px}
.notice{padding:10px 14px;border-radius:8px;background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent)}.notice.bad{border-left-color:var(--high)}.notice.good{border-left-color:var(--info)}.hint{font-size:12px;color:var(--mut)}.hint.ok{color:var(--info)}.hint.warn{color:var(--medium)}.notice.degraded{display:flex;gap:8px;align-items:flex-start;border-left-color:var(--medium)}.notice.degraded svg{color:var(--medium);flex:none;margin-top:2px}
/* charts */
figure.chart{margin:0;border-radius:12px;border:1px solid var(--line);background:var(--card);padding:18px 20px 14px;display:flex;flex-direction:column;gap:14px;min-width:0}
figure.chart figcaption{font-weight:600;font-size:14px}figure.chart figcaption.row{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px}
figcaption .left{display:flex;align-items:center;gap:12px}figcaption .right{display:flex;align-items:center;gap:16px;font-size:12px;font-weight:400;color:var(--mut)}
.legend{display:flex;gap:12px;font-size:12px;font-weight:400;color:var(--mut)}.legend span{display:inline-flex;align-items:center;gap:5px}
.sw{width:14px;height:8px;border-radius:2px;display:inline-block}.sw.fill{background:var(--accent)}.sw.line{border:1px solid var(--mut)}.sw.up{background:var(--up)}.sw.down{background:var(--down)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:16px}svg.chart text,figure.chart svg text{font:11px var(--mono);fill:var(--fg)}figure.chart svg text.lbl,svg.chart text.lbl{fill:var(--fg2)}figure.chart svg text.val{fill:var(--fg)}figure.chart svg tspan.mut,svg.chart tspan.mut{fill:var(--mut)}svg.trend,svg.strip{display:block}svg.inline{vertical-align:middle}
form.inline{display:inline;margin:0}.stripcap{font-size:12px;color:var(--mut)}
/* forms */
form.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px 18px}form.grid label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mut)}
form.grid label:has(input[type=checkbox]){flex-direction:row;align-items:center;gap:8px}form.grid input[type=checkbox]{height:auto;width:auto}
form.grid .row,form.grid div{grid-column:1/-1;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
fieldset.chan{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 14px;border:1px solid var(--line2);border-radius:8px;padding:8px 12px}
fieldset.chan legend{font-weight:600;padding:0 4px}label.inl{display:inline-flex;flex-direction:row;align-items:center;gap:4px;margin-right:10px}
select.sched{min-width:150px}input.cron{width:120px;font-family:var(--mono)}input.num{width:64px}td.actions{white-space:nowrap}tr.paused td{color:var(--mut)}
form.run{display:grid;gap:10px}form.run label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mut)}form.run label.ck{flex-direction:row;align-items:center}form.run input[type=checkbox]{height:auto}
dialog{border:1px solid var(--line2);border-radius:12px;background:var(--card);color:var(--fg);padding:20px;width:min(440px,92vw)}dialog::backdrop{background:rgba(0,0,0,.5)}dialog h2{margin-bottom:12px}
/* drawer */
.panel[hidden]{display:none}.panel{position:fixed;top:0;right:0;height:100vh;width:min(720px,92vw);background:var(--bg);border-left:1px solid var(--line2);box-shadow:-8px 0 24px rgba(0,0,0,.3);z-index:10;display:flex;flex-direction:column}
.panel-head{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--card)}.panel-head .grow{flex:1}
.panel-head button.x{background:none;border:0;color:var(--fg);font-size:22px;line-height:1;padding:0 4px;height:auto}.panel pre{flex:1;margin:0;border:0;border-radius:0;padding:12px 16px}
body.panel-open{overflow:hidden}
.toasts{position:fixed;right:20px;bottom:20px;z-index:50;display:flex;flex-direction:column;gap:8px;max-width:min(420px,calc(100vw - 40px));pointer-events:none}.toast{pointer-events:auto;display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:10px;background:var(--card);border:1px solid var(--line2);border-left:3px solid var(--info);box-shadow:0 8px 24px rgba(0,0,0,.25);font-size:13px;color:var(--fg);animation:toast-in .18s ease-out}.toast.bad{border-left-color:var(--high)}.toast .tmsg{flex:1;white-space:pre-wrap;overflow-wrap:anywhere}.toast button{height:auto;padding:0 2px;border:0;background:none;color:var(--mut);font-size:16px;line-height:1}.toast.out{opacity:0;transform:translateY(6px);transition:opacity .25s,transform .25s}@keyframes toast-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}form[aria-busy=true] button{opacity:.6;pointer-events:none}
.chat{flex:1;display:flex;flex-direction:column;min-height:0}.chat[hidden]{display:none}.chat-log{flex:1;overflow:auto;padding:12px 16px}
.msg{max-width:92%;margin:0 0 10px;padding:8px 12px;border-radius:10px;border:1px solid var(--line);background:var(--card);white-space:pre-wrap}
.msg.user{margin-left:auto;background:var(--accent);color:var(--on-accent);border-color:transparent}.msg.assistant{white-space:normal}.msg.assistant p{margin:0 0 6px}
.msg.assistant ul,.msg.assistant ol{margin:4px 0 6px 18px}.msg .foot{font-size:11px;margin-top:4px}
.chat-form{display:flex;gap:8px;padding:10px 16px;border-top:1px solid var(--line);background:var(--card)}.chat-form textarea{flex:1;resize:vertical}
a.chat-link{font-size:12px;margin-left:6px;white-space:nowrap;display:inline-flex;align-items:center;gap:4px}.chat-row{margin-top:8px}
/* snapshot detail */
main:has(.snapgrid){max-width:1600px}.snapgrid{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}.snapmain{flex:999 1 520px;min-width:0;display:flex;flex-direction:column;gap:20px}
.mono{font-family:var(--mono)}.dim{color:var(--fg2)}.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}.grow{flex:1 1 auto}
button.ai,.btn.ai{background:var(--ai);border-color:transparent;color:#fff;font-weight:600}button.ai:hover{background:var(--ai);color:#fff;filter:brightness(1.1)}
.rcard .rhead{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;padding:14px 18px;border-bottom:1px solid var(--line)}
.rhead .left{display:flex;align-items:center;gap:10px}.rhead h2{font-size:15px;color:var(--fg-strong)}.tags{display:flex;gap:8px;flex-wrap:wrap}
.tag{font-size:12px;padding:3px 8px;border-radius:999px;border:1px solid var(--line2);color:var(--fg2)}
.ricon{width:26px;height:26px;border-radius:7px;display:inline-flex;align-items:center;justify-content:center;flex:none}
.ricon.info{background:color-mix(in srgb,var(--info) 14%,transparent);color:var(--info)}.ricon.low{background:color-mix(in srgb,var(--low) 14%,transparent);color:var(--low)}
.ricon.medium{background:color-mix(in srgb,var(--medium) 14%,transparent);color:var(--medium)}.ricon.high{background:color-mix(in srgb,var(--high) 14%,transparent);color:var(--high)}
.banner{display:flex;align-items:flex-start;gap:10px;padding:10px 18px;border-bottom:1px solid var(--line);font-size:13px}
.banner.warn{background:color-mix(in srgb,var(--medium) 14%,transparent)}.banner.warn>svg{color:var(--medium);margin-top:2px;flex:none}.banner a{font-weight:500;white-space:nowrap}
.banner .grow{min-width:0}.banner details{display:inline;font-size:12px;color:var(--mut);margin-left:6px}.banner summary{cursor:pointer;display:inline}.banner details[open]{display:block;margin:6px 0 0}.banner details pre{margin-top:6px;font-size:12px}
.rbody{padding:16px 18px;display:flex;flex-direction:column;gap:12px}.rbody p{margin:0}
.flist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:12px}.flist>li{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:var(--bg)}
.fh{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.fh .n{font-family:var(--mono);font-size:12px;color:var(--mut)}.fh .where{font-size:12px;color:var(--fg2)}.fh .fs{font-weight:500;color:var(--fg-strong)}
.fh .chat-link{margin-left:auto}.flist dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 14px;margin:10px 0 0;font-size:13px}.flist dt{color:var(--mut);font-size:12px}.flist dd{margin:0;color:var(--fg2)}
.flist dd ul{margin:0;padding-left:16px}
.checked{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:var(--mut)}.checked .lbl{font-weight:500;color:var(--fg2)}
.mark{font-size:11px;padding:2px 7px;border-radius:999px;border:1px solid var(--line2);color:var(--fg2);white-space:nowrap}.mark.both{border-color:var(--info);color:var(--info)}.mark.model_only{border-color:var(--ai);color:var(--ai)}.mark.rules_only{border-color:var(--medium);color:var(--medium)}
.dropped{font-size:13px;color:var(--fg2);padding-top:10px;border-top:1px solid var(--line)}.dropped .lbl{font-weight:500;color:var(--fg2)}.dropped>.lbl{display:block;margin-bottom:6px}.dropped .agree{font-size:12px;color:var(--mut)}
.dropped ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}.dropped li{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.dropped details{font-size:12px;color:var(--mut);margin-top:6px}.dropped summary{cursor:pointer}
.life{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut)}.life .state{padding:2px 8px;border-radius:999px;border:1px solid var(--line2);color:var(--fg2);font-weight:500}
.life .state.new{border-color:var(--medium);color:var(--medium)}.life .state.worse{border-color:var(--high);color:var(--high)}.life .state.quiet,.life .state.gone,.life .state.recurring{border-color:var(--info);color:var(--info)}.life .state.returned{border-color:var(--medium);color:var(--medium)}
.life a.ticket{font-family:var(--mono);font-size:12px;color:var(--link)}.life code.ticket{font-family:var(--mono);font-size:12px;color:var(--fg2)}
.stillopen{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}.stillopen h3{margin:0 0 8px;font-size:13px;font-weight:600;color:var(--fg2)}
.stillopen ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}.stillopen li{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px}.stillopen .cleared{margin-top:8px;font-size:12px;color:var(--mut)}
.pill2{padding:2px 8px;border-radius:999px;background:var(--card2);border:1px solid var(--line);font-size:12px;color:var(--fg2)}
.filesrow{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--mut);padding-top:10px;border-top:1px solid var(--line)}
a.chat-link{font-size:12px;display:inline-flex;align-items:center;gap:5px;color:var(--ai);white-space:nowrap}
.bars{display:grid;gap:9px 12px;align-items:center;font-size:12px}.bars.waits{grid-template-columns:minmax(48px,max-content) minmax(0,1fr) 96px}.bars.delta{grid-template-columns:minmax(84px,max-content) minmax(0,1fr) minmax(110px,max-content)}
.bars .k{font-family:var(--mono);text-align:right;color:var(--fg2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bars .v{font-family:var(--mono);color:var(--mut);white-space:nowrap}.bars .v b{color:var(--fg);font-weight:500}
.bars .pair{display:flex;flex-direction:column;gap:3px}.bars .pair i,.bars .one i{display:block;border-radius:3px}.bars .pair .cur{height:8px;background:var(--info)}.bars .pair .ref{height:5px;border:1px solid var(--mut)}
.bars .one i{height:8px}.bars .one i.up{background:var(--up)}.bars .one i.down{background:var(--down)}
.sw.fill{background:var(--info)}.sw.down{background:var(--down)}
.share{display:flex;align-items:center;gap:8px;min-width:110px}.share .track{flex:1 1 auto;height:6px;border-radius:3px;background:var(--card2);border:1px solid var(--line)}
.share .track i{display:block;height:100%;border-radius:3px;background:var(--info)}.share .pct{font-family:var(--mono);font-size:12px;width:34px;text-align:right}
table.topsql code.sqltext{font-size:12px;color:var(--fg2);white-space:normal;overflow-wrap:anywhere}table.topsql td{vertical-align:middle}table.topsql td:last-child{white-space:nowrap;width:1%}table.topsql td:nth-child(2){width:150px}.snapmain .charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:20px}.snapmain .charts figure.chart{padding:16px 18px}
a.askbtn{width:28px;height:28px;border-radius:6px;border:1px solid var(--line2);color:var(--ai);display:inline-flex;align-items:center;justify-content:center;margin-left:6px}a.askbtn:hover{background:var(--ai-bg);text-decoration:none}
aside.discuss{flex:1 1 340px;max-width:400px;min-width:300px;border-radius:12px;border:1px solid var(--line);background:var(--card);display:flex;flex-direction:column;min-height:640px;max-height:calc(100vh - 96px);position:sticky;top:20px}
aside.discuss[hidden]{display:none}
.dhead{display:flex;align-items:center;justify-content:space-between;padding:12px 14px 12px 16px;border-bottom:1px solid var(--line)}.dhead .left{display:flex;align-items:center;gap:9px}
.aicon{width:24px;height:24px;border-radius:7px;background:var(--ai-bg);color:var(--ai);display:inline-flex;align-items:center;justify-content:center}
.dhead button.x{width:28px;height:28px;padding:0;justify-content:center;border:0;background:transparent;color:var(--mut)}.dhead button.x:hover{background:var(--card2);color:var(--fg)}
.dctx{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:6px;font-size:11px;color:var(--mut);align-items:center}.dctx .pill2{font-size:11px;padding:2px 7px}.dctx .focus{border-color:var(--ai);color:var(--ai)}
.dlog{flex:1 1 auto;padding:16px;display:flex;flex-direction:column;gap:14px;overflow-y:auto;min-height:0}
.dmsg.assistant{display:flex;gap:10px;font-size:13px}.dmsg .dtext{display:flex;flex-direction:column;gap:8px;min-width:0}.dmsg .dtext p{margin:0}.dmsg .dtext ul,.dmsg .dtext ol{margin:0;padding-left:18px}
.dmsg .dtext pre{font-size:12px}.dmsg .foot{font-size:11px;color:var(--mut)}
.avatar{width:24px;height:24px;flex:none;border-radius:50%;background:var(--ai-bg);color:var(--ai);display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:600}
.dmsg.user{align-self:flex-end;max-width:85%;padding:8px 12px;border-radius:10px;background:var(--card2);border:1px solid var(--line2);font-size:13px;white-space:pre-wrap}
.sugg{display:flex;flex-direction:column;gap:6px;padding-left:34px}.sugg .lbl{font-size:11px;color:var(--mut);font-weight:500}
.sugg button,.sugg a.suggest-link{height:auto;text-align:left;justify-content:flex-start;padding:8px 10px;border-radius:8px;border:1px solid var(--line2);background:var(--card2);color:var(--fg);font-size:13px;font-weight:400;white-space:normal}
.sugg a.suggest-link:hover{text-decoration:none;background:var(--active)}
.dfoot{padding:12px 14px 14px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:8px;margin:0}.dfoot textarea{resize:none;width:100%}
.dwarn{display:flex;align-items:flex-start;gap:8px;font-size:12px;color:var(--medium)}.dwarn svg{flex:none;margin-top:1px}
.drow{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11px}.drow button{height:32px}
@media(max-width:820px){aside.discuss{max-width:none;min-height:480px;position:static}}
/* review text */
.review p{margin:0 0 8px}.review ol{padding-left:22px;margin:8px 0}.review ol>li{margin-bottom:10px}.review ul{margin:4px 0 0;padding-left:18px;color:var(--fg2)}.review ul li{margin:2px 0}
"""


def md_to_html(md: str, item_suffix: Callable[[int], str] | None = None) -> str:
    """Just enough Markdown for triage.md: headings, bold, code, bullet and numbered lists, paragraphs.
    Bullets that follow a numbered item nest inside it, so a review's Impact/Evidence/Fix lines stay
    under their finding and the numbering runs 1, 2, 3. item_suffix(n) is appended to the n-th numbered
    item (1-based); the snapshot page uses it for the per-finding Discuss link."""
    out, in_ul, in_ol, li_open = [], False, False, False

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>"); in_ul = False

    def close_ol():
        nonlocal in_ol, li_open
        close_ul()
        if li_open:
            out.append("</li>"); li_open = False
        if in_ol:
            out.append("</ol>"); in_ol = False

    n_item = 0
    for raw in md.splitlines():
        line = html.escape(raw)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        line = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", line)
        if m := re.match(r"^(#{1,3}) (.*)", line):
            close_ol(); out.append(f"<h{len(m[1]) + 1}>{m[2]}</h{len(m[1]) + 1}>")
        elif m := re.match(r"^\s*- (.*)", line):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{m[1]}</li>")
        elif m := re.match(r"^\d+\. (.*)", line):
            close_ul()
            if li_open:
                out.append("</li>")
            if not in_ol:
                out.append("<ol>"); in_ol = True
            n_item += 1
            out.append(f"<li>{m[1]}{item_suffix(n_item) if item_suffix else ''}"); li_open = True
        elif line.strip():
            close_ol(); out.append(f"<p>{line}</p>")
        else:
            close_ul()  # a blank line ends a bullet run but not the numbered list around it
    close_ol()
    return "\n".join(out)


ICON = {  # inline stroke icons, 14px, currentColor
    "plus": "<svg width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' aria-hidden='true'><path d='M12 5v14M5 12h14'/></svg>",
    "db": "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' aria-hidden='true'><ellipse cx='12' cy='6' rx='7' ry='3'/><path d='M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3'/></svg>",
    "chat": "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z'/></svg>",
    "check": "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='m5 12 5 5L20 7'/></svg>",
    "alert": "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'/></svg>",
    "x": "<svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2' stroke-linecap='round' aria-hidden='true'><path d='M6 6l12 12M18 6 6 18'/></svg>",
    "arrow": "<svg width='13' height='13' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.4' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M5 12h14M13 6l6 6-6 6'/></svg>",
    "clock": "<svg width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' aria-hidden='true'><circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/></svg>",
}
STATUS = {"info": "healthy", "low": "degraded", "medium": "degraded", "high": "incident"}


def sev(s: str | None) -> str:
    s = s if s in ("high", "medium", "low", "info") else "info"
    return f"<span class='sev {s}'><i class='dot'></i>{s}</span>"


def sort_value(v) -> str:
    """A table cell's data-v: the raw number a sortable column orders by, where the cell shows 54% or 31.3k.
    Missing or non-numeric values are empty, and sort last."""
    if isinstance(v, bool) or v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return repr(f) if math.isfinite(f) else ""


def normalize_finding(f: dict) -> dict:
    """Today's finding shape from whatever was stored: older reviews used title/detail/kind and an evidence dict."""
    f = dict(f or {})
    ev = f.get("evidence")
    if isinstance(ev, dict):
        ev = [f"{k}: {v if isinstance(v, (str, int, float)) else json.dumps(v, default=str)[:160]}" for k, v in ev.items()]
    elif isinstance(ev, str):
        ev = [ev]
    ev = [str(e) for e in (ev or [])]
    if f.get("detail") and f["detail"] not in ev:
        ev.insert(0, str(f["detail"]))
    f["evidence"] = ev
    f["summary"] = f.get("summary") or f.get("title") or ""
    f["where"] = f.get("where") or (f.get("kind") or "").replace("_", " ")
    return f


MARK_LABEL = {"both": "both", "model_only": "model only", "rules_only": "rules only"}


def agreement_marks(tr: dict) -> dict[int, str]:
    """The mark pill for each model finding, by its index in `findings`: both when a rule found the same
    thing, model only when the model added it. Empty for a rules-only review or one stored without marks."""
    rows = tr.get("agreement")
    if not isinstance(rows, list):
        return {}
    return {r["model"]: f"<span class='mark {html.escape(r['mark'])}' title='{'a rule found this too' if r['mark'] == 'both' else 'only the model found this'}'>"
                        f"{MARK_LABEL.get(r['mark'], r['mark'])}</span>"
            for r in rows if r.get("model") is not None}


def dropped_html(tr: dict) -> str:
    """The rules findings the model left out, under the review: medium and above listed with title and severity,
    so a fluent write-up cannot hide a blocking or deadlocks hit; the low ones collapsed to a count. A rules-only
    review says so instead. Nothing for a triage.json stored before the marks existed."""
    if tr.get("source") != "agent":
        return "<div class='dropped'><span class='agree'>Rules-only review; there is no model review to compare with.</span></div>" if tr.get("source") else ""
    rows = tr.get("agreement")
    if not isinstance(rows, list) or not rows:  # nothing found by either side: no counts to show
        return ""
    heur = tr.get("heuristics") or []

    def item(r: dict) -> str:
        h = heur[r["rules"]] if r.get("rules") is not None and r["rules"] < len(heur) else {}
        title = h.get("title") or (r.get("kind", "") + (f" {r['subject']}" if r.get("subject") else ""))
        return f"<li>{sev(r.get('severity'))}<code class='where'>{html.escape(r.get('kind', ''))}</code><span>{html.escape(title)}</span></li>"

    n = {m: sum(1 for r in rows if r.get("mark") == m) for m in ("both", "model_only", "rules_only")}
    counts = f"<span class='agree'>Agreement: {n['both']} both · {n['model_only']} model only · {n['rules_only']} rules only</span>"
    shown = dropped(rows)
    low = [r for r in rows if r.get("mark") == "rules_only" and SEVERITY_RANK.get(r.get("severity"), 0) < SEVERITY_RANK["medium"]]
    if not shown and not low:
        return f"<div class='dropped'>{counts}</div>"
    out = f"<div class='dropped'><span class='lbl'>Rules found, not in the review</span><ul>{''.join(item(r) for r in shown)}</ul>"
    if low:
        out += (f"<details><summary>{len(low)} low-severity rules finding{'s' if len(low) != 1 else ''} not in the review</summary>"
                f"<ul>{''.join(item(r) for r in low)}</ul></details>")
    return out + f"<div>{counts}</div></div>"


def agent_reason(err: str) -> str:
    """One plain sentence for why the model review did not run; the raw error stays behind 'details'."""
    from dbtriage.egress import plain_provider_error

    return plain_provider_error(err)


def status_of(s: str | None) -> str:
    """The target-level state line next to the title: derived from the latest review's severity."""
    s = s if s in STATUS else "info"
    return f"<span class='status {s}'><i class='dot'></i>{STATUS[s]}</span>"


def finding_cell(title: str | None, where: str | None = None) -> str:
    """A finding in a table: percentages in bold, a SQL id moved into a chip under the text."""
    if not title:
        return "<span class='mut'>not triaged</span>"
    text = html.escape(title)
    chip = ""
    sql_id = None
    if where and (m := re.match(r"^(?:sql_id|sql|queryid|digest)\s+['\"`]?([^'\"`\s]+)['\"`]?$", where)):
        sql_id = m[1]
    elif m := re.search(r"\b([0-9a-f]{16,}|[0-9a-z]{13})\b", title):
        sql_id = m[1]
    if sql_id:
        short = sql_id if len(sql_id) <= 18 else f"{sql_id[:8]}…{sql_id[-7:]}"
        chip = f"<code class='idchip' title='{html.escape(sql_id)}'>{html.escape(short)}</code>"
        text = re.sub(r"\s*[('\"`]*" + re.escape(html.escape(sql_id)) + r"[)'\"`]*", "", text).strip() or text
    text = re.sub(r"(\d+(?:\.\d+)?%)", r"<b>\1</b>", text)
    return f"<div class='finding'><span>{text}</span>{chip}</div>"


def _show(v) -> str:
    return "unset" if v is None else str(v)


def _fmt_ts(iso: str | None) -> str:
    return (iso or "")[:19].replace("T", " ")


def _change_link(c: dict) -> str:
    """A feed item's description, linked to its deployment or commit when the feed gave one (http(s) only)."""
    text = html.escape(str(c.get("description") or ""))
    link = str(c.get("link") or "")
    if link.startswith(("https://", "http://")):
        return f"<a href='{html.escape(link)}' rel='noopener' target='_blank'>{text}</a>"
    return text


LIFE_LABEL = {"new": "new", "still_open": "still open", "worse": "worse", "quiet": "quiet", "returned": "returned",
              "recurring": "recurring", "gone": "gone"}


def life_badge(life: dict | None) -> str:
    """A finding's life (dbtriage.ledger) as a state pill, "open since" for one with a history, and its ticket(s)
    as links when the channel gave a URL. Empty for a review stored before the ledger existed."""
    if not life or life.get("state") not in LIFE_LABEL:
        return ""
    state = life["state"]
    bits = [f"<span class='state {state}'>{LIFE_LABEL[state]}</span>"]
    if state != "new" and (since := (life.get("first_seen_at") or life.get("first_seen") or "")[:10]):
        bits.append(f"<span>open since {html.escape(since)}</span>")
    if (misses := int(life.get("misses") or 0)) and state != "gone":
        bits.append(f"<span>not seen in the last {misses} run{'s' if misses != 1 else ''}</span>")
    if period := describe_period(life.get("period_s")):
        bits.append(f"<span>{period}</span>")
    for name, t in sorted((life.get("tickets") or {}).items()):
        if not isinstance(t, dict) or not t.get("key"):
            continue
        key_ = html.escape(str(t["key"]))
        if t.get("url"):
            bits.append(f"<a class='ticket' href='{html.escape(t['url'])}' title='{html.escape(name)} ticket'>{key_}</a>")
        else:
            bits.append(f"<code class='ticket' title='{html.escape(name)} ticket'>{key_}</code>")
    return "<span class='life'>" + "".join(bits) + "</span>"


def still_open_html(tr: dict) -> str:
    """The open findings that did not make the review's five, and the ones that cleared this run."""
    still, gone = tr.get("still_open") or [], tr.get("gone") or []
    if not still and not gone:
        return ""
    def ident(e: dict) -> str:
        return f"<code class='where'>{html.escape(f'{e.get('kind') or ''} {e.get('subject') or ''}'.strip())}</code>"

    items = "".join(f"<li>{sev(e.get('severity'))}{ident(e)}<span>{html.escape(str(e.get('title') or ''))}</span>{life_badge(e)}</li>" for e in still)
    out = f"<section class='stillopen' aria-labelledby='still-h'><h3 id='still-h'>Still open ({len(still)} not among the findings above)</h3>"
    out += f"<ul>{items}</ul>" if items else ""
    if gone:
        out += "<p class='cleared'>Cleared: " + "; ".join(f"{ident(e)} {life_badge(dict(e, state='gone'))}" for e in gone) + "</p>"
    return out + "</section>"


class App:
    """Everything the handler needs: config, storage, rendering, API payloads."""

    def __init__(self, cfg: Config, store: Storage, allow_run: bool, token: str | None, allow_edit: bool = False, allow_chat: bool = False):
        self.seed, self.store, self.allow_run, self.token, self.allow_edit = cfg.file_only or cfg, store, allow_run, token, allow_edit
        self.allow_chat = allow_chat
        self.scheduler: scheduler.Scheduler | None = None
        self.base = Path(cfg.base_dir)
        self.settings = Settings.for_config(cfg)
        self.running: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()
        self.reload()

    def allows(self, cap: str) -> bool:
        """Process flag is the ceiling. A trusted-proxy group list, when set, narrows it.
        With trust off, this is the flag. View is on unless a view group is configured."""
        flag = {"run": self.allow_run, "edit": self.allow_edit, "chat": self.allow_chat}.get(cap, True)
        return access.permits(access.current(), cap, flag)

    def _gate_notice(self, msg: str | None, kind: str, flag: bool, allowed: bool, off: str, narrowed: str) -> str:
        if msg:
            return self._notice(msg, kind or ("bad" if not allowed else ""))
        if allowed:
            return ""
        return self._notice(off if not flag else narrowed, "bad")

    def reload(self):
        """Recompute the effective config after the settings overlay changed."""
        cfg = self.settings.apply(self.seed)
        self.cfg = cfg
        self.web = {**DEFAULTS, **{k: v for k, v in cfg.web.items() if v not in (None, "")}}  # cleared = the default
        self.web["severity_colors"] = {**DEFAULTS["severity_colors"], **cfg.web.get("severity_colors", {})}
        self.logo_path = self._local(self.web.get("logo"))
        custom = self._local(self.web.get("custom_css"))
        self.custom_css = custom.read_text() if custom and custom.exists() else ""

    # ---------------------------------------------------------------- templates and branding
    def _local(self, value: str | None) -> Path | None:
        if not value or re.match(r"^https?://", value):
            return None
        p = Path(value)
        return p if p.is_absolute() else self.base / p

    def template(self, name: str) -> string.Template:
        override = self._local(self.web.get("templates_dir"))
        if override and (override / name).exists():
            return string.Template((override / name).read_text())
        return string.Template(resources.files("dbtriage.templates").joinpath(name).read_text())

    def sidenav(self, active: str | None, sid: str | None) -> str:
        """Left-hand navigation: every target with its latest severity, and under the active one its newest snapshots."""
        add = "<a class='add' href='/targets/new' aria-label='add target' title='add target'>" + ICON["plus"] + "</a>" if self.allows("edit") else ""
        out = [f"<div class='navhead'><h3>Targets</h3>{add}</div><div class='items'>"]
        for r in self.target_rows():
            n, lt = r["name"], r["latest"] or {}
            cls = " active" if n == active and not sid else ""
            out.append(f"<a class='t{cls}' href='/t/{html.escape(n)}'><i class='dot {html.escape(lt.get('severity') or 'info')}'></i><span>{html.escape(n)}</span><span class='cnt'>{r['snapshots']}</span></a>")
            if n == active:
                rows, _ = self.snapshot_rows(n, 8, 0)
                out.append("<div class='sub'>")
                for sn in rows:
                    cls = " class='active'" if sn["id"] == sid else ""
                    out.append(f"<a href='/s/{html.escape(sn['id'])}'{cls}><i class='dot {html.escape(sn['severity'] or 'info')}'></i>{_fmt_ts(sn['captured_at'])[5:16]}</a>")
                out.append("</div>")
        out.append("</div>")
        return "\n".join(out)

    def status(self) -> dict:
        """Storage and settings health for /health and the banner. Re-reads the settings database after an outage."""
        import time

        if self.settings.degraded and time.monotonic() - getattr(self, "_settings_retry", 0) > 30:
            self._settings_retry = time.monotonic()
            self.settings.load()
            if not self.settings.degraded:
                self.reload()
        if hasattr(self.store, "probe") and (getattr(self.store, "degraded", None) or self.store.spooled()):
            self.store.probe()  # rate-limited by the store's retry window
        spool = len(self.store.spooled()) if hasattr(self.store, "spooled") else 0
        return {"store": describe_store(self.store), "store_ok": not getattr(self.store, "degraded", None), "store_error": getattr(self.store, "degraded", None),
                "spooled": spool, "settings": self.settings.where, "settings_ok": not self.settings.degraded, "settings_error": self.settings.degraded}

    def degraded_banner(self) -> str:
        st = self.status()
        out = []
        waiting = f" ({st['spooled']} waiting)" if st["spooled"] else ""
        if not st["store_ok"] or st["spooled"]:
            out.append(f"Snapshot store {html.escape(st['store'])} is unreachable; pages show what is available, and new captures and chat answers are kept locally"
                       f"{waiting} and sync when it is back."
                       if not st["store_ok"] else f"{st['spooled']} object(s) captured during a store outage are waiting to sync.")
        if not st["settings_ok"]:
            out.append(html.escape(st["settings_error"]) + ". Changes cannot be saved until it is back.")
        return "".join(f"<div class='notice bad degraded' role='status'>{ICON['alert']} {m}</div>" for m in out)

    @staticmethod
    def _default_icon(accent: str) -> str:
        """The header's database badge as an inline SVG favicon, so browsers do not ask for a missing /favicon.ico."""
        from urllib.parse import quote

        color = accent if re.match(r"^#[0-9a-fA-F]{3,8}$", accent or "") else "#3dd68c"
        svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='{color}'/>"
               "<g fill='none' stroke='#0f1115' stroke-width='2.6' stroke-linecap='round'><ellipse cx='16' cy='10' rx='8' ry='3.4'/>"
               "<path d='M8 10v12c0 1.9 3.6 3.4 8 3.4s8-1.5 8-3.4V10M8 16c0 1.9 3.6 3.4 8 3.4s8-1.5 8-3.4'/></g></svg>")
        return f"<link rel='icon' href='data:image/svg+xml,{quote(svg, safe="")}'>"

    def page(self, name: "str | string.Template", title: str, refresh: bool = False, active: str | None = None, sid: str | None = None, **fields) -> bytes:
        w, colors = self.web, self.web["severity_colors"]
        logo = w.get("logo")
        logo_src = logo if logo and re.match(r"^https?://", logo) else ("/brand/logo" if self.logo_path else None)
        body = (name if isinstance(name, string.Template) else self.template(name)).safe_substitute(**fields)
        out = self.template("base.html").safe_substitute(
            title=html.escape(f"{title} · {w['title']}"), brand=html.escape(w["brand"]), theme=html.escape(w["theme"]), q=html.escape(fields.get("q", "")),
            logo_html=f"<img src='{html.escape(logo_src)}' alt=''>" if logo_src else f"<span class='badge'>{ICON['db']}</span>",
            icon_link=f"<link rel='icon' href='{html.escape(logo_src)}'>" if logo_src else self._default_icon(w["accent"]),
            refresh_meta=f"<meta http-equiv='refresh' content='{int(w['refresh_seconds'])}'>" if refresh and int(w["refresh_seconds"]) > 0 else "",
            css=string.Template(CSS).safe_substitute(accent=w["accent"], c_high=colors["high"], c_medium=colors["medium"],
                                                     c_low=colors["low"], c_info=colors["info"]),
            custom_css=self.custom_css, sidenav=self.sidenav(active, sid), body=self.degraded_banner() + body, footer=html.escape(w.get("footer") or ""),
        )
        return out.encode()

    # ---------------------------------------------------------------- data
    def triage_of(self, sid: str) -> dict | None:
        return self.store.get_json(f"{sid}/triage.json") if self.store.exists(f"{sid}/triage.json") else None

    def manifest_of(self, sid: str) -> dict:
        return self.store.get_json(f"{sid}/manifest.json")

    def target_rows(self) -> list[dict]:
        rows = []
        for name, t in sorted(self.cfg.targets.items()):
            ids = self.store.list_snapshots(t.engine, name)
            latest = ids[-1] if ids else None
            tr = self.triage_of(latest) if latest else None
            rows.append({"name": name, "engine": t.engine, "snapshots": len(ids), "links": t.extra.get("links") or [],
                         "latest": {"id": latest, "captured_at": self.manifest_of(latest)["captured_at"] if latest else None,
                                    "severity": (tr or {}).get("severity"), "title": (tr or {}).get("title")} if latest else None})
        return rows

    def snapshot_rows(self, name: str, limit: int, offset: int) -> tuple[list[dict], int]:
        t = self.cfg.targets[name]
        ids = list(reversed(self.store.list_snapshots(t.engine, name)))
        rows = []
        for sid in ids[offset:offset + limit]:
            m, tr = self.manifest_of(sid), self.triage_of(sid)
            trig = m.get("trigger") or {}
            rows.append({"id": sid, "captured_at": m["captured_at"], "mode": m["window"]["mode"], "load": self.load_of(sid),
                         "source": trig.get("source", ""), "reason": trig.get("reason", ""), "actor": trig.get("actor", ""),
                         "severity": (tr or {}).get("severity"), "title": (tr or {}).get("title"), "triaged_by": (tr or {}).get("source"),
                         "rules_dropped": rules_dropped(tr)})
        return rows, len(ids)

    LOAD_METRICS = ("avg_active_sessions", "db_time_ms", "questions", "tup_returned", "xact_commit", "batch_requests")

    def load_of(self, sid: str) -> dict | None:
        """One headline load number per snapshot for the activity strip, engine-agnostic."""
        if not self.store.exists(f"{sid}/compare.json"):
            return None
        lp = self.store.get_json(f"{sid}/compare.json").get("load_profile", {})
        for k in self.LOAD_METRICS:
            if k in lp:
                v = lp[k]
                return {"metric": k + ("/s" if "current_per_s" in v else ""), "value": v.get("current_per_s", v.get("current"))}
        return None

    def _compare(self, sid: str) -> dict | None:
        return self.store.get_json(f"{sid}/compare.json") if self.store.exists(f"{sid}/compare.json") else None

    def metrics_of(self, name: str) -> list[str]:
        """Load-profile metric names available for the trend select, from the newest compared snapshot."""
        t = self.cfg.targets[name]
        for sid in reversed(self.store.list_snapshots(t.engine, name)):
            c = self._compare(sid)
            if c:
                return sorted(k for k in c.get("load_profile", {}) if k != "elapsed_s")
        return []

    def trend_points(self, name: str, metric: str | None, limit: int = 200) -> tuple[list[dict], str]:
        """Per-snapshot value of one load metric (rate when the engine reports one), oldest first."""
        from datetime import datetime

        t = self.cfg.targets[name]
        ids = self.store.list_snapshots(t.engine, name)[-limit:]
        pts, used = [], metric or ""
        for sid in ids:
            c = self._compare(sid)
            if not c:
                continue
            lp = c.get("load_profile", {})
            if not used:
                used = next((k for k in self.LOAD_METRICS if k in lp), "")
            v = lp.get(used)
            if v is None:
                continue
            tr = self.triage_of(sid) or {}
            pts.append({"id": sid, "t": datetime.fromisoformat(self.manifest_of(sid)["captured_at"]), "value": v.get("current_per_s", v.get("current")),
                        "severity": tr.get("severity"), "title": tr.get("title"), "mode": c.get("current", {}).get("mode")})
        return pts, used + ("/s" if pts and "current_per_s" in (self._compare(pts[-1]["id"]) or {}).get("load_profile", {}).get(used.rstrip("/s"), {}) else "")

    def sql_history(self, name: str, sql_id: str, limit: int = 100) -> list[dict]:
        """One row per snapshot where sql_id appears in the window's top SQL (interval values), oldest first."""
        t = self.cfg.targets[name]
        rows = []
        for sid in self.store.list_snapshots(t.engine, name)[-limit:]:
            c = self._compare(sid)
            if not c:
                # no comparison (a target's first snapshot): an interval snapshot (Oracle AWR) still describes its own
                # window, so its top SQL is comparable; cumulative counters (Postgres, MySQL) are not, so skip those
                try:
                    s = self.store.get_json(f"{sid}/summary.json")
                except FileNotFoundError:
                    continue
                if s.get("metrics_kind") != "interval":
                    continue
                top = s.get("top_sql") or []
                total = sum(q.get("total_time_ms") or 0 for q in top) or 1
                c = {"sql": {"top": [dict(q, share_of_time=q.get("share_of_time", (q.get("total_time_ms") or 0) / total)) for q in top]},
                     "current": {"mode": s.get("mode")}}
            for q in c.get("sql", {}).get("top", []):
                if str(q.get("sql_id")) == sql_id:
                    tr = self.triage_of(sid) or {}
                    rows.append({"id": sid, "captured_at": self.manifest_of(sid)["captured_at"], "severity": tr.get("severity"),
                                 "mode": c.get("current", {}).get("mode"), **{k: q.get(k) for k in ("calls", "total_time_ms", "mean_time_ms", "cpu_time_ms", "rows", "io_blocks_read", "share_of_time", "plan_hash", "text")}})
        prev = None
        for r in rows:
            r["plan_changed"] = prev is not None and r.get("plan_hash") not in (None, prev)
            prev = r.get("plan_hash") or prev
        return rows

    def snapshot_detail(self, sid: str) -> dict:
        m = self.manifest_of(sid)
        detail = {"manifest": m, "triage": self.triage_of(sid), "findings": [], "top_sql": [],
                  "files": [f for f in self.web["files"] if self.store.exists(f"{sid}/{f}")] + [r for r in m.get("reports", []) if r not in self.web["files"] and "*" in self.web["files"]]}
        if self.store.exists(f"{sid}/compare.json"):
            c = self.store.get_json(f"{sid}/compare.json")
            detail["findings"], detail["top_sql"] = c.get("findings", []), ((c.get("sql") or {}).get("top") or [])[:10]
            detail["waits"], detail["load_profile"] = c.get("waits", {}), c.get("load_profile", {})
        if not detail["top_sql"] and self.store.exists(f"{sid}/summary.json"):
            # nothing compared yet (a target's first snapshot): show the snapshot's own top SQL, ranked by time
            s = self.store.get_json(f"{sid}/summary.json")
            top = sorted(s.get("top_sql") or [], key=lambda q: -(q.get("total_time_ms") or 0))[:10]
            total = sum(q.get("total_time_ms") or 0 for q in s.get("top_sql") or []) or 1
            detail["top_sql"] = [dict(q, share_of_time=q.get("share_of_time", (q.get("total_time_ms") or 0) / total)) for q in top]
            detail["top_sql_basis"] = "window" if s.get("metrics_kind") == "interval" else "since statistics were last reset"
        return detail

    def start_run(self, target: str, mode: str, reason: str, use_llm: bool, baseline_policy: str | None = None,
                  baseline: str | None = None, window_minutes: int | None = None, source: str = "web") -> tuple[int, dict]:
        from dbtriage.cli import run_request
        from dbtriage.triggers import MODES, TriageRequest

        if not self.allow_run:
            return 403, {"error": "runs are disabled (start with --allow-run)"}
        # The scheduler is the process, not a person, so a click-group does not stop it.
        if source != "schedule" and not self.allows("run"):
            return 403, {"error": "your group cannot start runs"}
        if target not in self.cfg.targets:
            return 400, {"error": f"unknown target {target!r}"}
        if mode not in MODES:
            return 400, {"error": f"mode must be one of {MODES}"}
        if baseline_policy and baseline_policy not in POLICIES:
            return 400, {"error": f"baseline_policy must be one of {POLICIES}"}
        with self.lock:
            th = self.running.get(target)
            if th and th.is_alive():
                return 409, {"error": f"a run for {target!r} is already in progress"}
            who = "" if source == "schedule" else access.current().name
            if who:
                access.audit("run", f"{target} {mode}")
            req = TriageRequest(target=target, mode=mode, source=source, reason=reason[:200], window_minutes=int(window_minutes or 60),
                                baseline_policy=baseline_policy or None, baseline=(baseline or "").strip() or None, actor=who)
            th = threading.Thread(target=self._run, args=(req, use_llm), daemon=True, name=f"run-{target}")
            self.running[target] = th
            th.start()
        return 202, {"accepted": True, "target": target, "mode": mode}

    def _run(self, req, use_llm):
        from dbtriage.cli import run_request

        try:
            run_request(req, self.cfg, use_llm)
        except (Exception, SystemExit):  # noqa: BLE001 - keep the server alive, log the failure
            print(f"web: run for {req.target} failed:\n{traceback.format_exc()}", file=sys.stderr)

    # ---------------------------------------------------------------- pages
    def run_dialog(self, target: str | None) -> str:
        """The Capture now dialog: the run form, for one target or with a target picker."""
        if not self.allows("run"):
            return ""
        tsel = (f"<input type=hidden name=target value='{html.escape(target)}'>" if target else
                "<label>Target<select name=target>" + "".join(f"<option>{html.escape(n)}</option>" for n in sorted(self.cfg.targets)) + "</select></label>")
        return (f"<dialog id='run'><form class='run' method='post' action='/run'><h2>Capture now{(' · ' + html.escape(target)) if target else ''}</h2>{tsel}"
                "<label>Mode<select name=mode><option>adhoc</option><option>incident</option><option>scheduled</option></select></label>"
                "<label>Baseline<select name=baseline_policy><option value=''>target default</option>"
                + "".join(f"<option value='{p}'>{LABELS[p]}</option>" for p in POLICIES) + "</select></label>"
                "<label>Reason<input name=reason placeholder='why now'></label>"
                "<label class='ck'><input type=checkbox name=no_llm> rules only, no model</label>"
                "<div class='actions'><button type=button class='quiet' onclick='this.closest(\"dialog\").close()'>Cancel</button><button class='primary'>Start run</button></div>"
                "</form></dialog>")

    def capture_button(self) -> str:
        return f"<button type=button class='primary' data-dialog='run'>{ICON['plus']}Capture now</button>" if self.allows("run") else ""

    def index(self) -> bytes:
        rows = []
        for r in self.target_rows():
            lt = r["latest"] or {}
            links = " · ".join(f"<a href='{html.escape(l.get('url', ''))}'>{html.escape(l.get('label', ''))}</a>" for l in r["links"] or [])
            tr = self.triage_of(lt["id"]) if lt.get("id") else None
            where = (tr or {}).get("findings", [{}])[0].get("where") if (tr or {}).get("findings") else None
            rows.append(f"<tr><td><a href='/t/{html.escape(r['name'])}'>{html.escape(r['name'])}</a></td><td class=dim>{r['engine']}</td>"
                        f"<td class='r mono'>{r['snapshots']}</td><td class=ts><a class=ts href='/s/{html.escape(lt.get('id') or '')}'>{_fmt_ts(lt.get('captured_at')) or '-'}</a></td><td>{sev(lt.get('severity'))}</td>"
                        f"<td>{finding_cell(lt.get('title'), where) if lt else ''}</td><td>{links}</td></tr>")
        counts: dict[str, int] = {}
        for r in self.target_rows():
            s_ = (r["latest"] or {}).get("severity") or "info"
            counts[s_] = counts.get(s_, 0) + 1
        return self.page("index.html", "overview", refresh=True, rows="\n".join(rows), tiles=charts.stat_tiles(counts), count=len(self.cfg.targets),
                         actions=self.capture_button(), run_dialog=self.run_dialog(None))

    def target(self, name: str, offset: int, metric: str | None = None, flt: str = "all") -> bytes:
        t = self.cfg.targets.get(name)
        if not t:
            return self.error_page(404, f"There is no target called {name!r}.")
        size = int(self.web["page_size"])
        rows, total = self.snapshot_rows(name, size, offset)
        if flt == "incidents":
            rows = [r for r in rows if r["mode"] == "incident" or r["severity"] == "high"]
        elif flt == "scheduled":
            rows = [r for r in rows if r["mode"] == "scheduled"]
        trs = []
        for r in rows:
            tr = self.triage_of(r["id"]) or {}
            where = tr["findings"][0].get("where") if tr.get("findings") else None
            who = f" · {html.escape(r['actor'])}" if r.get("actor") else ""
            trs.append(f"<tr><td><a class=ts href='/s/{html.escape(r['id'])}'>{_fmt_ts(r['captured_at'])}</a></td><td class=dim>{r['mode']}</td>"
                       f"<td class=dim>{html.escape(r['source'])}{who}{(' · ' + html.escape(r['reason'])) if r['reason'] else ''}</td><td>{sev(r['severity'])}</td>"
                       f"<td>{finding_cell(r['title'], where)}</td><td class='r mut'>{html.escape(r['triaged_by'] or '')}</td></tr>")
        links = [f"<a href='{html.escape(l.get('url', ''))}'>{html.escape(l.get('label', ''))}</a>" for l in t.extra.get("links") or []]
        sched = next((r for r in self.schedule_rows() if r["target"] == name), None)
        if sched and sched["schedule"]:
            when = "paused" if sched["paused"] else (f"next {_fmt_ts(sched['next'].isoformat())[5:16]}" if sched["next"] else "")
            links.insert(0, f"<a href='/settings' title='change on the settings page'>{ICON['clock']} {html.escape(sched['label'])}</a>" + (f"<span>{when}</span>" if when else ""))
        pager = ""
        if total > size:
            prev = f"<a href='/t/{name}?offset={max(0, offset - size)}'>newer</a>" if offset > 0 else ""
            nxt = f"<a href='/t/{name}?offset={offset + size}'>older</a>" if offset + size < total else ""
            pager = f"<div class='pager'>{prev} <span>{offset + 1}-{min(offset + size, total)} of {total}</span> {nxt}</div>"
        all_rows, _ = self.snapshot_rows(name, size, 0)
        latest = all_rows[0] if all_rows else None
        incidents = sum(1 for r in all_rows if r["severity"] == "high")
        pts, used = self.trend_points(name, metric)
        tiles = [("Snapshots", str(total), ""), ("Open incidents", str(incidents), "high" if incidents else "")]
        if pts:
            last_v, prev_v = pts[-1]["value"], (pts[-2]["value"] if len(pts) > 1 else None)
            delta = ""
            if prev_v is not None and last_v is not None and prev_v != last_v:
                delta = f"<span class='delta'>{'↑' if last_v > prev_v else '↓'} {charts.fmt(abs(last_v - prev_v))}</span>"
            tiles.append((used, charts.fmt(last_v) + delta, ""))
        tiles.append(("Last capture", _fmt_ts(latest["captured_at"])[11:16] if latest else "–", ""))
        tiles_html = "".join(f"<div class='tile'><span class='lbl'>{html.escape(l)}</span><span class='num {c}'>{v}</span></div>" for l, v, c in tiles)
        strip = ""
        if pts:
            opts = "".join(f"<option value='{html.escape(m)}'{' selected' if m == used.removesuffix('/s') else ''}>{html.escape(m)}</option>" for m in self.metrics_of(name))
            legend = "".join(f"<span><i class='dot {s_}'></i>{s_}</span>" for s_ in ("info", "low", "medium", "high") if any(p.get("severity") == s_ for p in pts))
            strip = (f"<figure class='chart'><figcaption class='row'><span class='left'><span>Trend</span><form method=get action='/t/{html.escape(name)}' class='inline'>"
                     f"<label class='pill'><select name=metric aria-label='metric' onchange='this.form.submit()'>{opts}</select></label><noscript><button class=sm>go</button></noscript></form></span>"
                     f"<span class='right'><span>{len(pts)} snapshots</span><span class='legend'>{legend}</span></span></figcaption>"
                     + charts.trend_chart(pts, used, height=220) + "</figure>")
        chips = "".join(f"<a href='/t/{html.escape(name)}?filter={k}'{' class=on' if flt == k else ''}>{l}</a>"
                        for k, l in (("all", "All"), ("incidents", "Incidents"), ("scheduled", "Scheduled")))
        actions = f"<a class='btn' href='/t/{html.escape(name)}/compare'>Compare</a>" + self.capture_button()
        return self.page("target.html", name, refresh=True, active=name, target=html.escape(name), engine=t.engine, links="".join(links), pager=pager,
                         rows="\n".join(trs), strip=strip, status=status_of(latest["severity"] if latest else None), tiles=tiles_html, chips=chips,
                         actions=actions, run_dialog=self.run_dialog(name))

    def snapshot(self, sid: str) -> bytes:
        d = self.snapshot_detail(sid)
        m = d["manifest"]
        tr = d["triage"] or {}
        target = m["target"]
        T = html.escape(target)
        top_rows = d["top_sql"]
        ids = [str(q.get("sql_id") or "") for q in top_rows if len(str(q.get("sql_id") or "")) >= 6]

        def short(q: str) -> str:
            return q if len(q) <= 16 else f"{q[:8]}…{q[-4:]}"

        def link_ids(text: str) -> str:
            """Escape, then turn every top-SQL id into a short link to its history."""
            out = html.escape(text or "")
            for q in sorted(ids, key=len, reverse=True):
                qe = html.escape(q)
                out = out.replace(qe, f"<a class='mono' href='/t/{T}/sql/{qe}' title='{qe}'>{html.escape(short(q))}</a>")
            return out

        captured = _fmt_ts(m["captured_at"])
        vs, b_time = "", None
        if b := (m.get("baseline") or {}).get("snapshot"):
            try:
                b_at = _fmt_ts(self.manifest_of(b)["captured_at"])
            except FileNotFoundError:
                b_at = b.rsplit("/", 1)[-1]
            b_time = b_at[11:19] if b_at[:10] == captured[:10] else b_at[5:16]
            pol = m["baseline"].get("policy", "")
            vs = (f" · vs <a class='mono' href='/s/{html.escape(b)}' title='{html.escape(m['baseline'].get('reason', ''))}'>{html.escape(b_time)}</a>"
                  f" ({html.escape(LABELS.get(pol, pol) or 'previous')})")

        # ---- review card
        sev_ = tr.get("severity") if tr.get("severity") in ("high", "medium", "low", "info") else "info"
        icon = ICON["check"] if sev_ == "info" else ICON["alert"]
        if not tr:
            review = "<section class='card rcard'><div class='rbody'><p class='mut'>Not triaged yet.</p></div></section>"
        else:
            src = "rules-based" if tr.get("source") != "agent" else f"{tr.get('provider', '')}:{tr.get('model', '')}".strip(":")
            tags = [src, f"confidence {tr.get('confidence', '?')}"]
            u = tr.get("usage") or {}
            if tr.get("source") == "agent" and u.get("input_tokens") is not None:
                tags.append(f"{(u.get('input_tokens') or 0) + (u.get('output_tokens') or 0):,} tokens"
                            + (f" (budget {u.get('token_budget', 0):,} reached)" if u.get("budget_reached") else ""))
            banner = ""
            if err := tr.get("agent_error"):
                banner = (f"<div class='banner warn'>{ICON['alert']}<div class='grow'><b>AI reviewer unavailable, rules-based review shown.</b> "
                          f"<span class='dim'>{html.escape(agent_reason(str(err)))}</span>"
                          f"<details><summary>details</summary><pre>{html.escape(str(err))}</pre></details></div><a href='/settings#agent'>Configure</a></div>")
            body = []
            if tr.get("summary"):
                body.append(f"<p>{link_ids(tr['summary'])}</p>")
            if "findings" in tr:
                items, marks = [], agreement_marks(tr)
                lifes = tr.get("life") if isinstance(tr.get("life"), list) else []  # reviews stored before the ledger have none
                for i, f in enumerate((normalize_finding(x) for x in tr.get("findings") or []), 1):
                    badge = life_badge(lifes[i - 1]) if i <= len(lifes) else ""
                    disc = ""
                    if self.allows("chat"):
                        disc = (f"<a class='chat-link' data-chat='finding-{i}' href='/api/snapshots/{html.escape(sid)}/chat/finding-{i}'>"
                                f"{ICON['chat']}Discuss{self._thread_count(sid, f'finding-{i}')}</a>")
                    ev = "".join(f"<li>{link_ids(e)}</li>" for e in (f.get("evidence") or [])[:4])
                    dl = ((f"<dt>Impact</dt><dd>{link_ids(f['impact'])}</dd>" if f.get("impact") else "")
                          + (f"<dt>Evidence</dt><dd><ul>{ev}</ul></dd>" if ev else "")
                          + (f"<dt>Fix</dt><dd>{link_ids(f['fix'])}</dd>" if f.get("fix") else ""))
                    items.append(f"<li><div class='fh'><span class='n'>{i}</span>{sev(f.get('severity'))}{marks.get(i - 1, '')}"
                                 f"<code class='where'>{link_ids(f.get('where', ''))}</code><span class='fs'>{link_ids(f.get('summary', ''))}</span>{badge}{disc}</div>"
                                 + (f"<dl>{dl}</dl>" if dl else "") + "</li>")
                if items:
                    body.append(f"<ol class='flist'>{''.join(items)}</ol>")
                body.append(still_open_html(tr))  # empty for a review without a ledger
                body.append(dropped_html(tr))  # the rules findings the model left out; its own block (see dropped_html)
            else:  # older stores: only the rendered Markdown
                md = self.store.get(f"{sid}/triage.md").decode() if self.store.exists(f"{sid}/triage.md") else ""
                body.append(f"<div class='review'>{md_to_html(md)}</div>")
            if tr.get("checked"):
                parts = [re.split(r" against | \(", c.strip())[0] for c in tr["checked"].split(",") if c.strip()]
                body.append("<div class='checked'><span class='lbl'>Checked</span>" + "".join(f"<span class='pill2'>{html.escape(c)}</span>" for c in parts) + "</div>")
            files = "".join(f"<a data-panel href='/f/{html.escape(sid)}/{html.escape(f)}'>{html.escape(f)}</a>" for f in d["files"])
            body.append(f"<div class='filesrow'><span>Files</span>{files}</div>")
            review = (f"<section class='card flush rcard' aria-labelledby='rev-h'><div class='rhead'><div class='left'><span class='ricon {sev_}' aria-hidden='true'>{icon}</span>"
                      f"<h2 id='rev-h'>{link_ids(tr.get('title') or 'Review')}</h2></div><div class='tags'>"
                      + "".join(f"<span class='tag'>{html.escape(t_)}</span>" for t_ in tags) + f"</div></div>{banner}<div class='rbody'>{''.join(body)}</div></section>")
        if not tr:  # still expose the files
            files = "".join(f"<a data-panel href='/f/{html.escape(sid)}/{html.escape(f)}'>{html.escape(f)}</a>" for f in d["files"])
            review = review.replace("</div></section>", f"<div class='filesrow'><span>Files</span>{files}</div></div></section>")

        # ---- charts and top SQL
        w = d.get("waits") or {}
        chart_html = charts.waits_chart(w.get("current", {}), w.get("reference", {})) + charts.delta_chart(d.get("load_profile", {}))
        top = ""
        if top_rows:
            trs = []
            cpu = any(q.get("cpu_time_ms") is not None for q in top_rows)  # Oracle and SQL Server record CPU time
            for q in top_rows:
                qid = str(q.get("sql_id") or "")
                ask = ""
                if self.allows("chat") and chat.KEY_RE.match(f"sql-{qid}"):
                    ask = (f"<a class='askbtn' data-chat='sql-{html.escape(qid)}' href='/api/snapshots/{html.escape(sid)}/chat/sql-{html.escape(qid)}'"
                           f" aria-label='Ask AI about this query' title='Ask AI about this query'>{ICON['chat']}</a>")
                trs.append(f"<tr><td><a class='mono' href='/t/{T}/sql/{html.escape(qid)}' title='{html.escape(qid)}'>{html.escape(short(qid))}</a></td>"
                           f"<td data-v='{sort_value(q.get('share_of_time') or 0)}'>{charts.share_bar(q.get('share_of_time') or 0)}</td>"
                           f"<td class='r mono dim' data-v='{sort_value(q.get('calls'))}'>{q.get('calls')}</td>"
                           f"<td class='r mono dim' data-v='{sort_value(q.get('mean_time_ms'))}'>{q.get('mean_time_ms')}</td>"
                           + (f"<td class='r mono dim' data-v='{sort_value(q.get('cpu_time_ms'))}'>{charts.fmt(q.get('cpu_time_ms'))}</td>" if cpu else "")
                           + f"<td><code class='sqltext'>{html.escape((q.get('text') or '')[:160])}</code></td>"
                           f"<td class='r'>{'<span class=\"sev medium\">plan changed</span>' if q.get('plan_changed') else ''}{ask}</td></tr>")
            basis = d.get("top_sql_basis")
            heading = "Top SQL in window" if basis in (None, "window") else "Top SQL"
            note = f"<span class='mut'>{html.escape(basis)}; no baseline yet</span>" if basis and basis != "window" else ""
            top = (f"<section class='card flush' aria-labelledby='sql-h'><div class='cardhead'><h2 id='sql-h'>{heading}</h2>{note}</div><div class='scroll'>"
                   "<table class='list topsql sortable'><thead><tr><th>SQL id</th><th>Share</th><th class='r'>Calls</th><th class='r'>Mean ms</th>"
                   + ("<th class='r'>CPU ms</th>" if cpu else "") + "<th>Text</th><th></th></tr></thead>"
                   f"<tbody>{''.join(trs)}</tbody></table></div></section>")

        # ---- discuss panel
        discuss, discuss_btn = "", ""
        if self.allows("chat") and tr:
            ok, why = llm.configured(self.cfg.agent)
            ctx = [T, f"{captured[11:19]} vs {b_time}" if b_time else f"{captured[11:19]}, no baseline", "triage.md + compare.json"]
            sugg = self._suggestions(sid, tr, top_rows, d.get("load_profile") or {}, m)
            who = "AI" if tr.get("source") == "agent" else "R"
            intro = (f"<div class='dmsg assistant'><span class='avatar' title='{'the model review' if who == 'AI' else 'the rules-based review'}'>{who}</span>"
                     f"<div class='dtext'><p>{link_ids(tr.get('summary') or tr.get('title') or '')}</p>"
                     + ("<p class='dim'>Ask about any finding, query or number; answers come from the stored snapshot, never the live database.</p>")
                     + "</div></div>" + (f"<div class='sugg'><span class='lbl'>Suggested</span>{sugg}</div>" if sugg else ""))
            warn = "" if ok else (f"<div class='dwarn'>{ICON['alert']}<span>Model provider not configured ({html.escape(why)}). "
                                  f"<a href='/settings#agent'>Configure</a> to enable replies.</span></div>")
            discuss = (f"<aside id='discuss' class='discuss' aria-label='Discuss this review' data-base='/api/snapshots/{html.escape(sid)}/chat/' data-ready='{'1' if ok else '0'}'>"
                       f"<div class='dhead'><div class='left'><span class='aicon' aria-hidden='true'>{ICON['chat']}</span><h2>Discuss this review</h2></div>"
                       f"<button type='button' class='x' id='discuss-close' aria-label='Close discussion'>{ICON['x']}</button></div>"
                       "<div class='dctx'><span>Context</span>" + "".join(f"<span class='pill2 mono'>{html.escape(c)}</span>" for c in ctx[:1])
                       + "".join(f"<span class='pill2 mono'>{html.escape(c)}</span>" for c in ctx[1:]) + "<span class='pill2 mono focus' id='discuss-focus'>whole review</span></div>"
                       f"<div class='dlog' id='chat-log'></div><template id='discuss-intro'>{intro}</template>"
                       f"<form class='dfoot' id='chat-form'>{warn}<label><span class='sr'>Your question</span>"
                       "<textarea id='chat-q' rows='3' maxlength='2000' placeholder='Ask about this snapshot, a query, or the comparison…' required></textarea></label>"
                       f"<div class='drow'><span class='mut'>Enter to send · Shift+Enter for newline</span><button type='submit' id='chat-send' class='ai'{'' if ok else ' disabled'}>Send{ICON['arrow']}</button></div></form></aside>")
            discuss_btn = f"<button type='button' class='ai' id='discuss-open'>{ICON['chat']}Discuss with AI</button>"
        actions = f"<a class='btn' href='/t/{T}/compare?a={html.escape(sid)}'>Change reference</a>" + discuss_btn
        return self.page("snapshot.html", sid, active=target, sid=sid, target=T, captured=captured, time=captured[11:19],
                         mode=html.escape(m["window"]["mode"]) + vs + _db_snapshot_note(m.get("db_snapshot")), review=review, top_sql=top, charts=chart_html, actions=actions, discuss=discuss,
                         sevpill=sev(tr.get("severity")) if tr else "", engine=html.escape(m.get("engine", "")))

    def error_page(self, code: int, msg: str) -> bytes:
        title = {404: "Not found", 503: "Temporarily unavailable", 403: "Not allowed", 400: "Bad request"}.get(code, "Something went wrong")
        hint = {404: "It may have been deleted, or the link is out of date.",
                503: "The data this page needs is out of reach right now. Try again shortly.",
                500: "The error has been logged on the server."}.get(code, "")
        body = (f"<div class='pagehead'><div class='lead'><div class='crumbs'><a href='/'>Targets</a></div>"
                f"<div class='titlerow'><h1>{html.escape(title)}</h1><span class='chip'>{code}</span></div></div></div>"
                f"<section class='card'><p>{html.escape(msg)}</p>" + (f"<p class='mut'>{html.escape(hint)}</p>" if hint else "")
                + "<p><a class='btn' href='/'>Back to the overview</a></p></section>")
        return self.page_html(f"error {code}", body)

    def page_html(self, title: str, body: str, **kw) -> bytes:
        """A page from ready-made body HTML (no template file)."""
        return self.page(string.Template("$body"), title, body=body, **kw)

    def _thread_count(self, sid: str, key: str) -> str:
        n = getattr(self, "_threads_cache", {}).get(sid, {}).get(key)
        if n is None:
            self._threads_cache = {sid: chat.list_threads(self.store, sid)}
            n = self._threads_cache[sid].get(key)
        return f" · {n // 2}" if n else ""

    def _suggestions(self, sid: str, tr: dict, top_rows: list[dict], lp: dict, m: dict) -> str:
        """Starter questions drawn from this snapshot's data; a click puts the question in the box, nothing is sent."""
        out: list[tuple[str, str]] = []
        fs = tr.get("findings") or []
        if fs:
            out.append(("finding-1", f"Why is {fs[0].get('where') or 'finding 1'} flagged, and what should I check first?"))
        moved = sorted(((k, v["pct"]) for k, v in lp.items() if isinstance(v, dict) and v.get("pct") is not None and k != "elapsed_s"), key=lambda kv: -abs(kv[1]))
        if moved and abs(moved[0][1]) >= 20:
            k, pct = moved[0]
            out.append(("review", f"Why did {k} change {pct:+.0f}% against the reference?"))
        if top_rows:
            q = str(top_rows[0].get("sql_id") or "")
            if chat.KEY_RE.match(f"sql-{q}"):
                txt = (top_rows[0].get("text") or "").split()[:6]
                out.append((f"sql-{q}", f"What is {' '.join(txt)}… doing, and is it worth tuning?" if txt else f"Is {q[:12]} worth tuning?"))
        html_ = "".join(f"<button type='button' data-ask='{html.escape(qs)}' data-key='{html.escape(k)}'>{html.escape(qs)}</button>" for k, qs in out[:3])
        t = self.cfg.targets.get(m["target"])
        b = (m.get("baseline") or {}).get("snapshot")
        if t and b and (before := self.store.previous(t.engine, m["target"], b)):
            try:
                at = _fmt_ts(self.manifest_of(before)["captured_at"])[11:19]
            except FileNotFoundError:
                at = before.rsplit("/", 1)[-1]
            html_ += f"<a class='suggest-link' href='/t/{html.escape(m['target'])}/compare?a={html.escape(sid)}&amp;b={html.escape(before)}'>Compare against the {html.escape(at)} snapshot instead</a>"
        return html_

    def compare_page(self, name: str, a: str | None, b: str | None) -> bytes:
        """Any two snapshots of a target side by side: load deltas, wait classes, SQL changes, heuristic findings."""
        from dbtriage.cli import compare_ids

        t = self.cfg.targets[name]
        ids = list(reversed(self.store.list_snapshots(t.engine, name)))
        if not ids:
            return self.page("compare.html", "compare", active=name, target=html.escape(name), engine=t.engine, a_opts="", b_opts="",
                             notice=self._notice("no snapshots yet", "bad"), tiles="", charts="", findings="", settings="", sql="")
        a = a if a in ids else ids[0]
        if b not in ids:
            m = self.manifest_of(a)
            b = (m.get("baseline") or {}).get("snapshot") or self.store.previous(t.engine, name, a)
        when = {sid: _fmt_ts(self.manifest_of(sid)["captured_at"]) for sid in ids}

        def opts(sel):
            return "".join(f"<option value='{html.escape(sid)}'{' selected' if sid == sel else ''}>{when[sid]}</option>" for sid in ids)

        notice, tiles, chart_html, findings_html, settings_html, sql_html = "", "", "", "", "", ""
        if not b or b == a:
            notice = self._notice("pick two different snapshots", "bad")
        else:
            try:
                c = compare_ids(self.store, t.engine, name, a, b)
            except (FileNotFoundError, KeyError, ValueError) as exc:
                c, notice = None, self._notice(f"cannot compare: {type(exc).__name__}: {exc}", "bad")
            if c is None:
                notice = notice or self._notice("nothing to compare", "bad")
            else:
                lp = c.get("load_profile") or {}
                w = c.get("waits") or {}
                found = c.get("findings") or []
                tiles = "".join(f"<div class='tile'><span class='lbl'>{l}</span><span class='num {k}'>{v}</span></div>" for l, v, k in (
                    ("Current", f"<a href='/s/{html.escape(a)}'>{when[a][5:16]}</a>", ""), ("Baseline", f"<a href='/s/{html.escape(b)}'>{when[b][5:16]}</a>", ""),
                    ("Heuristic findings", str(len(found)), (found[0]["severity"] if found else "")),
                    ("Severity", (found[0]["severity"] if found else "info"), (found[0]["severity"] if found else "info"))))
                chart_html = charts.waits_chart(w.get("current") or {}, w.get("reference") or {}) + charts.delta_chart(lp)
                if found:
                    lis = "".join(f"<li>{sev(f.get('severity'))} {html.escape(f.get('title', ''))}" + (f"<ul><li>{html.escape(f['detail'])}</li></ul>" if f.get("detail") else "") + "</li>" for f in found[:8])
                    findings_html = f"<section class='card review'><h2>Heuristic findings</h2><ol>{lis}</ol></section>"
                if changed := c.get("settings_changed"):  # parameters that moved inside the window, beside the regressions
                    trs = "".join(f"<tr><td><code>{html.escape(str(r.get('name')))}</code></td><td class=mono>{html.escape(_show(r.get('before')))}</td>"
                                  f"<td class=mono>{html.escape(_show(r.get('after')))}</td></tr>" for r in changed)
                    settings_html = (f"<section class='card flush'><div class='cardhead'><h2>Settings changed</h2><span class='mut'>{len(changed)}</span></div>"
                                     f"<div class='scroll'><table class='list'><thead><tr><th>parameter</th><th>before</th><th>after</th></tr></thead><tbody>{trs}</tbody></table></div></section>")
                if feed := c.get("changes"):  # deploys and jobs from the stored feed that sit in or just before the window
                    trs = "".join(f"<tr><td class=mono>{html.escape(_fmt_ts(r.get('time')))}</td><td>{html.escape(str(r.get('kind') or ''))}</td>"
                                  f"<td>{_change_link(r)}</td></tr>" for r in feed[:10])
                    settings_html += (f"<section class='card flush changes'><div class='cardhead'><h2>Changes</h2><span class='mut'>{len(feed)}</span></div>"
                                      f"<div class='scroll'><table class='list'><thead><tr><th>when</th><th>kind</th><th>what</th></tr></thead><tbody>{trs}</tbody></table></div></section>")
                sq = c.get("sql") or {}
                parts = []
                for key, label in (("new", "New"), ("changed", "Changed"), ("gone", "Gone")):
                    rows = sq.get(key) or []
                    if not rows:
                        continue
                    trs = "".join(f"<tr><td><a href='/t/{html.escape(name)}/sql/{html.escape(str(r.get('sql_id')))}'><code>{html.escape(str(r.get('sql_id')))}</code></a></td>"
                                  f"<td>{charts.share_bar(r.get('share_of_time') or 0)}</td><td class='r mono'>{charts.fmt(r.get('calls'))}</td><td class='r mono'>{charts.fmt(r.get('mean_time_ms'))}</td>"
                                  f"<td>{'yes' if r.get('plan_changed') else ''}</td><td class=mut>{html.escape((r.get('text') or '')[:110])}</td></tr>" for r in rows[:10])
                    parts.append(f"<section class='card flush'><div class='cardhead'><h2>{label} SQL</h2><span class='mut'>{len(rows)}</span></div><div class='scroll'><table class='list'>"
                                 f"<thead><tr><th>sql id</th><th>share</th><th class=r>calls</th><th class=r>mean ms</th><th>plan</th><th>text</th></tr></thead><tbody>{trs}</tbody></table></div></section>")
                sql_html = "".join(parts)
        return self.page("compare.html", f"compare {name}", active=name, target=html.escape(name), engine=t.engine, a_opts=opts(a), b_opts=opts(b),
                         notice=notice, tiles=tiles, charts=chart_html, findings=findings_html, settings=settings_html, sql=sql_html)

    def search(self, q: str) -> bytes:
        """Targets by name, SQL ids seen in any snapshot's top SQL, and snapshot ids: enough to jump anywhere."""
        q = q.strip()
        ql = q.lower()
        targets = [n for n in sorted(self.cfg.targets) if ql and ql in n.lower()]
        sqls: list[tuple[str, str, str]] = []
        snaps: list[tuple[str, str]] = []
        if len(ql) >= 3:
            for n, t in sorted(self.cfg.targets.items()):
                for sid in reversed(self.store.list_snapshots(t.engine, n)[-200:]):
                    if ql in sid.lower():
                        snaps.append((n, sid))
                    if not self.store.exists(f"{sid}/compare.json"):
                        continue
                    for r in self.store.get_json(f"{sid}/compare.json").get("sql", {}).get("top", []):
                        sid_ = str(r.get("sql_id") or "")
                        if ql in sid_.lower() and not any(x[1] == sid_ and x[0] == n for x in sqls):
                            sqls.append((n, sid_, (r.get("text") or "")[:100]))
                    if len(sqls) > 50:
                        break
        out = []
        if targets:
            out.append("<section class='card'><h2>Targets</h2><div class='meta'>" + " ".join(f"<a class='btn' href='/t/{html.escape(n)}'>{html.escape(n)}</a>" for n in targets) + "</div></section>")
        if sqls:
            trs = "".join(f"<tr><td><a href='/t/{html.escape(n)}/sql/{html.escape(s_)}'><code>{html.escape(s_)}</code></a></td><td class=dim>{html.escape(n)}</td><td class=mut>{html.escape(txt)}</td></tr>" for n, s_, txt in sqls)
            out.append(f"<section class='card flush'><div class='cardhead'><h2>SQL ids</h2><span class=mut>{len(sqls)}</span></div><div class=scroll><table class=list><thead><tr><th>sql id</th><th>target</th><th>text</th></tr></thead><tbody>{trs}</tbody></table></div></section>")
        if snaps:
            trs = "".join(f"<tr><td><a class=ts href='/s/{html.escape(s_)}'>{html.escape(s_)}</a></td><td class=dim>{html.escape(n)}</td></tr>" for n, s_ in snaps[:50])
            out.append(f"<section class='card flush'><div class='cardhead'><h2>Snapshots</h2></div><div class=scroll><table class=list><thead><tr><th>snapshot</th><th>target</th></tr></thead><tbody>{trs}</tbody></table></div></section>")
        if not out:
            out.append(f"<section class='card'><p class='mut'>{'Type at least 3 characters for SQL ids.' if q and len(ql) < 3 else 'Nothing matched.'}</p></section>")
        return self.page("search.html", f"search {q}", q=q, results="".join(out))

    def sql_page(self, name: str, sql_id: str) -> bytes:
        from datetime import datetime

        rows = self.sql_history(name, sql_id)
        text = next((r["text"] for r in reversed(rows) if r.get("text")), "")
        pts = [{"id": r["id"], "t": datetime.fromisoformat(r["captured_at"]), "value": r.get("mean_time_ms"), "severity": r["severity"],
                "title": f"{r.get('calls')} calls", "plan_changed": r["plan_changed"]} for r in rows]
        chart = charts.trend_chart(pts, "mean ms") if pts else ""
        cpu = any(r.get("cpu_time_ms") is not None for r in rows)  # Oracle and SQL Server record CPU time
        trs = "".join(f"<tr><td><a href='/s/{html.escape(r['id'])}'>{_fmt_ts(r['captured_at'])}</a></td><td>{html.escape(r.get('mode') or '')}</td>"
                      f"<td>{charts.share_bar(r.get('share_of_time') or 0)}</td><td>{charts.fmt(r.get('calls'))}</td><td>{charts.fmt(r.get('total_time_ms'))}</td>"
                      + (f"<td>{charts.fmt(r.get('cpu_time_ms'))}</td>" if cpu else "")
                      + f"<td>{charts.fmt(r.get('mean_time_ms'))}</td><td>{charts.fmt(r.get('rows'))}</td><td>{charts.fmt(r.get('io_blocks_read'))}</td>"
                      f"<td><code>{html.escape(str(r.get('plan_hash') or ''))}</code>{' <span class=\'sev medium\'>changed</span>' if r['plan_changed'] else ''}</td>"
                      f"<td>{sev(r['severity'])}</td></tr>" for r in reversed(rows))
        plans = len({r.get("plan_hash") for r in rows if r.get("plan_hash")})
        return self.page("sql.html", sql_id, active=name, target=html.escape(name), sql_id=html.escape(sql_id), text=html.escape(text),
                         seen=len(rows), plans=plans, chart=chart, rows=trs,
                         cpu_th='<th class="r">cpu ms</th>' if cpu else "")

    # ---------------------------------------------------------------- schedule
    def start_scheduler(self):
        """The in-process scheduler (dbtriage web --schedule): fires runs through start_run, so the run lock and gates apply."""
        def fire(name: str, window: int, reason: str) -> tuple[bool, str]:
            code, out = self.start_run(name, "scheduled", reason, True, window_minutes=window, source="schedule")
            return code == 202, out.get("error", "started")

        self.scheduler = scheduler.Scheduler(lambda: {n: t.extra for n, t in self.cfg.targets.items()}, fire,
                                             on_tick=self._housekeeping)
        self.scheduler.start()

    def _housekeeping(self):
        """Each scheduler tick: pick up .env edits, push anything spooled during a store outage."""
        if envfile.refresh():
            self.reload()
        if hasattr(self.store, "flush"):
            self.store.flush()

    def schedule_rows(self) -> list[dict]:
        extras = {n: t.extra for n, t in self.cfg.targets.items()}
        return self.scheduler.rows(extras) if self.scheduler else scheduler.rows_without_scheduler(extras)

    def save_schedule(self, name: str, fields: dict) -> list[str]:
        if name not in self.cfg.targets:
            return [f"unknown target {name!r}"]
        problems = self.settings.put_schedule(name, self.seed, fields)
        if not problems:
            self.reload()
            if self.scheduler:
                self.scheduler.tick()  # recompute next runs now rather than at the next tick
        return problems

    def _schedule_card(self, ro: str) -> str:
        rows = []
        for r in self.schedule_rows():
            n = html.escape(r["target"])
            custom = r["schedule"] and r["schedule"] not in scheduler.LABELS
            opts = "".join(f"<option value='{html.escape(c)}'{' selected' if c == r['schedule'] else ''}>{html.escape(l)}</option>" for c, l in scheduler.PRESETS)
            opts += f"<option value='custom'{' selected' if custom else ''}>custom cron…</option>"
            nxt = _fmt_ts(r["next"].isoformat()) [:16] if r["next"] else ("paused" if r["paused"] and r["schedule"] else "–")
            last = (_fmt_ts(r["last"].isoformat())[:16] + (f" <span class=mut>{html.escape(r['last_status'])}</span>" if r["last_status"] else "")) if r["last"] else "–"
            fid = "sched-" + re.sub(r"[^A-Za-z0-9_-]", "_", r["target"])
            f = f"form='{fid}'"  # every control names its form, so a row stays wired after it is replaced in place
            pause = (f"<button{ro} {f} class=quiet name=action value=resume>Resume</button>" if r["paused"] else f"<button{ro} {f} class=quiet name=action value=pause>Pause</button>") if r["schedule"] else ""
            runbtn = f"<button{'' if self.allows('run') else ' disabled'} {f} class=quiet name=action value=run title='collect and review now'>Run now</button>"
            rows.append(f"<tr{' class=paused' if r['paused'] else ''}><td><form id='{fid}' method=post action=/schedule/save class=inline data-ajax=refresh-card>"
                        f"<input type=hidden name=name value='{n}'></form><a href='/t/{n}'>{n}</a></td>"
                        f"<td><select {f} name=schedule class=sched aria-label='schedule for {n}'>{opts}</select> <input {f} name=schedule_custom class=cron aria-label='cron line for {n}' "
                        f"value='{html.escape(r['schedule']) if custom else ''}' placeholder='*/10 * * * *'{' hidden' if not custom else ''}></td>"
                        f"<td><input {f} name=schedule_window type=number min=5 max=1440 value='{r['window']}' class=num aria-label='window in minutes for {n}'> min</td>"
                        f"<td class=ts>{nxt}</td><td class=ts>{last}</td>"
                        f"<td class=actions><button{ro} {f}>Save</button> {pause} {runbtn}</td></tr>")
        if self.scheduler:
            status = f"<span class='ok'>●</span> scheduler running in this process since {_fmt_ts(self.scheduler.started_at.isoformat())[:16]} (local time)"
        else:
            status = "<span class='mut'>○</span> no scheduler in this process: start the web view with <code>--schedule</code>, run <code>dbtriage scheduler</code> on a host that reaches the databases, or keep using cron"
        return (f"<div class=scroll><table class=list><thead><tr><th>target</th><th>schedule</th><th>window</th><th>next</th><th>last</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
                f"<p class=mut>{status}</p>")

    # ---------------------------------------------------------------- discuss a finding
    def chat_thread(self, sid: str, key: str) -> dict:
        self.manifest_of(sid)  # 404 for unknown snapshots
        t = chat.load_thread(self.store, sid, key)
        for x in t["messages"]:
            if x["role"] == "assistant":
                x["html"], x["footer"] = md_to_html(x["text"]), chat.footer(x)
        return t

    def chat_ask(self, sid: str, key: str, question: str) -> tuple[int, dict]:
        if not self.allow_chat:
            return 403, {"error": "discussion is disabled (start with --allow-chat)"}
        if not self.allows("chat"):
            return 403, {"error": "your group cannot use chat"}
        finding = key if key.startswith("sql-") else (int(key.split("-", 1)[1]) if key.startswith("finding-") else None)
        try:  # decided from the snapshot itself: read it, so an outage is a 503, not a missing snapshot
            if not key_ok(sid):
                raise FileNotFoundError(sid)
            self.store.get_json(f"{sid}/manifest.json")
        except FileNotFoundError:
            return 404, {"error": "no such snapshot"}
        except StorageUnavailable as exc:
            return 503, {"error": f"the store is unavailable: {exc}"}
        except Exception as exc:  # noqa: BLE001 - any other store error is an answer, never a dropped request
            return 502, {"error": f"could not read the snapshot: {type(exc).__name__}: {str(exc)[:200]}"}
        try:
            answer = chat.Chat(self.store, lambda: llm.make_backend(self.cfg.agent)).ask(sid, finding, question)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except StorageUnavailable as exc:  # the store went away during the discussion: the same 503 as before it
            return 503, {"error": f"the store is unavailable: {exc}"}
        except SystemExit as exc:  # backend configuration problems surface as SystemExit
            return 502, {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - provider errors must not kill the server
            print(f"web: chat for {sid}/{key} failed:\n{traceback.format_exc()}", file=sys.stderr)
            return 502, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        answer["html"], answer["footer"] = md_to_html(answer["text"]), chat.footer(answer)
        access.audit("chat", f"{sid} {key}")
        return 200, answer

    # ---------------------------------------------------------------- settings (UI-editable config)
    def _notice(self, msg: str | None, kind: str = "") -> str:
        return f"<div class='notice {kind}'>{html.escape(msg)}</div>" if msg else ""

    def settings_page(self, msg: str | None = None, kind: str = "") -> bytes:
        w = self.web
        rows = []
        for name, t in sorted(self.cfg.targets.items()):
            edit = f"<a href='/targets/{html.escape(name)}'>edit</a>" if self.allows("edit") else ""
            rows.append(f"<tr><td><a href='/t/{html.escape(name)}'>{html.escape(name)}</a></td><td>{t.engine}</td>"
                        f"<td><code>{html.escape(mask_dsn(t.dsn) or ('$' + str(t.extra.get('dsn_env'))))}</code></td>"
                        f"<td class=mut>{self.settings.origin(name, self.seed)}</td><td>{edit}</td></tr>")
        ro = "" if self.allows("edit") else " disabled"
        notice = self._gate_notice(msg, kind, self.allow_edit, self.allows("edit"),
                                   "Read-only: start the web view with --allow-edit to change settings here.",
                                   "Read-only: your group cannot edit.")
        from dbtriage.awscred import profile_name, status_line

        return self.page("settings.html", "settings", active="/settings", notice=notice,
                         target_rows="\n".join(rows), add_link="<a href='/targets/new'>+ add target</a>" if self.allows("edit") else "",
                         db_writes_note=self._db_writes_note(), mcp_note=self._mcp_note(),
                         web_title=html.escape(w["title"]), brand=html.escape(w["brand"]), logo=html.escape(w.get("logo") or ""),
                         accent=html.escape(w["accent"]), refresh_seconds=int(w["refresh_seconds"]), page_size=int(w["page_size"]),
                         footer=html.escape(w.get("footer") or ""), settings_file=html.escape(self.settings.where),
                         theme_opts="".join(f"<option{' selected' if w['theme'] == o else ''}>{o}</option>" for o in ("auto", "light", "dark")),
                         save_button=f"<button{ro} class=primary>Save</button>", schedule_card=self._schedule_card(ro),
                         aws_status=html.escape(status_line(profile_name(self.cfg.agent))),
                         **self._agent_fields(ro), **self._delivery_fields(ro))

    def _agent_fields(self, ro: str) -> dict:
        a = self.cfg.agent
        opt = lambda cur, vals: "".join(f"<option value='{v}'{' selected' if cur == v else ''}>{v or '(default)'}</option>" for v in vals)  # noqa: E731
        fields = {f"a_{k}": html.escape(str(a.get(k) or "")) for k in AGENT_FIELDS}
        fields["a_max_iterations"] = int(a.get("max_iterations") or 25)
        fields["a_token_budget"] = llm.token_budget(a)  # 0 is a real value here: no limit
        return {**fields, "provider_opts": opt(a.get("provider", "anthropic"), AGENT_PROVIDERS),
                "sched_provider_opts": opt(a.get("scheduled_provider") or "", ("", *AGENT_PROVIDERS)).replace(">(default)<", ">(same as above)<"),
                "effort_opts": opt(a.get("effort", "high") or "", ("", "low", "medium", "high")),
                "agent_buttons": f"<button{ro}>Save</button> <button{ro} class=quiet name=action value=test>Test model</button>"}

    def agent_models(self, fields: dict) -> tuple[list[str], str, str]:
        """Models for the provider picked in the form. On the saved provider, saved settings fill the gaps; on another
        provider, form values that still hold the saved provider's settings (its key variable, its base URL) are
        dropped, so OpenRouter is never asked with Groq's key."""
        saved = self.cfg.agent
        prov = fields.get("provider") or saved.get("provider") or "anthropic"
        if prov == saved.get("provider"):
            cfg = {**saved, **fields}
        else:
            cfg = {k: v for k, v in fields.items() if k == "provider" or v != saved.get(k)}
        cfg["provider"] = prov
        cfg = {k: v for k, v in cfg.items() if k in AGENT_FIELDS or k in saved}  # a request cannot add api_key_cmd
        ids, note = llm.list_models(cfg)
        return ids, note, prov

    def _delivery_fields(self, ro: str) -> dict:
        d = self.settings.delivery_view(self.seed)
        enabled = d.get("channels", ["stdout"])
        toggles = " ".join(f"<label class=inl><input type=checkbox name=channels value='{ch}'{' checked' if ch in enabled else ''}> {ch}</label>" for ch in CHANNEL_FIELDS)
        blocks = []
        for ch, spec in CHANNEL_FIELDS.items():
            if not spec:
                continue
            cur = d.get(ch, {})
            inputs = []
            for name, kind, default in spec:
                v = cur.get(name, default if kind == "sev" else "")  # a gate shows what applies when unset
                if kind == "sev":
                    opts = "".join(f"<option value='{o}'{' selected' if v == o else ''}>{o or '(global)'}</option>" for o in ("", *SEVERITIES))
                    inputs.append(f"<label>{name} <select name='{ch}.{name}'>{opts}</select></label>")
                elif kind == "secret":
                    inputs.append(f"<label>{name} <input name='{ch}.{name}' type=password autocomplete=off placeholder='{html.escape(str(v) or 'https://…')}'></label>")
                else:
                    val = ", ".join(v) if isinstance(v, list) else str(v or default)
                    inputs.append(f"<label>{name} <input name='{ch}.{name}' value='{html.escape(val)}'></label>")
            test = f"<button{ro} class=quiet name=action value='test:{ch}'>Send test via {ch}</button>"
            blocks.append(f"<fieldset class=chan><legend>{ch}</legend>{''.join(inputs)}<div class=row>{test}</div></fieldset>")
        return {"min_sev_opts": "".join(f"<option{' selected' if d.get('min_severity', 'info') == o else ''}>{o}</option>" for o in SEVERITIES),
                "channel_toggles": toggles, "channel_blocks": "\n".join(blocks),
                "delivery_buttons": f"<button{ro}>Save</button> <button{ro} class=quiet name=action value='test:stdout'>Send test via stdout</button>"}

    @staticmethod
    def parse_delivery_form(form: dict) -> dict:
        g = lambda k: form.get(k, [""])[0].strip()  # noqa: E731
        data: dict = {"channels": form.get("channels", []), "min_severity": g("min_severity")}  # absent = every box unchecked
        for ch, spec in CHANNEL_FIELDS.items():
            data[ch] = {name: g(f"{ch}.{name}") for name, _kind, _d in spec}
        return data

    def latest_review(self) -> tuple[dict, str]:
        """Newest stored review across targets, or a short synthetic one when the store is empty."""
        best = None
        for name, t in self.cfg.targets.items():
            sid = self.store.latest(t.engine, name) if hasattr(self.store, "latest") else None
            if sid and self.store.exists(f"{sid}/triage.json"):
                m = self.manifest_of(sid)
                if best is None or m["captured_at"] > best[0]:
                    best = (m["captured_at"], sid)
        if best:
            sid = best[1]
            return self.store.get_json(f"{sid}/triage.json"), self.store.get(f"{sid}/triage.md").decode()
        tr = {"severity": "info", "title": "delivery test", "target": "dbtriage", "engine": "none", "ts": 0, "summary": "test message"}
        return tr, "✅ **DB review · dbtriage** test message from the settings page."

    def test_delivery(self, channel: str) -> tuple[bool, str]:
        from dbtriage.deliver import test_channel

        tr, md = self.latest_review()
        return test_channel(self.cfg.delivery, channel, tr, md)

    def test_agent(self, fields: dict | None = None) -> tuple[bool, str]:
        from dbtriage.llm import test_model

        # only the Agent card's own fields: never api_key_cmd (a command) or anything else a request might carry
        cfg = {**self.cfg.agent, **{k: v for k, v in (fields or {}).items() if k in AGENT_FIELDS and v not in (None, "")}}
        return test_model(cfg)

    def target_form(self, name: str | None, msg: str | None = None, kind: str = "", values: dict | None = None) -> bytes:
        v = values if values is not None else (self.settings.form_of(name, self.seed) if name else {})
        new = not name
        links = "\n".join(f"{l.get('label', '')} {l.get('url', '')}" for l in v.get("links") or [])
        ro = "" if self.allows("edit") else " disabled"
        buttons = f"<button{ro}>Save</button> <button{ro} class=quiet name=action value=probe>Test connection</button>"
        if not new and self.allows("edit"):
            buttons += f" <button class=danger name=action value=delete formaction='/targets/{html.escape(name)}/delete'>Delete</button>"
        notice = self._gate_notice(msg, kind, self.allow_edit, self.allows("edit"),
                                   "Read-only: start the web view with --allow-edit to change targets here.",
                                   "Read-only: your group cannot edit.")
        return self.page("target_edit.html", "add target" if new else name, active="/settings",
                         heading="Add target" if new else f"Edit target {html.escape(name)}", notice=notice,
                         tname=html.escape(name or v.get("name", "")), name_ro="" if new else "readonly",
                         engine_opts="".join(f"<option{' selected' if v.get('engine') == e else ''}>{e}</option>" for e in ENGINES),
                         dsn="", dsn_placeholder=html.escape(mask_dsn(v.get("dsn")) or "postgresql://user:pass@host:5432/db · mysql://… · host:1521/service"),
                         dsn_env=html.escape(v.get("dsn_env") or ""), user=html.escape(v.get("user") or ""), password="",
                         top_n=top_n_of(v.get("top_n")), diagnostics_pack="checked" if v.get("diagnostics_pack") else "",
                         snapshot_opts=self._snapshot_opts(v.get("create_snapshot")), snapshot_note=self._db_writes_note(short=True),
                         changes_file=html.escape(v.get("changes_file") or ""),
                         links=html.escape(links), notes=html.escape(v.get("notes") or ""), buttons=buttons,
                         baseline_opts="".join(f"<option value='{p}'{' selected' if (v.get('baseline_policy') or 'previous') == p else ''}>{LABELS[p]}</option>" for p in POLICIES),
                         baseline=html.escape(v.get("baseline") or ""))

    @staticmethod
    def _snapshot_opts(value) -> str:
        try:
            cur = dbsnap.mode_of(value)
        except ValueError:
            cur = "off"
        labels = {"off": "Off: only read", "incident": "Incident runs", "always": "Every run"}
        return "".join(f"<option value='{m}'{' selected' if m == cur else ''}>{labels[m]}</option>" for m in dbsnap.MODES)

    def _db_writes_note(self, short: bool = False) -> str:
        if dbsnap.writes_allowed(self.cfg.collect):
            return ("targets.toml allows it ([collect] allow_db_writes)." if short else
                    "<b>Database writes are on</b> in targets.toml ([collect] allow_db_writes = true): databases set to take "
                    "snapshots on the database take one before collecting. The rest only read.")
        return ("No effect while targets.toml has dbtriage only read: set allow_db_writes = true under [collect] there." if short else
                "dbtriage only reads your databases. To let it take snapshots on them (an AWR snapshot before each Oracle "
                "collection), set <code>allow_db_writes = true</code> under <code>[collect]</code> in targets.toml, then pick "
                "the databases on their edit pages. This page cannot turn it on.")

    def _mcp_note(self) -> str:
        s = mcp_server.settings(self.cfg)
        if not s["enabled"]:
            return ("The MCP server is off. To let MCP clients such as Claude Code read the stored snapshots and reviews, set "
                    "<code>enabled = true</code> under <code>[mcp]</code> in targets.toml and run <code>dbtriage mcp</code> "
                    "from the client (see docs/mcp.md). This page cannot turn it on.")
        if errs := mcp_server.problems(self.cfg):
            return ("<b>The MCP server is turned on in targets.toml but will not start:</b> "
                    + html.escape("; ".join(errs)) + ". Fix [mcp] there.")
        tools = mcp_server.offered(self.cfg)
        return (f"<b>The MCP server is on</b> in targets.toml ([mcp]): <code>dbtriage mcp</code> offers {len(tools)} tools"
                + (", including run_review, which starts runs" if mcp_server.RUN_TOOL in tools else ", all read-only") + ".")

    @staticmethod
    def parse_target_form(form: dict) -> tuple[str, dict]:
        g = lambda k: form.get(k, [""])[0].strip()  # noqa: E731
        links = []
        for line in g("links").splitlines():
            if line.strip():
                label, _, url = line.strip().partition(" ")
                links.append({"label": label, "url": url.strip()})
        return g("name"), {"engine": g("engine"), "dsn": g("dsn"), "dsn_env": g("dsn_env"), "user": g("user"), "password": g("password"),
                           "top_n": g("top_n") or 25, "diagnostics_pack": "diagnostics_pack" in form, "create_snapshot": g("create_snapshot") or "off",
                           "changes_file": g("changes_file"), "links": links, "notes": g("notes"),
                           "baseline_policy": g("baseline_policy") if g("baseline_policy") != "previous" else "", "baseline": g("baseline")}

    def save_target(self, name: str, fields: dict, keep_secrets: bool = True) -> list[str]:
        """Blank dsn/password on an edit means 'keep what is stored'."""
        if name not in self.cfg.targets and (other := next((n for n in self.cfg.targets if n.lower() == (name or "").lower()), None)):
            return [f"name: {other!r} exists; names that differ only in case would share one folder of snapshots"]
        cur = self.settings.form_of(name, self.seed)
        if keep_secrets:
            for k in ("dsn", "password"):
                if not fields.get(k) and cur.get(k):
                    fields[k] = cur[k]
        # put_target merges, so fields the form or a partial API body does not send (the schedule card's, say) stay
        problems = self.settings.put_target(name, fields, base=self.settings._seed_fields(name, self.seed))
        if not problems:
            self.reload()
        return problems

    def probe_target(self, name: str, fields: dict | None = None) -> tuple[bool, str]:
        from dbtriage.config import Target

        if fields:
            cur = self.settings.form_of(name, self.seed)
            dsn = fields.get("dsn") or cur.get("dsn") or os.environ.get(fields.get("dsn_env") or "", "")
            extra = {"user": fields.get("user"), "password": fields.get("password") or cur.get("password")}
            t = Target(name=name or "new", engine=fields.get("engine", ""), dsn=dsn, extra=extra)
        else:
            t = self.cfg.targets[name]
        return probe(t)

    def api_settings(self) -> dict:
        return {"web": {k: self.web.get(k) for k in WEB_FIELDS}, "settings_file": self.settings.where, "editable": self.allows("edit"),
                "agent": {k: self.cfg.agent.get(k) for k in AGENT_FIELDS if self.cfg.agent.get(k) not in (None, "")},
                "allow_db_writes": dbsnap.writes_allowed(self.cfg.collect),  # targets.toml only: never changed here
                "mcp": {**mcp_server.settings(self.cfg), "problems": mcp_server.problems(self.cfg),  # targets.toml only too
                        "offered": [] if mcp_server.problems(self.cfg) else mcp_server.offered(self.cfg)},
                "delivery": self.settings.delivery_view(self.seed),
                "targets": {n: {"engine": t.engine, "dsn": mask_dsn(t.dsn), "top_n": t.top_n, "diagnostics_pack": t.diagnostics_pack,
                                "origin": self.settings.origin(n, self.seed), **{k: v for k, v in t.extra.items() if k not in ("password", "dsn")}}
                            for n, t in sorted(self.cfg.targets.items())}}


def _db_snapshot_note(d: dict | None) -> str:
    """The snapshot page's word on a snapshot dbtriage took (or was asked to take) on the database."""
    if not d:
        return ""
    if d.get("status") == "taken":
        return f" · took {html.escape(str(d.get('detail', 'a snapshot')))} on the database"
    if d.get("status") == "not needed":
        return " · <span title='a snapshot on the database was recent enough'>database snapshot not needed</span>"
    return f" · <span class='dim' title='{html.escape(str(d.get('reason', '')))}'>no database snapshot ({html.escape(str(d.get('status')))})</span>"


def serve(cfg: Config, store: Storage, host: str, port: int, allow_run: bool, token: str | None, allow_edit: bool = False,
          allow_chat: bool = False, schedule: bool = False):
    make_server(cfg, store, host, port, allow_run, token, allow_edit, allow_chat, schedule).serve_forever()


def make_server(cfg: Config, store: Storage, host: str, port: int, allow_run: bool, token: str | None, allow_edit: bool = False,
                allow_chat: bool = False, schedule: bool = False) -> ThreadingHTTPServer:
    """The web view's HTTP server, not yet serving: serve() runs it forever; tests run it in a thread and shut it down."""
    app = App(cfg, store, allow_run, token, allow_edit, allow_chat)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if schedule:
        if not allow_run:
            raise SystemExit("--schedule needs --allow-run (scheduled runs go through the same gate)")
        app.start_scheduler()

    class H(BaseHTTPRequestHandler):
        server_version = "dbtriage"

        # ---- plumbing
        def _authed(self) -> bool:
            if not token:
                return True
            return hmac.compare_digest(self._basic_password().encode(), token.encode())

        def _basic_parts(self) -> tuple[str, str]:
            hdr = self.headers.get("Authorization", "")
            try:
                raw = base64.b64decode(hdr[6:]).decode() if hdr.startswith("Basic ") else ""
                user, _, password = raw.partition(":")
                return user, password
            except Exception:  # noqa: BLE001
                return "", ""

        def _basic_password(self) -> str:
            return self._basic_parts()[1]

        def _enter(self) -> None:
            """Who this request is. Proxy headers only when trusted. The Basic username counts
            only after the password matches, and never when the process has no token."""
            user = self._basic_parts()[0] if token and self._authed() else ""
            access.set_current(access.actor_from(self.headers, user))

        def _cap(self, cap: str, api: bool) -> bool:
            """True when this request may do cap. The process flag's sentence stays when the
            flag itself is off; a different sentence when the flag is on and the group is not."""
            if app.allows(cap):
                return True
            flag = {"edit": allow_edit, "run": allow_run, "chat": allow_chat}[cap]
            off = {"edit": "editing is disabled (start with --allow-edit)",
                   "run": "runs are disabled (start with --allow-run)",
                   "chat": "discussion is disabled (start with --allow-chat)"}[cap]
            narrowed = {"edit": "your group cannot edit", "run": "your group cannot start runs",
                        "chat": "your group cannot use chat"}[cap]
            self._error(403, off if not flag else narrowed, api)
            return False

        def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if "html" in ctype or "json" in ctype else "max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, default=str).encode(), "application/json", code)

        def _error(self, code: int, msg: str, as_json: bool):
            if as_json:
                return self._json({"error": msg}, code)
            self._send(app.error_page(code, msg), code=code)

        def _read_body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

        # ---- routing
        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        # ---- request origin checks
        def _host_ok(self) -> bool:
            """Bound to loopback, answer only to loopback names: a foreign domain re-pointed at 127.0.0.1 (DNS
            rebinding) must not reach the API with the browser's same-origin rights."""
            if not loopback:
                return True
            h = (self.headers.get("Host") or "").lower()
            name = h[1:].split("]", 1)[0] if h.startswith("[") else h.rsplit(":", 1)[0]
            return name in ("127.0.0.1", "localhost", "::1", "")

        def _same_origin(self) -> bool:
            """A request that changes something, starts work or calls a model must come from this site. Browsers send
            Sec-Fetch-Site and Origin; command-line clients send neither and are allowed."""
            site = self.headers.get("Sec-Fetch-Site")
            if site and site not in ("same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            if origin == "null":
                return False
            if origin:
                return urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").lower()
            return True

        def _refuse(self, why: str):
            print(f"web: refused {self.command} {urlparse(self.path).path} from {self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site') or '?'}: {why}",
                  file=sys.stderr)
            return self._error(403, why, urlparse(self.path).path.startswith("/api/"))

        def do_GET(self):  # noqa: N802
            if envfile.refresh():  # a key added to .env is live on the next request
                app.reload()
            self._enter()
            u = urlparse(self.path)
            path, q = unquote(u.path), parse_qs(u.query)
            api = path.startswith("/api/")
            if not self._host_ok():
                return self._refuse("unexpected Host header")
            if path == "/health":
                st = app.status()
                out = {"ok": st["store_ok"] and st["settings_ok"], "targets": len(app.cfg.targets), "runs_enabled": allow_run, "edit_enabled": allow_edit,
                       "chat_enabled": allow_chat, "scheduler": bool(app.scheduler), **st}
                if token and not self._authed():  # without the password, health only: no hosts, no locations
                    out = {k: out[k] for k in ("ok", "store_ok", "settings_ok", "spooled")}
                else:
                    from dbtriage.awscred import describe as aws_describe
                    from dbtriage.awscred import profile_name

                    # Cached status only. Do not call STS or the metadata service from here.
                    out["aws"] = aws_describe(profile_name(app.cfg.agent))
                return self._json(out)
            if path == "/api/settings/agent/models" and not self._same_origin():  # it calls out with the provider key
                return self._refuse("cross-site request refused")
            if not self._authed():
                return self._send(b"auth required", "text/plain", 401, {"WWW-Authenticate": 'Basic realm="dbtriage"'})
            if not app.allows("view"):
                return self._error(403, "your group cannot view this", api)
            try:
                if path == "/":
                    return self._send(app.index())
                if path == "/brand/logo" and app.logo_path and app.logo_path.exists():
                    return self._send(app.logo_path.read_bytes(), mimetypes.guess_type(str(app.logo_path))[0] or "application/octet-stream")
                if m := re.match(r"^/t/([^/]+)/sql/([^/]+)$", path):
                    if m[1] not in app.cfg.targets:
                        return self._error(404, "unknown target", False)
                    return self._send(app.sql_page(m[1], m[2]))
                if m := re.match(r"^/t/([^/]+)/compare$", path):
                    if m[1] not in app.cfg.targets:
                        return self._error(404, "unknown target", False)
                    return self._send(app.compare_page(m[1], q.get("a", [None])[0], q.get("b", [None])[0]))
                if path == "/search":
                    return self._send(app.search(q.get("q", [""])[0]))
                if path.startswith("/t/"):
                    return self._send(app.target(path[3:], int(q.get("offset", ["0"])[0]), q.get("metric", [None])[0], q.get("filter", ["all"])[0]))
                if path == "/settings":
                    return self._send(app.settings_page())
                if path == "/targets/new":
                    return self._send(app.target_form(None))
                if m := re.match(r"^/targets/([^/]+)$", path):
                    if m[1] not in app.cfg.targets and m[1] not in app.settings.data["targets"]:
                        return self._error(404, "unknown target", False)
                    return self._send(app.target_form(m[1]))
                if path == "/api/settings":
                    return self._json(app.api_settings())
                if path == "/api/settings/agent/models":
                    if not self._cap("edit", True):
                        return
                    ids, note, prov = app.agent_models({k: q[k][0] for k in AGENT_FIELDS if q.get(k, [""])[0]})
                    return self._json({"provider": prov, "models": ids, "note": note})
                if path == "/api/schedule":
                    return self._json({"scheduler": bool(app.scheduler), "targets": app.schedule_rows()})
                if path.startswith("/s/"):
                    return self._send(app.snapshot(path[3:]))
                if path.startswith("/f/"):
                    sid, _, name = path[3:].rpartition("/")
                    return self._file(sid, name)
                if path == "/api/targets":
                    return self._json(app.target_rows())
                if m := re.match(r"^/api/targets/([^/]+)/snapshots$", path):
                    if m[1] not in app.cfg.targets:
                        return self._error(404, "unknown target", True)
                    limit, offset = int(q.get("limit", [app.web["page_size"]])[0]), int(q.get("offset", ["0"])[0])
                    rows, total = app.snapshot_rows(m[1], min(limit, 500), offset)
                    return self._json({"target": m[1], "total": total, "offset": offset, "snapshots": rows})
                if m := re.match(r"^/api/targets/([^/]+)/trend$", path):
                    if m[1] not in app.cfg.targets:
                        return self._error(404, "unknown target", True)
                    pts, used = app.trend_points(m[1], q.get("metric", [None])[0], min(int(q.get("limit", ["200"])[0]), 1000))
                    return self._json({"target": m[1], "metric": used, "metrics": app.metrics_of(m[1]), "points": pts})
                if m := re.match(r"^/api/targets/([^/]+)/sql/([^/]+)$", path):
                    if m[1] not in app.cfg.targets:
                        return self._error(404, "unknown target", True)
                    return self._json({"target": m[1], "sql_id": m[2], "history": app.sql_history(m[1], m[2])})
                if m := re.match(r"^/api/snapshots/(.+?)/files/([^/]+)$", path):
                    return self._file(m[1], m[2])
                if m := re.match(r"^/api/snapshots/(.+?)/chat/([^/]+)$", path):
                    if not chat.KEY_RE.match(m[2]):
                        return self._error(400, "bad thread key", True)
                    return self._json(app.chat_thread(m[1], m[2]))
                if m := re.match(r"^/api/snapshots/(.+)$", path):
                    return self._json(app.snapshot_detail(m[1]))
            except FileNotFoundError:
                return self._error(404, "not found", api)
            except StorageUnavailable:
                return self._error(503, "the snapshot store is unreachable and this page needs data only it holds; try again shortly", api)
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to the browser
                print(f"web: error serving {path}:\n{traceback.format_exc()}", file=sys.stderr)
                return self._error(500, f"internal error: {type(exc).__name__}", api)
            self._error(404, "not found", api)

        def _file(self, sid: str, name: str):
            if "/" in name or name.startswith(".") or not (name in app.web["files"] or "*" in app.web["files"] or name in app.manifest_of(sid).get("reports", [])):
                return self._error(404, "file not exposed", self.path.startswith("/api/"))
            data = app.store.get(f"{sid}/{name}")
            self._send(data, "application/json" if name.endswith(".json") else "text/plain; charset=utf-8")

        def do_POST(self):  # noqa: N802
            if envfile.refresh():
                app.reload()
            self._enter()
            if not self._host_ok():
                return self._refuse("unexpected Host header")
            if not self._same_origin():
                return self._refuse("cross-site request refused")
            if not self._authed():
                return self._send(b"auth required", "text/plain", 401, {"WWW-Authenticate": 'Basic realm="dbtriage"'})
            path = unquote(urlparse(self.path).path)
            if not app.allows("view"):
                return self._error(403, "your group cannot view this", path.startswith("/api/"))
            try:
                if path == "/api/run":
                    body = json.loads(self._read_body() or b"{}")
                    code, out = app.start_run(str(body.get("target", "")), str(body.get("mode", "adhoc")), str(body.get("reason", "")),
                                              not body.get("no_llm", False), body.get("baseline_policy"), body.get("baseline"))
                    return self._json(out, code)
                if path == "/api/settings/agent/test":
                    if not self._cap("edit", True):  # it can point the provider key at another endpoint: an edit in all but name
                        return
                    ok, msg = app.test_agent(json.loads(self._read_body() or b"{}"))
                    return self._json({"ok": ok, "message": msg})
                if m := re.match(r"^/api/snapshots/(.+?)/chat/([^/]+)$", path):
                    if not chat.KEY_RE.match(m[2]):
                        return self._error(400, "bad thread key", True)
                    body = json.loads(self._read_body() or b"{}")
                    try:
                        code, out = app.chat_ask(m[1], m[2], str(body.get("question", "")))
                    except FileNotFoundError as exc:
                        return self._error(404, str(exc) or "not found", True)
                    return self._json(out, code)
                if m := re.match(r"^/api/settings/delivery/([^/]+)/test$", path):
                    if not self._cap("edit", True):  # it posts to real channels and can open tickets
                        return
                    ok, msg = app.test_delivery(m[1])
                    return self._json({"ok": ok, "message": msg})
                if path == "/api/settings" or re.match(r"^/api/targets/[^/]+$", path):
                    return self._api_edit(path)
                if m := re.match(r"^/api/schedule/([^/]+)/run$", path):
                    r = app.schedule_rows()
                    win = next((x["window"] for x in r if x["target"] == m[1]), 60)
                    code, out = app.start_run(m[1], "scheduled", "run now from the schedule page", True, window_minutes=win)
                    return self._json(out, code)
                if m := re.match(r"^/api/schedule/([^/]+)$", path):
                    if not self._cap("edit", True):
                        return
                    body = json.loads(self._read_body() or b"{}")
                    problems = app.save_schedule(m[1], {k: body.get(k) for k in SCHEDULE_FIELDS})
                    if problems:
                        return self._json({"error": "; ".join(problems)}, 400)
                    access.audit("settings", f"schedule {m[1]}")
                    return self._json(next(x for x in app.schedule_rows() if x["target"] == m[1]))
                if m := re.match(r"^/api/targets/([^/]+)/probe$", path):
                    if m[1] not in app.cfg.targets:
                        return self._error(404, "unknown target", True)
                    ok, msg = app.probe_target(m[1])
                    return self._json({"ok": ok, "message": msg})
                if path == "/schedule/save":
                    form = parse_qs(self._read_body().decode(), keep_blank_values=True)
                    g = lambda k: form.get(k, [""])[0].strip()  # noqa: E731
                    name, action = g("name"), g("action")
                    if action == "run":
                        win = next((x["window"] for x in app.schedule_rows() if x["target"] == name), 60)
                        code, out = app.start_run(name, "scheduled", "run now from the schedule page", True, window_minutes=win)
                        return self._send(app.settings_page(f"{name}: {'run started' if code == 202 else out['error']}", "good" if code == 202 else "bad"), code=200 if code == 202 else code)
                    if not self._cap("edit", False):
                        return
                    cur = next((x for x in app.schedule_rows() if x["target"] == name), None)
                    sched = g("schedule_custom") if g("schedule") == "custom" else g("schedule")
                    paused = (cur["paused"] if cur else False)
                    paused = True if action == "pause" else False if action == "resume" else paused
                    problems = app.save_schedule(name, {"schedule": sched, "schedule_window": g("schedule_window"), "schedule_paused": paused})
                    if problems:
                        return self._send(app.settings_page("; ".join(problems), "bad"), code=400)
                    access.audit("settings", f"schedule {name}")
                    return self._send(app.settings_page(f"{name}: {scheduler.describe(sched)}{' (paused)' if paused and sched else ''}", "good"))
                if path.startswith("/settings/") or path.startswith("/targets/"):
                    if not self._cap("edit", False):
                        return
                    form = parse_qs(self._read_body().decode(), keep_blank_values=True)
                    return self._edit_form(path, form)
                if path == "/run":
                    form = parse_qs(self._read_body().decode())
                    code, out = app.start_run(form.get("target", [""])[0], form.get("mode", ["adhoc"])[0], form.get("reason", [""])[0], "no_llm" not in form,
                                              form.get("baseline_policy", [""])[0], form.get("baseline", [""])[0])
                    if code == 202:
                        return self._send(b"", code=303, extra={"Location": f"/t/{out['target']}"})
                    return self._error(code, out["error"], False)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._error(400, str(exc), path.startswith("/api/"))
            except SettingsUnavailable as exc:
                app.reload()
                if path.startswith("/api/"):
                    return self._json({"error": str(exc)}, 503)
                return self._send(app.settings_page(str(exc), "bad"), code=503)
            except StorageUnavailable:
                return self._error(503, "the snapshot store is unreachable; try again shortly", path.startswith("/api/"))
            self._error(404, "not found", path.startswith("/api/"))

        def _edit_form(self, path: str, form: dict):
            if path == "/settings/save":
                # only the fields the form actually has: a missing one is "not given", never "cleared"
                app.settings.put_web({k: form[k][0].strip() for k in WEB_FIELDS if k in form}, base={**DEFAULTS, **app.seed.web})
                app.reload()
                access.audit("settings", path)
                return self._send(app.settings_page("saved", "good"))
            if path == "/settings/agent":
                fields = {k: form[k][0].strip() for k in AGENT_FIELDS if k in form}  # the card has no max_tokens input
                if form.get("action", [""])[0] == "test":
                    ok, msg = app.test_agent(fields)
                    return self._send(app.settings_page(f"model {'ok: ' if ok else 'failed: '}{msg}", "good" if ok else "bad"))
                problems = app.settings.put_agent(fields, base=app.seed.agent)
                if problems:
                    return self._send(app.settings_page("; ".join(problems), "bad"), code=400)
                app.reload()
                access.audit("settings", path)
                return self._send(app.settings_page("agent settings saved", "good"))
            if path == "/settings/delivery":
                action = form.get("action", [""])[0]
                problems = app.settings.put_delivery(app.parse_delivery_form(form), base=app.seed.delivery)
                if problems:
                    return self._send(app.settings_page("; ".join(problems), "bad"), code=400)
                app.reload()
                access.audit("settings", path)
                if action.startswith("test:"):
                    ok, msg = app.test_delivery(action[5:])
                    return self._send(app.settings_page(f"delivery {'ok: ' if ok else 'failed: '}{msg}", "good" if ok else "bad"))
                return self._send(app.settings_page("delivery settings saved", "good"))
            if path == "/targets/save":
                name, fields = app.parse_target_form(form)
                if form.get("action", [""])[0] == "probe":
                    ok, msg = app.probe_target(name, fields)
                    return self._send(app.target_form(name if name in app.cfg.targets or name in app.settings.data["targets"] else None,
                                                      f"connection {'ok: ' if ok else 'failed: '}{msg}", "good" if ok else "bad", values={**fields, "name": name}))
                problems = app.save_target(name, fields)
                if problems:
                    return self._send(app.target_form(name if name in app.cfg.targets else None, "; ".join(problems), "bad", values={**fields, "name": name}), code=400)
                access.audit("settings", f"target {name}")
                return self._send(b"", code=303, extra={"Location": "/settings"})
            if m := re.match(r"^/targets/([^/]+)/delete$", path):
                app.settings.delete_target(m[1], seeded=m[1] in app.seed.targets)
                app.reload()
                access.audit("settings", f"delete target {m[1]}")
                return self._send(b"", code=303, extra={"Location": "/settings"})
            return self._error(404, "not found", False)

        def _api_edit(self, path: str):
            if not self._cap("edit", True):
                return
            body = json.loads(self._read_body() or b"{}")
            if path == "/api/settings":
                if "collect" in body or "allow_db_writes" in body:  # the owner of targets.toml decides whether dbtriage may write
                    return self._json({"error": "allow_db_writes can only be set in targets.toml ([collect]); the web view cannot let dbtriage write"}, 400)
                if "mcp" in body:  # ... and whether MCP clients may read it
                    return self._json({"error": "[mcp] can only be set in targets.toml; the web view cannot turn the MCP server on"}, 400)
                sections = body if any(k in body for k in ("web", "agent", "delivery")) else {"web": body}
                problems = []
                if "web" in sections:
                    app.settings.put_web({k: str(v) for k, v in sections["web"].items() if k in WEB_FIELDS}, base={**DEFAULTS, **app.seed.web})
                if "agent" in sections:
                    problems += app.settings.put_agent(dict(sections["agent"]), base=app.seed.agent)
                if "delivery" in sections:
                    problems += app.settings.put_delivery(dict(sections["delivery"]), base=app.seed.delivery)
                app.reload()
                if problems:
                    return self._json({"error": "; ".join(problems)}, 400)
                access.audit("settings", path)
                return self._json(app.api_settings())
            name = path.rsplit("/", 1)[1]
            if self.command == "DELETE":
                if name not in app.cfg.targets:
                    return self._error(404, "unknown target", True)
                app.settings.delete_target(name, seeded=name in app.seed.targets)
                app.reload()
                access.audit("settings", f"delete target {name}")
                return self._json({"deleted": name})
            problems = app.save_target(name, dict(body))
            if problems:
                return self._json({"error": "; ".join(problems)}, 400)
            access.audit("settings", f"target {name}")
            return self._json(app.api_settings()["targets"][name])

        def do_PUT(self):  # noqa: N802
            self.do_POST()

        def do_DELETE(self):  # noqa: N802
            self.do_POST()

        def log_message(self, fmt, *args):
            print(f"web: {self.address_string()} {fmt % args}")

    if not loopback and not token and (allow_edit or allow_run or allow_chat):
        print(f"web: WARNING listening on {host} with no DBTRIAGE_WEB_TOKEN while editing, runs or chat are on: anyone who can reach "
              "this port can change settings, start runs and spend model credit. Set a token or put it behind your single sign-on proxy.",
              file=sys.stderr)
    print(f"dbtriage web on http://{host}:{port}/  ({'runs enabled' if allow_run else 'no runs'}, {'editing enabled' if allow_edit else 'read-only settings'}, "
          f"{'discussion enabled' if allow_chat else 'no discussion'}, {'scheduler on' if app.scheduler else 'no scheduler'}, "
          f"{'basic auth' if token else 'no auth'}, brand {app.web['brand']!r}, settings {app.settings.where}, store {describe_store(store)})")
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    srv.app = app
    return srv
