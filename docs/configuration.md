# Configuration

Everything starts from one file, `targets.toml`. Anything you change in the web view's Settings page is saved
on top of it, in a separate settings overlay, so the file can stay in version control. The overlay holds only
what you changed: a setting you leave as the file has it keeps following the file, and one you change or clear
wins over it.

Rather than starting from scratch, copy a [recipe](../recipes/README.md). Every option is explained in
[recipes/reference.toml](../recipes/reference.toml), and every environment variable in
[recipes/reference.env](../recipes/reference.env). How a laptop paste differs from a cloud role, and how the
corporate proxy and CA bundle apply, is in [models](models.md#enterprise-identity).

## A minimal `targets.toml`

```toml
[storage]
url = "file://./data"                # where snapshots live (see storage.md)

[agent]
provider = "anthropic"               # which AI model writes the review (see models.md)
model = "claude-opus-5-5"

[delivery]
channels = ["stdout", "ntfy"]        # where reviews go (see operations.md)
min_severity = "low"                 # per finding: a channel fires when a finding is new or worse at or above it
gone_after_runs = 3                  # a finding absent this many of its own periods (or runs, before it has one) is gone

[targets.prod-db-3]
engine = "oracle"                    # postgres | oracle | mysql | sqlserver
dsn_env = "PROD_DB_3_DSN"            # read the connection string from this environment variable
diagnostics_pack = true              # Oracle only: you hold the licence for AWR/ASH/ADDM
create_snapshot = "off"              # snapshot on the database first: off | incident | always (opt-in, below)
changes_file = "changes/prod-db-3.json"   # optional list of recent deploys; or a [targets.prod-db-3.changes] feed (below)
baseline_policy = "same-time-last-week"   # what to compare against (below)
schedule = "0 * * * *"               # optional: run hourly (see web.md, Scheduling)
```

A database's name (`prod-db-3` above) is part of every stored snapshot's path, so it is kept simple: letters,
digits, `.`, `_` and `-`, up to 64 characters, starting with a letter or digit and not ending in `.`, and not a
Windows device name such as `CON` or `NUL`. Two names that differ only in case would share one folder on some
stores, so they are refused too. The same rule applies to databases added on the Settings page.

`gone_after_runs` is the one threshold behind a finding's life (operations.md, "A finding's life"): how many of its
own periods a finding must be missing before the review says it is gone and its ticket is told it cleared, or how
many runs while it has no period yet. It sits under `[delivery]` because flapping is a channel problem, and on the
Settings page's Delivery card for the same reason. Blank means 3.

## Connecting to a database

Each database needs a **read-only monitoring user**. The lab's grants are in `compose/*/01-init.sql` and are a
good starting point.

| Engine | Connection string |
|---|---|
| PostgreSQL | `postgresql://user:pw@host:5432/db` |
| MySQL | `mysql://user:pw@host:3306/db` |
| MariaDB | `mysql://user:pw@host:3306/db`, with `engine = "mysql"` |
| Oracle | `user/pw@host:1521/SERVICE` |
| SQL Server | `mssql://user:pw@host:1433/db`, or a connection string such as `Server=tcp:host,1433;Database=db;Authentication=ActiveDirectoryMsi;Encrypt=yes` |

**SQL Server** (2016 and later, on Windows or Linux, and Azure SQL Managed Instance; spills from 2016 SP2 and 2017
CU3) uses Microsoft's
`mssql-python` driver, an optional extra: `uv sync --extra sqlserver`. It needs no separate ODBC install. dbtriage
reads the plan cache (`sys.dm_exec_query_stats` for the database it connects to, one row per query hash and plan, so
a new plan shows as a plan change),
wait statistics (idle and background waits left out), active and blocked requests, performance counters and the
server configuration. These counters are cumulative, like Postgres and MySQL, so two snapshots are compared as an
interval. A restart of the instance starts them over, and a statement's own counters start over when its plan leaves the
cache and comes back, which the comparison sees too.

- The login needs `VIEW SERVER PERFORMANCE STATE` on SQL Server 2022 (`VIEW SERVER STATE` on earlier versions) and,
  in the database it connects to, `VIEW DATABASE STATE` and `VIEW DEFINITION`. The last only lets it see table and
  index names, which SQL Server otherwise hides from a login with no permission on them; it cannot read a row.
  `compose/sqlserver/01-init.sql` creates such a login, with the narrow grant where the version has it.
- SQL Server has no read-only session mode, so collection is kept read-only by those grants alone. It runs in
  autocommit, so it never holds a transaction open while it reads the plan cache.
- `encrypt` defaults to yes and the server certificate is verified; `encrypt=strict` uses TDS 8.0. For a lab with a
  self-signed certificate add `?trust_server_certificate=yes`; in production, give the server a certificate your
  clients trust.
- For Windows or Entra ID logins, give a connection string with `Authentication=...` instead of a URL.
- The lab runs SQL Server 2022 Developer edition on k3s (`compose/sqlserver/k8s.yaml`), tested through
  `kubectl port-forward`.

**MariaDB** is read by the MySQL collector: use `engine = "mysql"`. Turn on `performance_schema`
(`performance_schema = ON` in its config; it is off by default in MariaDB), or there are no statement or wait figures to
read. The lab's MariaDB 11.4 (`docker compose up -d mariadb`, port 3307) is tested the same way as MySQL 8.4.

Prefer `dsn_env`, the name of an environment variable filled from your secret store or the `.env` file. dbtriage
then never writes the connection string into its own files. A string typed into the web view is kept in the
settings file with owner-only permissions and shown masked afterwards.

If a `dsn_env` variable is not set, only that database is affected: dbtriage says so when it starts, the others
keep working, and a run for that database stops with a clear message until the variable is set.

Relative paths in `targets.toml` (a `file://./data` store, `changes_file`, a change feed's `path`, `logo`,
`custom_css`, `templates_dir`) are relative to `targets.toml` itself, not to the directory you start dbtriage from.

## The change feed

A regression is easier to read next to the change that sits beside it. Every snapshot already diffs the database's
own settings; a `changes` table on a database adds deploys. The feed is read when the snapshot is taken, for the
window plus `lookback_hours`, and stored with it as `changes.json`, so the review and every tool read only the
store and a review written later sees exactly what was known at capture time. A deploy inside the window, or up
to `correlate_minutes` before it, is written into the evidence of every regressed or new statement, plan change
and wait shift in the comparison, next to the settings that changed, and the rules review says it in one line:
"deploy billing@1.4.2 to production landed 6 minutes before the window".

The feed is set up per database on its edit page in the web view (the **Change feed** fields show only the chosen
source's; **Test feed** reads the last 48 hours as typed and shows the items, or the error, without saving), or in
the file:

```toml
[targets.prod-db-3.changes]
source = "github"                    # file | github | gitlab
repo = "acme/billing"                # github: owner/name
# project = "acme/billing"           # gitlab: the project path or numeric id
environment = "production"           # optional: only this environment's deployments
token_env = "GITHUB_TOKEN"           # the forge token's environment variable; blank reads anonymously
# base_url = "https://gitlab.example.com"   # GitHub Enterprise's API host, or your GitLab instance
# releases = true                    # github: read releases when the repository records no deployments
lookback_hours = 48                  # how far before the window the feed is read
correlate_minutes = 30               # a change this long before the window still sits beside a regression
```

`source = "file"` with `path = "changes/prod-db-3.json"` reads a JSON list you keep yourself, each item
`{"time", "kind", "description", "link"}` with an ISO time; `changes_file = "..."` on the database is the short
form of that. Kinds are `deploy`, `parameter`, `job`, `failover` and `other`; a few common spellings
(`release`, `maintenance`, `config`, `switchover`) are mapped onto them. Only the first four are evidence: `other`
holds notes and the one line a feed leaves when it could not be read, so an unreachable forge never fails a
capture and the review can say the feed was down. The feed is read through the same proxy and trust store as every
other outbound call (`HTTPS_PROXY`, `AWS_CA_BUNDLE`), with a ten-second timeout.

**Telling it about a deploy yourself.** A deploy tool that is not GitHub or GitLab (Jenkins, Octopus, Ansible, a
shell script) posts the deploy to the webhook receiver, `dbtriage serve`, on `POST /changes`. The body is one
object or a list of them, `{"target", "time", "kind", "description", "link"}`: `target` is the database's name in
`targets.toml` (an unknown one is a 404), `description` is required, `time` is ISO 8601 and defaults to now
(UTC when no offset is given), `kind` is one of the five above and defaults to `deploy`, `link` is optional.
The receiver's bearer token, `DBTRIAGE_WEBHOOK_TOKEN`, is the same one alerts use. It answers 202 with what it
recorded. From a CI job, after the deploy step:

```sh
curl -sS -X POST "https://secondlook.example.com:8080/changes" \
  -H "Authorization: Bearer $DBTRIAGE_WEBHOOK_TOKEN" -H "Content-Type: application/json" \
  -d "{\"target\": \"prod-db-3\", \"kind\": \"deploy\", \"description\": \"billing@$CI_COMMIT_TAG to production\", \"link\": \"$CI_JOB_URL\"}"
```

Posted items land in the database's inbox in the store (`<engine>/<db>/changes-inbox.json`) with `source =
"webhook"`, and every snapshot reads that inbox beside the configured source, so a database on GitHub still sees
what another tool deployed, and one without a `changes` table has a feed as soon as something is posted.
`lookback_hours` governs the inbox too: it is what a capture reads before its window, so an item older than the
lookback plus a day can reach no feed and is dropped on the next post; the inbox keeps at most a few hundred
items, the newest. Two pipelines posting at the same moment both land (the write is a compare-and-set, like the
ledger's). While the store is unreachable the post is spooled, and the store merges inbox copies item by item on
replay rather than choosing between them, so a deploy posted during an outage still reaches the feed.

Cloud or on-premises makes no difference: dbtriage needs a network path and a credential. RDS, Cloud SQL and
Azure Database for PostgreSQL or MySQL work as they are. For Oracle on RDS, set `diagnostics_pack` to match the
licence.

**Oracle licensing.** With `diagnostics_pack = false`, dbtriage reads only the `V$` views and never queries
`DBA_HIST*` or AWR. Set it to `true` only when the Diagnostics Pack is licensed. As a second guard, the database's
own `control_management_pack_access` must include DIAGNOSTIC too; if it does not, dbtriage uses the `V$` views and
notes why in the snapshot. `create_snapshot` only applies with the pack on.

## Letting dbtriage take snapshots on the database

dbtriage only reads, and that is the default. Some teams want it to take the database's own snapshot before it
collects. On Oracle that is an AWR snapshot, so the review's window ends now rather than at the last hourly
snapshot. Two switches turn this on, and both are needed:

```toml
[collect]
allow_db_writes = true        # the owner of targets.toml says dbtriage may write at all

[targets.prod-db-3]
create_snapshot = "always"    # this database: "incident" for incident runs only, "always" for every run
```

`allow_db_writes` can only be set in `targets.toml`. The Settings page shows whether it is on but cannot change it,
and the settings API refuses it. `create_snapshot` can be set per database on its edit page, as can the Oracle and
SQL Server limits (`call_timeout_ms`, `connect_timeout_s`, `min_snapshot_age_min`, `mask_report_literals`; see
`recipes/reference.toml`) under **Engine limits**. While writes are off, a database set to take snapshots takes none,
and each of its snapshots says why.

- **Oracle** (built in) needs `diagnostics_pack = true` and `EXECUTE` on `DBMS_WORKLOAD_REPOSITORY`, which reading
  the AWR report already needs. It skips the snapshot when the newest one is younger than `min_snapshot_age_min`
  (default 5). The snapshot is taken on a separate session, and collection itself only reads.
- **Other engines**, or another method such as Statspack or pg_profile's `take_sample()`, plug in as a package
  entry point in the `dbtriage.db_snapshot` group, named after the engine. A plugin replaces the built-in one for
  that engine:

  ```toml
  [project.entry-points."dbtriage.db_snapshot"]
  postgres = "my_pkg.snap:take"   # def take(target, window) -> str | None
  ```

  The function opens its own connection, takes the snapshot and returns what it did, or `None` when none was
  needed. If it raises, the run carries on without it and the snapshot records the error.

Every snapshot records what happened (`db_snapshot` in its manifest: taken, not needed, skipped or failed), and
the snapshot page shows it next to the run mode.

## Picking the baseline

A review compares a snapshot with a baseline: the "normal moment". You choose how that baseline is picked, per
database (`baseline_policy`, or on the database's edit page, where a pinned snapshot is picked from the stored
ones) or per run (`--baseline-policy`, the Capture now dialog, or the API).

| Policy | Compares with | Good for |
|---|---|---|
| `previous` (default) | the snapshot just before | catching sudden change |
| `same-time-yesterday` | the snapshot nearest the same clock time a day earlier, within 2 hours | daily cycles |
| `same-time-last-week` | the same, a week earlier | weekly batch jobs; the fairest comparison for busy Mondays |
| `pinned` | one fixed snapshot (`baseline = "<id>"`) | a known-good run, until someone re-pins it |

A specific snapshot id given with `--baseline` always wins. If a policy finds nothing (no run last week, the
pinned snapshot was deleted), dbtriage falls back to `previous` and says so: on the snapshot page, in the
review's footer, and in the stored `manifest.json` and `compare.json`.

### The usual band

One baseline is one moment, and that moment can itself be odd: last Monday's batch did not run, or ran twice.
Once a database has a few weeks of snapshots, each review also compares its interval with the spread of the same
clock hour and weekday over the past `band_weeks` weeks (default 6; `0` switches it off): the median and a high
mark (the 90th percentile from five weeks on, the maximum below) per load-profile metric, for total SQL time and
for each wait class's share. The result is one sentence in the evidence of the findings about those metrics, such
as "commits 12/s is inside the usual Monday 02:00 band (6 weeks)", "above every same-hour window in 6 weeks" or
"below the usual band", and a line under the review's summary for the interval as a whole. The baseline you chose
stays the comparison the review is written against; the band only says whether this hour is unusual for this
database.

The band is honest about what it is built from. For PostgreSQL, MySQL and MariaDB a week counts only when both
snapshots around that hour exist; for Oracle AWR one snapshot per week within an hour of the same time. A week with
a counter reset or an instance restart inside its window is skipped, the sentence says how many weeks remain
("4 of 6 weeks"), and below three usable weeks no band is written at all. A window much longer than the band's
(a week-long interval under `same-time-last-week`) is not compared with hour-long ones either; `compare.json`
notes why under `band`. It reads only the stored `summary.json` files: nothing new is collected or stored.

## How the comparison works

Every engine's snapshot is turned into the same shape: load profile, top SQL (with the plan hash where the
engine has one), wait events, sessions, tables, settings, and Oracle's ADDM findings.

![Counters that only grow: three snapshots give a reference period (the snapshot before the baseline to the baseline) and a current period (the baseline to this snapshot), compared as rates per second. Oracle AWR: the baseline's AWR period and this snapshot's are compared directly.](img/comparison-periods.svg)

- Postgres, MySQL and SQL Server counters keep growing (and so do Oracle's `V$` counters when AWR is not used), so dbtriage works out two periods and compares them: from the
  baseline to now, against the period just before the baseline. That is why the first two runs have nothing to
  compare.
- Oracle's AWR snapshots already describe a period, so they are compared directly. AWR cannot report across an
  instance restart, so a window that spans one covers the time since the restart, and the snapshot says so.
- Everything becomes a rate per second, so a 15-minute window compares fairly with an hour-long one. Counter
  resets after a restart are detected.

Then plain rules look for the usual suspects and rank them: a slower query, a new heavy query, a plan change,
a shift in wait classes, a falling cache-hit rate, deadlocks, blocking, table bloat, stale statistics, growing
sequential scans. The AI model reads these through a tool; with no model, they become the review. The rules
live in `src/dbtriage/compare.py`.
