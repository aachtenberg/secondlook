# MCP server

`dbtriage mcp` lets an MCP client, such as Claude Code, Claude Desktop or any other that speaks the Model Context
Protocol, read what dbtriage has stored: the databases it watches, their snapshots, and the review of each. You
ask in your own words ("anything odd on prod-db-3 since Tuesday?") and the client's model looks it up with the
same tools the reviewer uses.

It is **off by default**, and only `targets.toml` can turn it on. The web view shows whether it is on but cannot
change it, and its settings API refuses to.

```toml
[mcp]
enabled = true                 # without this, dbtriage mcp refuses to start
allow_run = false              # true adds run_review (below)
# tools = ["list_targets", "list_snapshots", "get_review"]    # offer only these; default: every read tool
```

It needs the optional SDK: `uv sync --extra mcp`.

## Connecting a client

The client starts `dbtriage mcp` itself and talks to it over stdin and stdout, so nothing listens on the network
and it runs as you, with your `targets.toml` and `.env`.

Claude Code:

```sh
claude mcp add dbtriage -- uv --directory /path/to/db-triage run --extra mcp dbtriage mcp
```

Claude Desktop, in `claude_desktop_config.json`:

```json
{"mcpServers": {"dbtriage": {"command": "uv",
  "args": ["--directory", "/path/to/db-triage", "run", "--extra", "mcp", "dbtriage", "mcp"]}}}
```

Add `--config /path/to/targets.toml` after `dbtriage` if the file is not in that directory.

## Tools

| Tool | What it returns |
|---|---|
| `list_targets` | the databases dbtriage watches, each with its latest snapshot and the review's severity and summary |
| `list_snapshots` | a database's snapshots, newest first, with when and why each was taken and its severity |
| `get_review` | one snapshot's review: findings with impact, evidence and fix, what was checked, the baseline |
| `get_report` | one section of a snapshot: load profile, top SQL, waits, ASH, sessions, tables, settings ... |
| `compare_snapshots` | the comparison of two snapshots: load deltas, new and changed SQL, plan changes, wait shifts |
| `get_sql_history` | one statement across a database's snapshots: calls, time, CPU time (Oracle, SQL Server), plan |
| `get_raw_report` | a page of an engine-native report stored with a snapshot, such as Oracle's AWR text |
| `get_recent_changes` | deploys and changes near a snapshot, from the target's changes file |
| `run_review` | collect and review a database now, only with `allow_run = true` |

The read tools are the reviewer's own, so they share its limits: SQL literals are masked before anything is
stored, raw reports are served only when the snapshot lists them, and none of them reaches a live database or
changes anything. Connection strings are never returned. A mistake, such as an unknown target or snapshot id,
comes back as text the model can act on.

`run_review` does what the web view's Run button does: it connects to the database as collection does
(read-only, unless that database is opted into taking snapshots on the database), reviews the snapshot with your configured model unless `use_model` is false, and posts the review
to your delivery channels. That spends model credit, so it is a separate switch.

## What the client's model sees

Whatever the tools return goes to the model your MCP client uses, which may not be the one dbtriage reviews with.
Masking SQL literals and AWR/ADDM text (`mask_report_literals`, on by default) keeps names and amounts out of it,
as it does for the reviewer. Use `tools` to offer less, for example only `list_targets` and `get_review`.
