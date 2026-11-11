# Storage

Every snapshot, review and discussion is kept in one store. You pick it with a URL, in `[storage] url` or the
`DBTRIAGE_STORAGE` environment variable. Nothing else changes when you switch.

## Options

| URL | Where the data goes | How it signs in |
|---|---|---|
| `file:///var/lib/dbtriage` | a local disk, or an NFS or SMB share used by several servers | file permissions |
| `s3://bucket/prefix` | AWS S3, or anything S3-compatible: MinIO, Ceph, versitygw (set `AWS_ENDPOINT_URL`) | the usual AWS chain: environment, profile, or the role on the workload. A daily session token is part of that chain, and a long-running process re-reads the credentials file when it changes |
| `az://container/prefix` | Azure Blob Storage (install with `uv sync --extra azure`) | `AZURE_STORAGE_CONNECTION_STRING`, or `AZURE_STORAGE_ACCOUNT` with a SAS token or a managed identity |
| `postgres://user@host:5432/db?schema=dbtriage` | any PostgreSQL-compatible database: PostgreSQL, RDS or Aurora, Cloud SQL, AlloyDB, Azure Database for PostgreSQL, CockroachDB, YugabyteDB | `PGPASSWORD` or `~/.pgpass` is best; a password in the URL works and is always shown masked |

Buckets, containers and tables are created on the first write. In Postgres everything sits in one table,
`dbtriage_objects`; JSON goes into a `jsonb` column, so you can query snapshots with plain SQL. Extra connection
settings such as `sslmode=require` can be added to the URL.

In a container, mount a volume and point the URL at it. The compose file does that for the `dbtriage` service:

```yaml
    environment: {DBTRIAGE_STORAGE: "file:///var/lib/dbtriage"}
    volumes: ["dbtriage-data:/var/lib/dbtriage"]
```

To try Azure locally, run Azurite: `docker run -d -p 10000:10000 mcr.microsoft.com/azure-storage/azurite
azurite-blob --blobHost 0.0.0.0 --skipApiVersionCheck` and use its well-known `devstoreaccount1` connection
string.

## Sharing settings between servers

What you change on the Settings page (databases, schedules, the model, delivery, branding) is normally saved in
`dbtriage-settings.json` next to `targets.toml`. Point `DBTRIAGE_SETTINGS` at a `postgres://` URL instead and it
lives in table `dbtriage_settings`, so every web and scheduler server sees the same configuration.

Moving what you already have takes one command each, and both can be re-run safely:

```bash
dbtriage copy-store    --from file://./data            --to postgres://dbtriage@db/ops?schema=dbtriage
dbtriage copy-settings --from dbtriage-settings.json   --to postgres://dbtriage@db/ops?schema=dbtriage
```

## When the store is down

A remote store (Postgres, S3, Azure) can go away without taking dbtriage with it.

![Timeline: a save to the store gets no answer in time, so the snapshot is kept in the local spool; pages show a banner and the store is left alone for 30 seconds; a health check, the next write or a scheduler tick finds it answering again, and what waited is pushed, files first and the manifest last](img/store-outage.svg)

What you see during an outage:

- **Everything still starts.** The web view, the scheduler and the command line do not need the store to start.
- **Pages keep working.** They show what they can reach under a warning banner. A page that needs data only the
  store holds says so with a clear "try again shortly" (HTTP 503) instead of an error.
- **Nothing hangs.** A Postgres store gives up on a call after 5 seconds; S3 and Azure give up on connecting after
  5 seconds and on a read after 15, with two attempts. After a failure the store is left alone for 30 seconds, so
  only one request is slow.
- **New work is kept.** Captures, reviews and chat answers wait in a local spool (`./spool` next to the
  config, or `DBTRIAGE_SPOOL`) and are pushed automatically when the store answers again. `dbtriage flush-spool`
  pushes them by hand.
- **Settings stay usable.** Settings kept in Postgres also have a last good copy on disk
  (`dbtriage-settings.cache.json`, owner-only). The app runs on it, and saving is refused with a clear message so
  no server drifts away from the shared settings.
- **Monitoring knows.** `/health` returns `ok: false` with `store_ok`, `spooled` and `settings_ok`.

One trade-off: a capture taken during an outage cannot read the history in the store, so it has nothing to
compare with and is reviewed on its own.

`DBTRIAGE_NO_SPOOL=1` turns the spool off. A `file://` store is not covered: a local disk or NFS share has its own
failure modes, and a hung NFS mount will still block.

The live Postgres tests run when `DBTRIAGE_TEST_PG_URL` names a database; they work in a throwaway schema.
