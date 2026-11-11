# Features that set Second Look apart

Second Look is a written review of one database at one moment: collect a read-only snapshot, compare it with a normal moment, write findings (impact, evidence, fix), and deliver them where the team already looks. The model sees only the stored snapshot. With no model, the rules still write the review.

The pack is the other shape of product.

- Dashboards you live in: Datadog Database Monitoring, Dynatrace, New Relic, SolarWinds DPA, Quest Foglight, Redgate SQL Monitor, Percona PMM, pganalyze, Oracle Enterprise Manager.
- Cloud consoles bound to one account: Performance Insights, DevOps Guru, Azure SQL insights.
- Tuners that want the SQL and sometimes change the database: EverSQL, OtterTune, DBtune.
- A chat box pointed at production.

Those products are deeper on continuous charts, one engine, or automatic tuning. They are weak at the thing Second Look already is: a short, evidenced review, in the same words for Oracle, PostgreSQL, MySQL, MariaDB, and SQL Server, that still exists when the model is off and when the network is closed.

New work should make that review more trustworthy and more useful over time. It should not grow a second dashboard.

## Leave these alone

- A metrics platform. High-cardinality query charts are how Datadog and PMM win. Second Look already has a trend line and a compare page. More charts do not change the buying reason.
- Applying a fix. The safety rules already say people decide, and a rollback belongs in the recommendation. Auto-index and knob tuning is OtterTune’s product, and it fights the “second pair of eyes” story.
- A SQL IDE, a trace product, or a live connection from the model. The reviewer-access picture is the enterprise pitch. Do not blur it.

Coverage work still belongs on the list, and it is not the story. Query Store (SQL Server plan cache forgets statements) and Statspack (Oracle Standard Edition) make the same review possible on more databases. Ship them as coverage. Do not market them as the difference.

## What the code already has

The bets below lean on these facts, so they are stated once.

- Every run stores `summary.json`, `raw.json`, `manifest.json`, `compare.json`, `triage.json` and `triage.md` under the snapshot id.
- The rules run on every run, not only when the model fails. `compare.py` produces ranked findings with a stable `kind` (`sql_regressed`, `plan_change`, `wait_shift`, `deadlocks`, `blocking`, …) and structured evidence (sql id, wait class, sessions). The CLI copies them into `triage.json` as `heuristics`, next to the model's findings.
- The model's finding has severity, kind, subject, a free-text `where`, summary, impact, evidence and fix. `findings.py` holds the one matcher (identity, `same_finding`, `match`) that joins a model finding to a rules finding or to the same finding in an earlier snapshot. *(Done: the groundwork below.)*
- Every collector stores a `settings` section, and `compare.py` diffs it: `setting_changed` findings, a `settings_changed` list in `compare.json`, and the changed names attached as evidence to regressions, plan changes and wait shifts in the same window. *(Done: the first half of bet 3.)*
- Every capture reads the target's change feed (a changes file, GitHub or GitLab deployments) for its window plus a lookback and stores it as `changes.json`; `compare.py` writes the deploys in or just before the window into the evidence of regressions, new statements, plan changes and wait shifts, and `get_recent_changes` reads the stored feed. *(Done: bet 3.)*
- Jira and Plane delivery keeps the created issue's key, id and url on the finding's ledger entry, comments on it for worse and cleared findings, and the `min_severity` gate is per finding (new or worse) when the review carries `life`; legacy reviews keep the whole-run gate. *(Done: bet 2.)*
- Storage is per-snapshot prefixes on local disk, S3, Azure or Postgres, with an outage spool. The one cross-snapshot object is the per-target ledger `<engine>/<target>/findings.json`, written through the same interface and rebuildable with `dbtriage rebuild-ledger`. *(Done: bet 2.)*
- The model run stores provider, model, token usage, a tool-call count, and `tools.json`: every tool call with its arguments and a truncated result, written even when the run fails. *(Done: the prerequisite for bet 8.)*
- There is no SQL text hash. `sql_id` is engine-specific and SQL text is redacted before storage.

## The groundwork: a finding with a kind and a subject

Bets 1, 3, 4 and 6 all need the same thing first: a way to say two findings are about the same problem.

Add `kind` and `subject` to the model's finding schema in `submit_review`, with the same kind vocabulary the rules use, and the subject being the thing named (sql id, table, wait class, parameter, session). Keep `where` as display text. Then write one matcher, `same_finding(a, b)`, that compares engine, target, kind and subject, and use it everywhere: model against rules in one run, and run against run across time.

This is a small schema change and one pure function with unit tests. It is the first task, and it is not a bet.

## The bets

Ranked by how sharply they differ, and by how much of the current pipeline they reuse (snapshot store, `compare.py` finding kinds, rules review plus model review, Jira and Plane delivery, MCP).

### 1. Rules and model on the same review *(landed)*

Both paths already run on every run, and both are already stored in `triage.json`. What the reader does not see is agreement.

Run the matcher over the model's findings and the `heuristics` list. The snapshot page shows three marks: **both**, **rules only**, **model only**. A model-only high finding stays, and it is labeled as the model’s addition. A rules finding the model dropped stays, labeled as dropped, so a fluent write-up cannot hide a `blocking` or `deadlocks` hit.

Regulated buyers trust a review they can diff. Almost no AI database tool keeps the deterministic pass beside the prose. Storage cost is zero: the data is already there. The work is the matcher, the marks on the page, and a line in the delivered markdown.

This goes first because it is the cheapest proof that the matcher works, on data that already exists.

### 2. A finding that stays a finding *(landed)*

Today every run writes a fresh review. The same `sql_id` can page the channel every hour. `min_severity` only drops quiet runs. It does not remember yesterday’s high finding.

Using the matcher across snapshots, mark each finding **new**, **still open**, **worse**, **quiet**, or **gone**. Define the last two with thresholds, not judgment: *quiet* is the same identity present but below its own severity in this run; *gone* is decided by cadence, not runs. Each finding keeps the gaps between its returns, and its period is their median. It is gone when absent for N times its period (N configurable, default 3), or N runs while it has no period yet. A gone finding stays as a tombstone for a week, so when it comes back it is **returned**, not new, and keeps its ticket. A return at the same gap as before is **recurring**: the nightly batch, quiet unless worse. Without this, hourly snapshots and a nightly job flap once a day.

The review then says “still the plan change on `d8vkav3yzdfuz`, open since Tuesday, ticket PLANE-12” instead of a new red item. Delivery updates that ticket. A gone finding can close it, or say it cleared. Quiet-unless-worse becomes real: a still-open medium does not repost.

Two design decisions this bet owns:

- **Where the ledger lives.** Open findings need a per-target object (`<target>/findings.json`) that outlives any one snapshot. It must work on every storage backend and survive the outage spool, so it is written through the same `Storage` interface and replayed the same way. It is derived data: it can always be rebuilt from the stored `triage.json` files, so losing it is a rebuild, not a loss.
- **Tickets as state.** Jira and Plane senders must parse the created issue's key from the response and store it on the ledger entry. Later runs update or comment on that issue instead of opening a new one. The severity gate moves from the run to the finding: a run delivers when any finding is new or worse at or above the gate.

One interaction to watch: the review caps at five findings. A still-open finding should not take a slot from a new one. Still-open items get their own short section after the five.

Nobody in the pack writes that life next to a prose review. Dashboards mute alerts. Tuners open a new suggestion. The snapshot history to do this is already stored.

### 3. The change that sits beside the regression *(landed)*

The prompt already tells the model to correlate only when the data supports it. The missing half is a real feed. It has a free first half and an integration second half.

**First: diff the settings** *(landed)*. Every snapshot already stores `settings`. Teach `compare.py` to diff current against baseline and emit a `setting_changed` finding (name, before, after) with low severity on its own, and attach it as evidence to any `sql_regressed`, `plan_change` or `wait_shift` in the same window. No integration, no new config, and the correlation line is true for parameter changes on day one.

**Second: deploys** *(landed)*. Replace the local-file read in `get_recent_changes` with a feed interface, with the file as one implementation, and add GitHub and GitLab deployment sources, stats jobs and failovers. The feed is fetched at capture time and stored with the snapshot, so the review never reads outside the store.

The review line that matters is specific: “plan hash changed in this window; deploy `billing@1.4.2` landed 6 minutes earlier.” AWR shows the plan change. Datadog shows the deploy on another screen. The written sentence that ties them, with both ids, is the product.

Shape it as evidence inside a finding, not as a third-party timeline page.

### 4. One review for the fleet

A team with Oracle, PostgreSQL, and SQL Server has three consoles. Second Look already normalizes load, top SQL, waits, and sessions into one summary shape.

A scheduled fleet note, not a dashboard: across targets, which findings opened since the last digest, which share a cause, and which databases had no significant change. The weekly trend digest on the “what’s next” list should be this note. Per-database trend mail is a thinner version of the snapshot page they already have.

Two links join findings across targets, and they are not equally strong:

- **The same deploy**, from bet 3. This is the link that crosses engines. “After `billing@1.4.2`, findings opened on `ora-prod` and `pg-prod`” is a true sentence, because the deploy id is the same on both.
- **The same statement**, within one engine. Replicas, shards and read copies run the same SQL, so a normalized text hash (whitespace and literals stripped, computed before redaction and stored next to `sql_id`) ties them. Do not promise this across engines: Oracle and PostgreSQL rarely run the same text, and bind styles differ.

OEM, pganalyze, and Query Store cannot write either sentence.

### 5. A proposed read, answered by a person

Chat already refuses to touch the live database and tells the reader what to run. Make that a first-class step on a finding: the model proposes one read-only statement (an `EXPLAIN`, a wait slice, a stats query), the person runs it, and they paste the result back into the thread.

The pasted result is operator-supplied text entering the evidence. It goes through the same literal masking as every report, it is stored with the snapshot, and it is labeled **supplied by a person** both in the prompt the next review sees and on the page. The model may cite it; it may not treat it as something Second Look measured.

That is the middle the pack misses. Either the assistant has production credentials, or it only has metrics and guesses the fix. A proposed read keeps credentials on the operator’s side and still deepens the evidence.

### 6. The fix as a change proposal

Findings already require a fix and, when the fix changes the database, a rollback. Take the next step as an artifact the reader can copy: the statement (create index, gather stats, a rewrite), the snapshot ids that justify it, how to undo it, and who would run it in a change window. Attach it to the Jira or Plane ticket from bet 2.

Still no apply button. The difference from EverSQL is that the proposal cites a before and after Second Look measured, and it stays a suggestion.

### 7. The usual band

Baselines today are one snapshot: previous, same time yesterday, same time last week, or a pin. A Monday batch still looks like an incident when last Monday’s run is missing or was itself odd.

For each target, compare the current interval with the spread of the same clock hour over the last several weeks (median and a high band), and say “inside the usual Monday 02:00 band” or “above every same-hour window in six weeks.” One comparison snapshot remains the narrative baseline. The band is a sentence in the evidence, which a single `same-time-last-week` pick cannot say. This uses stored summaries. It does not need a time-series database.

Two constraints from the data:

- Cumulative engines (PostgreSQL, MySQL, MariaDB) need two consecutive snapshots to form an interval, so each week contributes a window only when both exist. Interval engines (Oracle AWR, SQL Server) contribute one snapshot per week.
- A counter reset or restart inside a window (`counters_since`, AWR startup) voids that week. The band states how many weeks it is built from, and it is not written at all below three.

### 8. The record you can hand someone

Every run already keeps `manifest.json`, `compare.json`, `triage.json`, and discussions. Package one snapshot as a bundle a postmortem or an auditor can take: what triggered it, what was compared, which model and how many tokens, the redacted SQL, the findings with their agreement marks and life, and the decision (accepted, muted, ticketed). No live database in the bundle.

One prerequisite: the model run stores only a tool-call count today. Capture the tool transcript (tool name, arguments, truncated result) as `tools.json` on each run before this bet, so the bundle can show which reports the model actually read.

Datadog exports charts. The model chat exports a transcript. The bundle is the review plus the evidence, with the safety properties intact (literals masked, credentials absent).

## What to build first

Status on 2026-11-11: the groundwork, bets 1, 2 and 3 and the tool transcript have landed. Next is bet 4, the fleet note, which now has the deploy id to join findings across targets.

The groundwork (kind and subject on the model's finding, one matcher), then bets 1 and 2, then the settings diff from bet 3, then deploy feeds.

Agreement marks come first because the data is already stored and the matcher is the only new code: it is the cheapest proof the join works. Finding identity is the behavior on-call will notice on the second day: the channel stops repeating itself, and the ticket stays the same object. The settings diff makes the correlation line true without a single integration. The deploy feed is already named in `docs/operations.md` and in the reviewer prompt.

Fleet digest, proposed reads, change proposals, the usual band, and the audit bundle sit on top of those. They are the same idea at a larger scope: a finding with a life, evidence a person can check, and a record.

## How we will know

Both counts come from stored objects. Neither needs a new service.

- **Repeat posts per target per week.** A delivered finding whose identity was already open in the previous run. Bet 2 should drive this toward zero for unchanged findings.
- **Rules findings the model dropped.** Per run, the count of `heuristics` entries at medium or above with no matching model finding. Bet 1 makes this visible; a rising number is a prompt or model problem to look at, not something to hide.

Query Store and Statspack stay on the coverage list. They widen who can use the review. They do not replace it.
