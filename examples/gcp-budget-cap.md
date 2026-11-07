# Google Cloud: a hard stop on the lab project

_The IDs and names on this page are placeholders: put in your own._

Budgets in Google Cloud only alert. This turns the `monthly-cap-10 (my-ai-project)` budget (10 CAD a month,
project `my-ai-project` only, on billing account `000000-AAAAAA-BBBBBB`) into a "stop the bleeding" cap. The
budget publishes its status to a Pub/Sub topic several times a day. A small Cloud Run function reads each message
and, once actual spend reaches the budget, turns off the Vertex AI API on the project. Every Gemini or Claude call
through Vertex then fails until a person turns it back on. Nothing is deleted.

```
budget monthly-cap-10 (my-ai-project)    10 CAD/month; emails at 50, 80 and 100% actual and 100% forecast
  └─ Pub/Sub topic budget-cap-10           billing-budget-alert@system.gserviceaccount.com publishes
       └─ Eventarc trigger                 runs as budget-cap@my-ai-project.iam.gserviceaccount.com
            └─ function budget-cap-10      us-central1, Python 3.12, one instance at most
                 └─ costAmount >= budgetAmount: disable aiplatform.googleapis.com on my-ai-project
```

It is the same idea as [the Azure cap](azure-budget-cap.md): cut off the model and leave everything else alone.
Google's own documented cap unlinks billing from the project, which stops every service and can lead to
resources being deleted; this one does not go that far.

## What was created (2026-11-06)

| Resource | Name | Notes |
|---|---|---|
| APIs | billingbudgets, pubsub, cloudfunctions, run, cloudbuild, artifactregistry, eventarc | enabled on the project |
| Pub/Sub topic | `budget-cap-10` | the budget's notification channel |
| Service account | `budget-cap@my-ai-project.iam.gserviceaccount.com` | runs the function and its trigger; holds nothing else |
| Function (2nd gen) | `budget-cap-10` | source in [gcp-budget-cap/](gcp-budget-cap/); env `CAP_PROJECT`, `CAP_SERVICE`; 120 s timeout |
| Budget | `monthly-cap-10 (my-ai-project)` | 10 CAD, calendar month, filtered to this project; Pub/Sub rule to the topic |

The function and Pub/Sub run well inside the free tiers. Building the function used Cloud Build's free minutes,
and its image sits in Artifact Registry (well under the free 0.5 GB).

## The roles

The service account needs three roles on the project (granted 2026-11-06). Granting them is for the project owner:

```sh
for R in roles/serviceusage.serviceUsageAdmin roles/run.invoker roles/eventarc.eventReceiver; do
  gcloud projects add-iam-policy-binding my-ai-project \
    --member=serviceAccount:budget-cap@my-ai-project.iam.gserviceaccount.com --role=$R --condition=None
done
```

`serviceUsageAdmin` lets it turn APIs on and off in this project, and only this one. `run.invoker` and
`eventarc.eventReceiver` let the Pub/Sub trigger call the function.

## Testing it

Publish a fake budget message, in the same shape the budget sends. Under the cap, the function only logs:

```sh
gcloud pubsub topics publish budget-cap-10 --project my-ai-project \
  --message='{"budgetDisplayName":"test-under","costAmount":1.0,"budgetAmount":10.0,"currencyCode":"CAD"}'
```

Over the cap, it turns Vertex AI off. Check it, then turn Vertex AI back on (below):

```sh
gcloud pubsub topics publish budget-cap-10 --project my-ai-project \
  --message='{"budgetDisplayName":"test-over","costAmount":11.0,"budgetAmount":10.0,"currencyCode":"CAD"}'
gcloud services list --enabled --project my-ai-project --filter=config.name=aiplatform.googleapis.com
gcloud functions logs read budget-cap-10 --gen2 --region us-central1 --project my-ai-project --limit 5
```

## Verified (2026-11-06)

Both test messages above went through the whole chain. The under-the-cap one logged
`test-under: 1.00 of 10.00 CAD: under the cap`. The over-the-cap one logged
`test-over: 11.00 >= 10.00: turned off aiplatform.googleapis.com on my-ai-project`, and Vertex AI was off 12 seconds
after publishing. The audit log showed one `DisableService`, on `aiplatform.googleapis.com` only, by
`budget-cap@`, and every other API stayed on. Vertex AI was turned back on straight after.

A forecast-style message (accrued `costAmount` 2.0, `forecastThresholdExceeded` 1.0) on the redeployed function only
logged `test-forecast: 2.00 of 10.00 CAD: under the cap`, and Vertex AI stayed on.

On revision 3, which logs what it turns off, an over-the-cap message logged
`turned off aiplatform.googleapis.com on my-ai-project. Revert: gcloud services enable aiplatform.googleapis.com
--project my-ai-project`. 57 APIs were enabled before and 56 after, and Vertex AI was turned back on straight after.

## Revert (turn the model back on)

```sh
gcloud services enable aiplatform.googleapis.com --project my-ai-project
```

If other enabled APIs depend on the one the cap turns off, they go off with it: otherwise Google refuses the
request, and a cap that is refused keeps spending. The function waits for the switch-off to finish (up to 90 s)
and logs every API it turned off, with the revert command for all of them. If it is still running when the wait
ends, the log says so and points at the audit log, which records each one
(`gcloud logging read 'protoPayload.methodName:"DisableService"' --project my-ai-project`):

```sh
gcloud functions logs read budget-cap-10 --gen2 --region us-central1 --project my-ai-project --limit 5
```

On this project nothing depends on Vertex AI, so only `aiplatform.googleapis.com` goes off.

## Caveats

- **Actual spend only.** Each message's `costAmount` is the cost accrued so far this month; a forecast arrives
  only as a separate `forecastThresholdExceeded` key, which the function ignores. So the 100% forecast threshold
  sends an email but never cuts anything off, as with the Azure cap. (Google's
  [notification format](https://cloud.google.com/billing/docs/how-to/budgets-programmatic-notifications).)
- **Lag.** Billing data arrives hours late, and the budget reports only a few times a day, so expect to overshoot
  10 CAD by up to a day's spend.
- **It keeps firing.** The budget reports all month, so once spend is over 10 CAD, the function turns Vertex AI off
  again on the next message. To keep using it after a revert, raise the budget amount or wait for next month.
- **Scope.** Only the Vertex AI API on this project is turned off, plus any API that depends on it (none here; the
  log names them). The two AI Studio projects on the same billing
  account (`gen-lang-client-…`) are not covered by this budget.
- **Removal.** `gcloud billing budgets delete`, `gcloud functions delete budget-cap-10 --gen2 --region us-central1`,
  `gcloud pubsub topics delete budget-cap-10` and `gcloud iam service-accounts delete budget-cap@…`, all with
  `--project my-ai-project` (the budget with `--billing-account 000000-AAAAAA-BBBBBB`).
