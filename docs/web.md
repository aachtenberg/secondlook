# Web view

```bash
dbtriage web --port 8081 --allow-run --allow-edit --allow-chat --schedule
```

Each flag switches on one thing, so a read-only dashboard is just `dbtriage web`: without `--allow-edit` nothing
can be changed, and the model and delivery test buttons are off too, since they call out or post to real channels.

| Flag | Adds |
|---|---|
| `--allow-run` | the **Capture now** button (mode, baseline and reason in a small dialog); one run per database at a time |
| `--allow-edit` | editing on the Settings page: databases, schedules, the model, delivery, the look |
| `--allow-chat` | the **Discuss** panel on snapshot pages |
| `--schedule` | runs the scheduler inside the web server (needs `--allow-run`) |

It uses only the Python standard library and loads no outside scripts or fonts, so it runs on closed networks.

## Pages

- **Overview.** Every database with its latest result, and how many are healthy or in trouble.
- **Database.** A trend line for a chosen metric, headline numbers, and its snapshots filtered by All,
  Incidents or Scheduled.
- **Snapshot.** The review with its findings, wait-time and load charts, the top SQL (click a column header to
  sort by it), links to every stored file (opened in a side panel), and the discussion panel.
- **Compare.** Any two snapshots of a database side by side, with the parameters that changed between them in
  their own table (each one is also a low `setting_changed` finding, and its name is written into the evidence of
  any SQL regression, plan change or wait shift in the same window).
- **SQL.** One statement's history across snapshots, with plan changes marked.
- **Search.** Database names, SQL ids and snapshot ids.

Saving on the Settings page happens in place: the page stays where it is and the result appears as a toast in the
corner. Changing the look reloads the page once, at the same spot, so the new colours apply.

## The diagnosis card and the Ask dock

A database's page opens with a **Diagnosis** card: five cells written by rules from stored data, with no model call.
Health (the latest review's title and age, and, once a usual band has been stored with the comparison, whether the interval sat inside it), Open
(how many findings, the worst, the oldest, their tickets), Changes (the last deploy or setting change and whether a
finding opened after it), Last 7 days (opened, cleared, net, and rules findings the model left out), Next (the next
scheduled run, paused, or why the last one did not finish). A cell that is a *setup gap*, no change feed or no
schedule, is tinted, counted in the header ("2 setup gaps") and links to where the fix is made. It is instant, the
same for everyone, and still there with the model off. Operators who copied `target.html` into `templates_dir` add
`$diagnosis` and `$ask` to their copy to see it.

**Ask AI** is the model's optional part. A round button bottom right (and "Ask AI about this target" in the card's
header) opens a dock: the discussion panel as a small window on the database's page, on a thread about the database
as a whole rather than one snapshot. Its first message is the rules' synopsis of the five cells, then three starter
questions drawn from the facts (the open finding, a spike in the trend, the missing schedule). The model sees the
latest review, the ledger of open findings with their life and tickets, what cleared lately, and the change feed.
Answers are stored with the latest snapshot like any discussion, footer showing their token cost; "Open the full
discussion" takes the thread to the snapshot page. The dock appears only with `--allow-chat` and a review to talk
about, and the button points at the settings page when no model provider is configured.

## Rules and model on the same review

The rules run on every snapshot, whether or not a model reviews it, and both results are stored in `triage.json`
(the model's `findings`, the rules' `heuristics`). When the model wrote the review, each of its findings carries a
mark: **both** when a rule found the same thing (same kind and subject: the sql id, table, wait class or parameter),
or **model only** when the model added it. Rules findings at medium or above that the model left out are listed
under the review as **Rules found, not in the review**, with their title and severity, so a fluent write-up cannot
hide a `blocking` or `deadlocks` hit; the low ones fold into a count. The same section goes into the delivered
markdown as a few **Agreement** lines, and the count of dropped findings is in `/api/targets/<name>/snapshots` as
`rules_dropped` and in the `dbtriage_rules_dropped` gauge. A rules-only review has nothing to compare with and
says so; reviews stored before the marks existed render as before.

## Branding

On the Settings page, or in `[web]`: `brand`, `title`, `logo` (a file or a URL), `accent`, `theme` (dark,
light or auto, with a toggle in the header), `footer`, `refresh_seconds`, `page_size` and `files`, the stored files
a snapshot page links to (`*` for every one). Only in `[web]`:
`severity_colors`, `custom_css`, which adds your own styles, and `templates_dir`, which replaces page templates
file by file. Start a replacement from a copy of the built-in one in `src/dbtriage/templates/`; `base.html` in
particular carries the page scripts. `examples/branding/` has a logo and a theme. Per-database `links` (say, a Grafana dashboard) show up next to each database.

## What the Settings page can change

Every key `targets.toml` accepts, and where it is in the web view. "targets.toml only" keys are file-only on
purpose; the reason is in the row. Credentials are never typed into the page: every `*_env` field holds the name
of an environment variable, and the value is read from the process at run time.

| Section | Key | In the UI |
|---|---|---|
| `[storage]` | `url` | targets.toml only (or `DBTRIAGE_STORAGE`): where every snapshot lives; moving the store is a deployment, not a setting |
| `[agent]` | `provider`, `model` | Settings › Agent: Provider, Model (the list the provider offers, or a typed id) |
| `[agent]` | `region`, `project_id`, `resource`, `aws_profile`, `base_url` | Settings › Agent: Region, Project (Vertex), Resource (Foundry / Azure OpenAI), AWS profile, Base URL |
| `[agent]` | `api_key_env` | Settings › Agent: API key env var (the variable's name only) |
| `[agent]` | `api_key_cmd` | targets.toml only: it runs a command on the server |
| `[agent]` | `effort`, `max_iterations`, `token_budget` | Settings › Agent: Effort, Max iterations, Token budget per review |
| `[agent]` | `max_tokens` | Settings › Agent: Max tokens per model call |
| `[agent]` | `fallback_models` | Settings › Agent: Fallback models (comma-separated; `none` turns the chain off) |
| `[agent]` | `scheduled_provider`, `scheduled_model`, `scheduled_base_url`, `scheduled_api_key_env` | Settings › Agent › Scheduled runs |
| `[agent.extra]` | any | targets.toml only: free-form request fields passed to the provider unchanged |
| `[delivery]` | `channels` | Settings › Delivery: Enabled channels |
| `[delivery]` | `min_severity` | Settings › Delivery: Below this severity only stdout fires |
| `[delivery]` | `gone_after_runs` | Settings › Delivery: A finding is gone after |
| `[delivery.ntfy]` | `url`, `topic`, `token_env`, `min_severity` | Settings › Delivery › ntfy |
| `[delivery.slack]`, `[delivery.teams]` | `webhook_url`, `webhook_url_env`, `min_severity` | Settings › Delivery › slack, teams (a typed URL is masked afterwards) |
| `[delivery.email]` | `smtp_host`, `smtp_port`, `starttls`, `from`, `to`, `user_env`, `password_env`, `min_severity` | Settings › Delivery › email |
| `[delivery.jira]` | `base_url`, `project_key`, `issue_type`, `user_env`, `token_env`, `min_severity` | Settings › Delivery › jira |
| `[delivery.plane]` | `base_url`, `workspace`, `project_id`, `project_identifier`, `api_key_env`, `min_severity` | Settings › Delivery › plane |
| `[delivery.webhook]` | `url`, `min_severity` | Settings › Delivery › webhook |
| `[delivery.webhook.headers]` | any | targets.toml only: header values name secrets with `$VARIABLES` |
| `[delivery.pushgateway]` | `url`, `job`, `min_severity` | Settings › Delivery › pushgateway |
| `[web]` | `title`, `brand`, `logo`, `accent`, `theme`, `refresh_seconds`, `page_size`, `footer` | Settings › Look and feel |
| `[web]` | `files` | Settings › Look and feel: Files shown on a snapshot page |
| `[web]` | `custom_css`, `templates_dir` | targets.toml only: files on the server |
| `[web]` | `severity_colors` | targets.toml only: a nested table, set once with the brand |
| `[web]` | `settings_file` | targets.toml only (or `DBTRIAGE_SETTINGS`): where the page's own changes are kept |
| `[web]` | port, host | neither: `dbtriage web --port --host` on the command line |
| `[collect]` | `allow_db_writes` | targets.toml only: the file's owner decides whether dbtriage may write to databases; the API refuses it |
| `[mcp]` | `enabled`, `allow_run`, `tools` | targets.toml only: whether another process may read the store and start runs; the API refuses it |
| `[targets.X]` | `engine`, `dsn`, `dsn_env`, `user`, `password` | Settings › Targets › edit: Engine, DSN (masked afterwards), DSN from env var, Oracle user, Oracle password |
| `[targets.X]` | `top_n`, `diagnostics_pack`, `create_snapshot` | edit: Top N SQL, Diagnostics Pack, Snapshot on the database before collecting |
| `[targets.X]` | `baseline_policy`, `baseline` | edit: Baseline policy, Pinned baseline snapshot (picked from the stored snapshots) |
| `[targets.X]` | `changes_file`, `[targets.X.changes]` (`source`, `path`, `repo`, `project`, `environment`, `token_env`, `base_url`, `releases`, `lookback_hours`, `correlate_minutes`) | edit › Change feed, with Test feed; a file feed with default numbers is saved as `changes_file` |
| `[targets.X]` | `links`, `notes` | edit: Links, Notes for the reviewer |
| `[targets.X]` | `schedule`, `schedule_window`, `schedule_paused` | Settings › Schedule card |
| `[targets.X]` | `call_timeout_ms`, `connect_timeout_s`, `min_snapshot_age_min`, `mask_report_literals` | edit › Engine limits |

## Who can see it

The web view listens on `127.0.0.1` unless you pass `--host`, so only this machine can reach it. While it does, it
also refuses requests whose `Host` is not a loopback name, so a web page re-pointed at your machine (DNS
rebinding) cannot use it.

Reading is open to whoever can reach it. Each kind of change is off until you start it with its flag:
`--allow-run` for runs, `--allow-edit` for settings, targets, schedules and the test buttons, `--allow-chat` for
discussions. A few things can never be changed from the web view at all: `api_key_cmd`, `allow_db_writes`,
`[mcp]`, the store and settings locations and the files the page is built from live in `targets.toml` only
(the table above says why for each).

`DBTRIAGE_WEB_TOKEN` turns on a simple password (HTTP Basic). In an enterprise, put the web view behind the
single sign-on proxy you already run and leave the token unset. Listening beyond loopback with runs, editing or
discussions on and no token prints a warning at start, since anyone who can reach the port could use them.

That proxy can also say who clicked. It is off until `DBTRIAGE_TRUST_PROXY` is set, because a browser can send
the headers itself. `DBTRIAGE_PROXY_USER_HEADER` (default `X-Forwarded-User`) and
`DBTRIAGE_PROXY_GROUPS_HEADER` (default `X-Forwarded-Groups`) name the headers the proxy overwrites. Optional
comma-separated lists `DBTRIAGE_GROUP_VIEW`, `DBTRIAGE_GROUP_RUN`, `DBTRIAGE_GROUP_EDIT` and
`DBTRIAGE_GROUP_CHAT` narrow who may read, run, edit and discuss. The process flags stay the ceiling: a group
cannot turn on something the process was started without. `/health` stays open for monitors. With a token, the
Basic username is recorded only after the password matches. This is not the cloud access portal, and it does
not read `~/.aws/credentials`. When a person is known, changes print one `audit:` line. A run they start stores
their name on the snapshot.

`dbtriage mcp`, the [MCP server](mcp.md), is another: off unless `targets.toml` turns it on, and it talks over
stdio only, so it does not listen on the network at all. `dbtriage serve`, the webhook receiver, is a separate process too: it listens on all interfaces (port 8080) and wants
`DBTRIAGE_WEBHOOK_TOKEN` (see [operations](operations.md)).

## Discussing a review

With `--allow-chat`, each snapshot page has a **Discuss this review** panel. It shows what it is looking at,
offers a few starter questions drawn from the snapshot, and warns when no model is configured. Each finding has
its own **Discuss** link and each top SQL row its own ask button, each with a separate thread.

The model gets the review, the item you asked about, and the same read-only tools the reviewer used. So answers
come from the stored evidence, and each one shows the model, tool calls, tokens and time taken. Each answer keeps
to the reviewer's token budget (see [models](models.md#keeping-the-bill-down)). Two limits are deliberate:

- **No live database.** There is no tool for it. When a question needs a live query, the model says what to run.
- **No memory across reviews.** A thread is context for itself only; nothing said in it shapes the next review.

Threads are stored next to the snapshot, so they survive restarts and form part of the record. A thread holds up
to 20 questions, and the last 8 exchanges are sent with each new one. `/s/<id>#chat-finding-2` links straight to
one. From the shell: `dbtriage ask <snapshot> "question" --finding 2`.

## Scheduling

The Settings page has a **Schedule** card. For each database pick a preset (every 15 or 30 minutes, hourly,
every 6 hours, daily at 03:00, weekdays at 08:00) or type a cron line, set how many minutes each run looks back,
and pause, resume or **Run now**. The next and last run are shown beside it. The same can go in `targets.toml`:

```toml
[targets.prod-db-3]
schedule = "0 * * * *"      # cron, in the server's local time; or "every 15m", "every 2h"
schedule_window = 60        # minutes collected per run
# schedule_paused = true
```

Something has to keep time. Either the web server does it (`--schedule`), or `dbtriage scheduler` runs on its
own on a server that can reach the databases. Both pick up changes without a restart. Only one run per database
runs at a time; a slot that comes up while the last run is still going is skipped and shown as such. Slots
missed while nothing was running are not caught up. Cron and the cloud timers in `examples/` still work too.

Scheduled runs can be reviewed by a cheaper or local model than the rest: set it under **Scheduled runs** in the
Agent card, or `scheduled_provider` and `scheduled_model` in `[agent]` ([models](models.md#keeping-the-bill-down)).
Only runs the schedule starts use it; **Run now**, the Run button, webhooks and discussion keep the main model.

## JSON API

The same data as JSON, behind the same password, for your own portal, Grafana or ServiceNow. Requests that change
something are refused when they come from another web site; scripts and `curl` are fine. `/health` answers without
the password, but then only says whether things are reachable.

| Request | Returns |
|---|---|
| `GET /api/targets` | every database with its latest result |
| `GET /api/targets/<name>/snapshots?limit=&offset=` | a database's snapshots |
| `GET /api/targets/<name>/trend?metric=` | one metric across snapshots |
| `GET /api/targets/<name>/sql/<sql_id>` | one statement's history |
| `GET /api/snapshots/<id>` | a snapshot with its review, findings and top SQL |
| `GET /api/snapshots/<id>/files/<name>` | a stored file, such as `awr.txt` |
| `POST /api/run` | start a run (needs `--allow-run`): `{"target", "mode", "reason", "no_llm", "baseline_policy", "baseline"}`; the window is 60 minutes |
| `GET` or `POST /api/snapshots/<id>/chat/<key>` | a discussion thread (posting needs `--allow-chat`); `key` is `review`, `finding-<n>` or `sql-<sql id>`; post `{"question"}` |
| `GET /api/schedule`, `POST /api/schedule/<name>` | read or change schedules (changing needs `--allow-edit`) |
| `POST /api/schedule/<name>/run` | run now with the scheduled window (needs `--allow-run`) |
| `POST /api/targets/<name>/probe` | test the connection to a configured database (read-only; no flag needed) |
| `POST /api/settings/agent/test`, `GET /api/settings/agent/models?provider=` | try the model, list a provider's models (need `--allow-edit`) |
| `POST /api/settings/delivery/<channel>/test` | post the latest review to one channel (needs `--allow-edit`) |
| `GET` or `PUT /api/settings`, `PUT` or `DELETE /api/targets/<name>` | read or change settings (changing needs `--allow-edit`); `GET` also says whether `allow_db_writes` is on, and a `PUT` that tries to set it is refused (it lives in `targets.toml` only); a `PUT` changes only what it sends, down to single channel fields. Saved settings keep only what differs from `targets.toml`, so later edits to the file still apply; an empty value clears a field the file sets. A deleted target comes back only with an engine and a dsn or dsn_env |
| `GET /health` | whether the store and settings are reachable, and how much is waiting to sync; locations only with the password |
