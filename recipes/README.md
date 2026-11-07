# Recipes

Ready-to-copy setups. Pick the one closest to yours, copy its two files next to each other, fill in the
`.env`, and start dbtriage. Every line is commented, so delete what you do not need.

```bash
cp recipes/aws/targets.toml targets.toml
cp recipes/aws/.env.example .env      # then fill in the real values
uv run dbtriage web --allow-run --allow-edit --allow-chat --schedule
```

| Recipe | For | Snapshots in | AI model | Reviews go to |
|---|---|---|---|---|
| [small-team](small-team/targets.toml) | one server, a home lab, a small team | local disk | Ollama on your own machine (free) | phone notifications (ntfy) |
| [aws](aws/targets.toml) | RDS, Aurora or EC2 databases | S3 | Amazon Nova on Bedrock, or Claude | Slack, Jira, Prometheus |
| [azure](azure/targets.toml) | Azure Database or Oracle on Azure VMs | Blob Storage | Azure OpenAI | Teams, email |
| [gcp](gcp/targets.toml) | Cloud SQL and friends | a Cloud SQL Postgres database | Gemini on Vertex AI | Slack |
| [shared-postgres](shared-postgres/targets.toml) | several servers sharing one setup, behind single sign-on | a shared Postgres database (settings too) | Claude on Bedrock | Teams, Jira |

Mix and match freely: the storage, model and delivery sections are independent of each other.

## The full reference

- [reference.toml](reference.toml) explains every option in `targets.toml`, with its default.
- [reference.env](reference.env) lists every environment variable dbtriage reads, grouped by purpose.

A test (`tests/test_recipes.py`) loads every recipe and fails when the code gains an option or an environment
variable these files do not mention, so they stay complete.
