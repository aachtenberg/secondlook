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

## Branding

On the Settings page, or in `[web]`: `brand`, `title`, `logo` (a file or a URL), `accent`, `theme` (dark,
light or auto, with a toggle in the header), `footer`, `refresh_seconds` and `page_size`. Only in `[web]`:
`severity_colors`, `custom_css`, which adds your own styles, and `templates_dir`, which replaces page templates
file by file. Start a replacement from a copy of the built-in one in `src/dbtriage/templates/`; `base.html` in
particular carries the page scripts. `examples/branding/` has a logo and a theme. Per-database `links` (say, a Grafana dashboard) show up next to each database.

## Who can see it

The web view listens on `127.0.0.1` unless you pass `--host`, so only this machine can reach it. While it does, it
also refuses requests whose `Host` is not a loopback name, so a web page re-pointed at your machine (DNS
rebinding) cannot use it.

Reading is open to whoever can reach it. Each kind of change is off until you start it with its flag:
`--allow-run` for runs, `--allow-edit` for settings, targets, schedules and the test buttons, `--allow-chat` for
discussions. A few things can never be changed from the web view at all: `api_key_cmd` and `allow_db_writes`
live in `targets.toml` only.

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
