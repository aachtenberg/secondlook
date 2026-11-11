# Running it

## Commands

| Command | What it does |
|---|---|
| `dbtriage run -t <db> [--mode scheduled\|adhoc\|incident] [--window 60] [--reason ...]` | the whole pipeline: collect, compare, review, deliver |
| `dbtriage collect -t <db>` | take a snapshot only |
| `dbtriage compare -t <db> [--snapshot ID] [--baseline ID \| --baseline-policy P]` | print the comparison as JSON |
| `dbtriage triage -t <db> [--snapshot ID] [--no-llm] [--deliver]` | review a stored snapshot again, for example with another model |
| `dbtriage deliver <snapshot> [--channel slack,teams]` | send a stored review again |
| `dbtriage list -t <db>`, `dbtriage show <snapshot> [file]` | browse what is stored (`triage.md`, `compare.json`, `awr.txt` ...) |
| `dbtriage ask <snapshot> "question" [--finding N]` | discuss a review from the shell; leave out the question to print the thread |
| `dbtriage web [--allow-run] [--allow-edit] [--allow-chat] [--schedule]` | the web view (see web.md) |
| `dbtriage scheduler` | run every database's schedule from this server, without the web view |
| `dbtriage mcp` | the MCP server, for MCP clients such as Claude Code; off unless `[mcp] enabled = true` (see mcp.md) |
| `dbtriage serve --port 8080` | receive alert webhooks; listens on all interfaces, so set `DBTRIAGE_WEBHOOK_TOKEN` (without it anyone who can reach the port can start runs, and it warns at start) |
| `dbtriage flush-spool` | push work saved during a store outage now |
| `dbtriage deliver <snapshot> [--force]` | re-send a stored review; `--force` ignores the severity gates and finding life and posts it as it stands |
| `dbtriage rebuild-ledger -t <db>` | recompute the database's ledger of open findings from its stored reviews (below, "A finding's life") |
| `dbtriage copy-store --from URL --to URL`, `dbtriage copy-settings --from X --to Y` | move snapshots or settings, for example from files into Postgres |

`--baseline-policy` and `--baseline` work on `run`, `compare` and `triage` (see configuration.md).

## Keeping the web view running

With `--schedule`, the web view is also what keeps time, so it has to stay up. On a Linux server, run it as a
systemd service. A user service needs no root and runs from your checkout, so after pulling new code you only
restart it:

```ini
# ~/.config/systemd/user/dbtriage-web.service
[Unit]
Description=dbtriage web view with the scheduler
After=network-online.target

[Service]
WorkingDirectory=%h/db-triage
# the checkout's venv (uv sync --all-extras); dbtriage reads the checkout's .env itself
ExecStart=%h/db-triage/.venv/bin/dbtriage web --port 8081 --allow-run --allow-edit --allow-chat --schedule
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now dbtriage-web
loginctl enable-linger              # keep it running while you're logged out, and start it at boot
systemctl --user restart dbtriage-web       # after uv sync with new code
journalctl --user -u dbtriage-web -f        # its log
```

On a laptop, that user service is the one place a credentials file is expected. After the access portal, paste
today's keys into `~/.aws/credentials`. The process notices the new file and does not need a restart. The keys
die in under a day; the scheduler on the laptop stops until the next paste, and the review says so. See
[where the process gets its cloud identity](#where-the-process-gets-its-cloud-identity).

Running it as its own user, or as a system service with `User=`, works the same way.

The repo's `Dockerfile` is the other route. The image has every optional extra (the SQL Server driver, Azure Blob
storage, the MCP server). Mount one folder that holds `targets.toml` and `.env`: the Settings page's file and a
`file://./data` store are kept next to `targets.toml`, so they survive a new container too.

```bash
docker build -t dbtriage .
docker run -d --name dbtriage-web --restart unless-stopped \
  -v /srv/dbtriage:/conf -e DBTRIAGE_CONFIG=/conf/targets.toml \
  -p 127.0.0.1:8081:8081 \
  dbtriage web --host 0.0.0.0 --port 8081 --allow-run --allow-edit --allow-chat --schedule
```

Inside a container the web view has to listen on `0.0.0.0`; publishing the port on `127.0.0.1` keeps it on this
machine. Publish it wider only with `DBTRIAGE_WEB_TOKEN` set or behind your single sign-on proxy
([web view](web.md#who-can-see-it)).

## Where the process gets its cloud identity

Two shapes. They are not mixed.

**Desktop or laptop on the same LAN as the databases.** Browser sign-in at the access portal ends in keys you
paste into `~/.aws/credentials` (a profile such as `[dev]`) or export as `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY` and `AWS_SESSION_TOKEN`. Nothing refreshes them. Set `aws_profile` to that profile.
Outbound HTTPS goes through `HTTPS_PROXY` and trusts `AWS_CA_BUNDLE` (the corporate chain plus the public CAs
for hosts that bypass the proxy).

**A pod or a container.** EKS (IRSA or Pod Identity), Cloud Run, Azure Container Apps, or the same pattern on
k3s with a projected service-account token. The role is attached to the workload. Leave `aws_profile` blank.
Do not mount someone's `~/.aws/credentials` and do not forward their portal session. The scheduler keeps
running on that role. Sketches: `examples/eks.yaml`, `examples/cloudrun.yaml`, `examples/container-apps.yaml`.

Database protocols are not HTTP. Postgres, Oracle, MySQL and SQL Server on the cluster or on other machines on
the LAN are not sent through the proxy. Do not set `ALL_PROXY`. `NO_PROXY` lists what HTTP must still reach
directly: `169.254.169.254`, `.svc`, `.cluster.local`, and the LAN of databases and local model hosts (an
example is in `recipes/reference.env`). That covers a local model, webhooks and the instance metadata service.
The proxy and the bundle still apply to Bedrock and to other HTTPS calls when the network inspects TLS.

## What can start a run

The request is the same everywhere: which database, which mode, how many minutes, and why.

| Trigger | How |
|---|---|
| The built-in scheduler | the Schedule card, or `schedule` in `targets.toml` (see web.md) |
| A button | **Capture now** in the web view |
| cron | `examples/crontab` |
| An alert | `dbtriage serve` takes webhooks: `examples/alertmanager.yml` (Grafana works the same way), `examples/datadog-dynatrace.md` (also PagerDuty) |
| AWS | an EventBridge schedule calling the Lambda handler `dbtriage.triggers.lambda_handler`: `examples/eventbridge-schedule.json` |
| Azure or Google Cloud | `examples/azure-function.md` (the same pattern works for Cloud Run jobs) |
| AWX or Ansible | `examples/ansible-playbook.yml` |

## Where reviews go

Pick channels in `[delivery] channels` or on the Settings page, where each has a **Send test** button:
`stdout`, `ntfy`, `slack`, `teams`, `email`, `jira`, `plane`, `webhook`, `pushgateway`.

- `min_severity` keeps quiet results quiet. Tickets (Jira, Plane) default to high only. Once a database has a
  ledger of open findings (below) the gate is on the finding, not the run: a channel fires when some finding is
  **new** or **worse** at or above its gate, so a still-open medium does not repost every hour. `stdout` always
  prints, and `pushgateway` always records the run, so an alert on "runs stopped" keeps working.
- Jira and Plane remember the ticket they opened. A finding that gets worse is a comment on its ticket, not a new
  issue; a finding that is gone gets a "cleared" comment. Plane keys read `OPS-12` when `project_identifier` is set.
- `pushgateway` exposes `dbtriage_severity{target,engine}` and `dbtriage_last_run_timestamp_seconds`, so Prometheus
  can alert on a bad result or on runs that stopped, and `dbtriage_rules_dropped`, the number of rules findings at
  medium or above the model's review left out (a rising number is a prompt or model problem to look at).
- `webhook` posts the review as JSON to anything else, such as the Datadog or Dynatrace events API.

## Safety rules

- **No secrets in the repo.** A gitleaks scan of the whole history runs on every pull request and every push to
  `main` (`.github/workflows/secrets-scan.yml`), and `main` takes no change that fails it. `.githooks/pre-commit`
  runs the same scan on what you are about to commit: install gitleaks 8.21.2, then run
  `git config core.hooksPath .githooks` once per clone. `.gitleaks.toml` lists the lab's made-up credentials, which
  are public on purpose; a new made-up value that trips the scan goes in its stopwords. Real ones live in `.env`
  (never committed) or your secret store.
- **Read-only unless you opt in.** Each database is reached with a read-only monitoring user, and Postgres and
  MySQL collection sessions are read-only on the server too. The one write dbtriage can make is a snapshot on the
  database before collecting, and only when `targets.toml` sets `allow_db_writes` and the database asks for it
  ([configuration](configuration.md#letting-dbtriage-take-snapshots-on-the-database)). Prefer `dsn_env` so the
  connection string stays in your secret store.
- **Licences respected.** Oracle's `DBA_HIST*` and AWR views are queried only with `diagnostics_pack = true` and
  when the database's `control_management_pack_access` includes DIAGNOSTIC; otherwise the `V$` views are used and
  the snapshot says so.
- **Literals masked.** Values inside SQL text are masked before anything is stored or shown to the model: in the
  summary, in Oracle's raw data, and as quoted values in the AWR and ADDM report text (`mask_report_literals = false`
  keeps the original reports). Postgres and MySQL already report SQL without them. The reviewer can open only the
  snapshot's listed reports.
- **A full record.** Every run keeps what triggered it and why (`manifest.json`), what was compared (`compare.json`),
  what was said (`triage.json`, discussions), and what the model read to say it (`tools.json`: every tool call with its
  arguments and the first 2000 characters of its result, kept even when the model failed to submit a review).
- **People decide.** dbtriage recommends fixes and never applies them. Automatic fixes, if ever added, belong
  behind an approval step with a ready rollback.

## A finding's life

Every run writes a fresh review, but a finding is not fresh just because the run is. Each database keeps a ledger
of its open findings (`<engine>/<database>/findings.json` in the store), and every review says, next to each
finding, what its life is:

| State | Meaning |
|---|---|
| **new** | not in the ledger before this run |
| **still open** | seen again at the same severity; shown with "open since" and its ticket |
| **worse** | seen again at a higher severity than last run; delivery fires, and the ticket gets a comment |
| **quiet** | seen again, but below its own severity last run; nothing is posted |
| **returned** | back after being absent, or after being reported gone within the last seven days; delivery fires, and an existing ticket gets a comment instead of a new issue |
| **recurring** | returned at about the same interval as last time: the nightly batch, the Monday job. Shown with "about every 24h"; nothing is posted unless it is worse |
| **gone** | absent for `gone_after_runs` times its own period, or that many runs when it has no period yet (default 3); said once, its ticket told, then kept as a tombstone for seven days |

Two findings are the same finding when they share a `kind` and a `subject` (the sql_id, table, wait class or
parameter named), whatever the wording. Presence comes from the rules, which run on every snapshot, so the model
choosing different words, or not mentioning a finding this time, does not make it new or gone.

*Gone* is decided by cadence, not by counting runs, because runs are hourly and batch jobs are not. A finding's
period is the median gap between its returns. A statement present in every hourly run has a period of an hour and
is gone after three; a statement that appears only in the 02:00 run has, from its second night, a period of a day,
so three quiet hours do not clear it and its ticket is not told "cleared" every morning. On its first night it has
no period and the run count applies, which is why the tombstone exists: when it comes back the next night it is the
same finding, returned, with its ticket and its first-seen date. For a target with a batch cycle, also set its
baseline policy to `same-time-yesterday` or `same-time-last-week` (configuration.md) so the batch window is compared
with itself and the relative findings do not fire at all.

The review still caps at five findings, and an open finding never takes a slot from a new one: open findings that
did not make the five are listed under **Still open** after them, with how long they have been open and how many
runs since they were last seen. They are gated like the five: one that got worse, or came back, pages even though
the review did not name it, and its ticket gets the comment. A finding that cleared this run is named under
**Cleared**. Reviews stored before findings had a kind are not tracked: a rebuild takes presence from their rules
findings, which always had one.

One run's new findings share one Jira or Plane issue. When one of them clears while another on the same issue is
still open, the comment says so instead of "cleared".

Runs of one database take turns within a process (the web view and the scheduler refuse a second run; `dbtriage
serve` queues it). The ledger file carries a generation: a save goes ahead only when nobody saved in between, and
is redone on the new state if someone did; a copy that lost the race is never written. The outage spool replays
the ledger under the same per-database lock and drops its copy when the store already holds a newer generation,
so coming out of an outage cannot put an older ledger over a fresh run. Two separate processes writing one
database's ledger at the same instant can still race (no store has a conditional put), so run one scheduler per
store; `dbtriage rebuild-ledger` repairs the file from the stored reviews.

The ledger is derived data. It is written through the same store as the snapshots, so an outage spools it like
any other file, and `dbtriage rebuild-ledger -t <db>` recomputes it from the stored `triage.json` and
`compare.json` files, tickets included. Losing it is a rebuild, not a loss. A run that cannot read the store
reviews without a life (and delivers on the run's severity, as before) rather than start a ledger that would
overwrite the real one.

## The review format

Reviews read like a code review, in the style of [agent-review](https://github.com/aachtenberg/agent-review): a
count line, up to five findings most severe first, each naming one thing (a SQL id, a table, a wait class, a
setting) with **Impact**, **Evidence** and **Fix**. When nothing is wrong you get "No significant change" and a
**Checked:** line saying what was looked at. The footer names the model, tool calls, tokens, time taken and the
two snapshots compared.

The model fills in this structure by calling one tool, `submit_review`, and `report.py` turns it into text. So
every review looks the same whichever model, or the rules, wrote it, and the same text goes to every channel.
Each finding also carries a `kind` from the rules' vocabulary (`sql_regressed`, `plan_change`, `wait_shift`, ...)
and a `subject`, the exact sql_id, table, wait class or parameter it names; `findings.py` joins on those two to say
when the model and the rules found the same thing, or when a finding is the same one as in the previous snapshot.

The reviewer's tools are `get_report`, `get_raw_report` (pages through the AWR text), `compare_snapshots`,
`get_sql_history`, `get_recent_changes` and `submit_review`. Reports are never pasted into the prompt; the model
asks for what it needs, so the numbers stay exact.

## Code layout

```
src/dbtriage/
  cli.py          commands and the run pipeline
  config.py       targets.toml
  settings.py     the Settings page's overlay (file or Postgres)
  collectors/     base.py (the shared snapshot shape), postgres.py, oracle.py, mysql.py (MySQL and MariaDB), sqlserver.py
  dbsnap.py       opt-in snapshots on the database (an AWR snapshot), and the entry point for more
  storage.py      disk, S3, Azure Blob and Postgres stores, and the outage spool
  envfile.py      reading .env and noticing edits
  redact.py       masking values in SQL and report text
  pgstore.py      the Postgres connection used by storage and settings
  baseline.py     picking the baseline
  compare.py      periods, differences and the rules
  agent.py        the AI reviewer and its tools, and the rules-only review
  llm/            one module per model provider family
  chat.py         discussing a review
  scheduler.py    schedules
  report.py       turning a review into text
  findings.py     what makes two findings the same finding (kind and subject)
  ledger.py       the per-database ledger of open findings: new, still open, worse, quiet, gone
  deliver.py      the delivery channels, and the finding-level gate
  triggers.py     webhook receiver and Lambda handler
  mcp_server.py   the MCP server (off unless [mcp] enabled = true)
  web.py, charts.py, templates/   the web view
compose/          lab database setup (users, grants, sample data)
examples/         trigger wiring, branding, the Azure and Google Cloud budget caps
scripts/load.py   lab load generator (--regress plants a slow query)
.githooks/        the pre-commit secrets scan
tests/
```

## Testing

```bash
uv sync --all-extras                            # dev tools, and the SQL Server driver and MCP SDK their tests need
uv run pytest                                   # everything that can run here; the rest skips with a reason
uv run pytest -m "not browser and not live"     # unit tests only: seconds, no services
uv run pytest -m live                           # the lab: docker compose up -d first
uv run pytest -m browser                        # the web view in a real Chrome
```

| Tier | What it covers | Needs |
|---|---|---|
| unit | comparison rules, baselines, storage and outage spool, settings, scheduler, chat, model backends with fake clients, every web page and API route | nothing |
| live | the whole pipeline (collect, store, compare, review) against the lab Postgres, MySQL, MariaDB, Oracle and SQL Server; the reviewer's real tool loop over HTTP; the S3 store | the lab containers, the SQL Server lab on k3s (`compose/sqlserver/k8s.yaml`), and the repo's `.env` |
| browser | every page at desktop and phone width (no console errors, no sideways scrolling, every control named, select lists readable in both themes), theme and nav toggles, the file drawer, sorting the top SQL, the discussion panel end to end, in-place saves and toasts, schedule rows, the model picker, Capture now, search, compare, the outage banner | Chrome, or `uv run playwright install chromium` |

Nothing in the suite calls a real AI model or posts anywhere. Live and browser tests talk to a small fake
OpenAI-compatible server started by the tests, which lists models and makes real tool calls. Live tests take
their credentials only from the environment or `.env`. The Postgres store tests also run when
`DBTRIAGE_TEST_PG_URL` names a database; they use a throwaway schema.

## The Oracle lab

Oracle Free (with AWR) is behind a compose profile because the image is 2 GB:
`docker compose --profile oracle up -d --wait oracle`, then the same steps with `-t ora-lab` and
`scripts/load.py oracle --seconds 20 --awr-snapshot`, so each capture covers a fresh AWR interval. Without new
AWR snapshots, captures reuse the newest two, or stop with "AWR has fewer than two snapshots". AWR cannot report
across an instance restart, so only snapshots since the latest startup count: a window that spans a restart covers the
time since it (the snapshot notes that), and right after one, captures wait for two new AWR snapshots.

The lab containers restart with the machine (`restart: unless-stopped`); `docker compose stop` keeps them down until
the next `up`. A reboot, `stop` and `start` keep every database's data. Postgres, MySQL and MariaDB keep theirs in the
images' anonymous volumes, which an `up` that recreates the container (after a change to its service, say) carries
over, but `docker compose down` throws away: the next `up` starts them from the init scripts. Oracle Free keeps its
database inside the container, so a recreate starts it over too, AWR history included.

## What's next

- Query Store for SQL Server: interval statistics for the window, instead of the plan cache, which forgets
  statements when plans are evicted.
- Statspack, for Oracle Standard Edition. Taking Statspack snapshots can already plug in through the
  `dbtriage.db_snapshot` entry point ([configuration](configuration.md#letting-dbtriage-take-snapshots-on-the-database)); reading them is the missing part.
- A weekly trend digest across many snapshots.
