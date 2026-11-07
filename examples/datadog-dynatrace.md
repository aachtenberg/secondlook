# Alert-driven runs from Datadog / Dynatrace / PagerDuty

All of them POST JSON to `dbtriage serve` (default port 8080, bearer token from `DBTRIAGE_WEBHOOK_TOKEN`).
The receiver only needs to find a **target name** that matches `[targets.<name>]` in `targets.toml`.

**Datadog** - monitor notification to a Webhook integration with a custom payload:

```json
{"target": "prod-db-3", "mode": "incident", "window_minutes": 30, "reason": "$EVENT_TITLE", "source": "datadog"}
```
(or leave the default payload and tag the monitor `target:prod-db-3`; the receiver reads `$TAGS`).

**Dynatrace** - Problem notification, custom integration, JSON payload:

```json
{"target": "prod-db-3", "mode": "incident", "reason": "{ProblemTitle}", "source": "dynatrace"}
```
(or tag the monitored database entity `target:prod-db-3`; `ProblemDetails.tags` is read).

**PagerDuty** - v3 webhook subscription; put `target` in the incident's `custom_details`.

**Grafana / Alertmanager** - see `alertmanager.yml`; a `target` or `db` label is enough.

**Outputs back into observability**: add `pushgateway` (Prometheus gauge `dbtriage_severity{target,engine}`,
alert on it) or `webhook` (point it at the Datadog Events API or the Dynatrace Events API v2 with the
auth header in `[delivery.webhook].headers`) to `[delivery].channels`.
