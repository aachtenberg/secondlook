# Azure: a hard stop on the lab subscription

_The IDs and names on this page are placeholders: put in your own._

Budgets in Azure only alert. This turns the `monthly-cap-10` budget (10 CAD/month on subscription
`00000000-0000-0000-0000-000000000000`) into a "stop the bleeding" cap: at 100% of actual spend an
action group calls a Logic App whose managed identity disables public network access on the AI Services
account, so every gpt-5-mini call fails until a human turns it back on. Emails at 50/80/100% and the
forecast alert stay as they were.

```
budget monthly-cap-10 (actual > 100%)
  └─ action group ag-budget-cap-10         (rg-dbtriage; also emails the owner)
       └─ Logic App la-budget-cap-10       (Consumption, eastus2, system-assigned identity)
            └─ PATCH .../accounts/my-ai-resource  {"publicNetworkAccess": "Disabled"}
```

## What was created (2026-11-05)

| Resource | Name | Notes |
|---|---|---|
| Logic App (Consumption) | `la-budget-cap-10` | HTTP-request trigger `budget_alert`; one HTTP action, auth = managed identity, audience `https://management.azure.com`. Costs fractions of a cent per run. |
| Action group | `ag-budget-cap-10` | Logic App receiver (callback URL stored inside the action group, never written down) + email to the owner. |
| Budget notification | `monthly-cap-10` / `actual100` | `contactGroups` now points at the action group; emails unchanged. |
| Role assignment | **not yet done, see below** | The Logic App identity needs *Cognitive Services Contributor* on the account. |

Resource providers `Microsoft.Logic` and `microsoft.insights` were registered on the subscription.

## One step left: grant the role

The automation assistant is not allowed to grant permissions, so run this once (owner of the resource):

```sh
az role assignment create \
  --assignee-object-id 11111111-1111-1111-1111-111111111111 --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services Contributor" \
  --scope /subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dbtriage/providers/Microsoft.CognitiveServices/accounts/my-ai-resource
```

`11111111-…` is the Logic App's system-assigned identity (`az logic workflow show … --query identity.principalId`
or `az rest` on the workflow). Role assignments take a minute or two to propagate.

## Verified

A manual run of the Logic App (`POST …/triggers/budget_alert/run`) on 2026-11-05 went through the whole
chain and failed only at the PATCH with `Forbidden`, which is the missing role; the account stayed `Enabled`.
After granting the role, re-run the same test and expect `publicNetworkAccess: Disabled`, then revert:

```sh
SUB=00000000-0000-0000-0000-000000000000; RG=rg-dbtriage
LA=/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Logic/workflows/la-budget-cap-10
az rest --method post --url "https://management.azure.com$LA/triggers/budget_alert/run?api-version=2019-05-01" --body '{}'
sleep 10
az rest --method get --url "https://management.azure.com$LA/runs?api-version=2019-05-01&\$top=1" --query "value[0].properties.status" -o tsv
az rest --method get --url "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/my-ai-resource?api-version=2024-10-01" --query properties.publicNetworkAccess -o tsv
```

## Revert (turn the model back on)

```sh
az rest --method patch \
  --url "https://management.azure.com/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-dbtriage/providers/Microsoft.CognitiveServices/accounts/my-ai-resource?api-version=2024-10-01" \
  --body '{"properties":{"publicNetworkAccess":"Enabled"}}' --query properties.publicNetworkAccess -o tsv
```

(Validated as a no-op while the account was already `Enabled`.) The Portal equivalent is the account's
Networking blade, "All networks".

## Caveats

- **Lag.** Budgets evaluate against cost data that arrives 8 to 24 hours late, and the notification fires
  on the next evaluation. Expect to overshoot 10 CAD by up to a day of spend. For a tighter stop lower the
  deployment's TPM quota, or raise the threshold to `forecast100` as well (left as email-only on purpose so a
  busy day's forecast doesn't cut off smoke tests).
- **Scope.** Only this one account is disabled. Anything else that spends in the subscription keeps running.
- **Re-arming.** Once per month at most: budgets notify once per threshold per period. After a revert the
  cap will not fire again until next month unless spend crosses 100% again after a reset.
- **Removal.** `az rest --method delete` on the Logic App and action group, then put `contactGroups: []`
  back on the budget's `actual100` notification.
