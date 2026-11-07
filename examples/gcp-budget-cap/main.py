"""Budget cap for one Google Cloud project: when a budget's actual spend reaches its amount, turn off one API.

The budget publishes its current status to a Pub/Sub topic several times a day, whether or not a threshold was
crossed. This function reads each message and, once costAmount >= budgetAmount, disables CAP_SERVICE (default
aiplatform.googleapis.com, Vertex AI) on CAP_PROJECT. costAmount is the cost accrued so far in the period, never a
forecast: forecasts arrive only as forecastThresholdExceeded, which this ignores, so a forecast alert stays email-only.
Nothing is deleted; turn it back on with:  gcloud services enable aiplatform.googleapis.com --project <project>
"""
import base64
import json
import os
import time

import functions_framework
import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT = os.environ["CAP_PROJECT"]
SERVICE = os.environ.get("CAP_SERVICE", "aiplatform.googleapis.com")


@functions_framework.cloud_event
def cap(event):
    data = json.loads(base64.b64decode(event.data["message"]["data"]))
    cost, budget = float(data.get("costAmount", 0)), float(data.get("budgetAmount", 0))
    name = data.get("budgetDisplayName", "?")
    if budget <= 0 or cost < budget:
        print(f"{name}: {cost:.2f} of {budget:.2f} {data.get('currencyCode', '')}: under the cap")
        return
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    session = AuthorizedSession(creds)
    url = f"https://serviceusage.googleapis.com/v1/projects/{PROJECT}/services/{SERVICE}"
    if session.get(url).json().get("state") == "DISABLED":
        print(f"{name}: {cost:.2f} >= {budget:.2f}: {SERVICE} already off")
        return
    # Dependents go off too: without them the call refuses, and a cap that refuses keeps spending. So note what is
    # on before and after, and log every service turned off: the revert re-enables exactly those.
    before = _enabled(session)
    r = session.post(f"{url}:disable", json={"disableDependentServices": True, "checkIfServiceHasUsage": "SKIP"})
    r.raise_for_status()
    op = r.json()
    if not _wait(session, op):  # still running: the list would be incomplete, so point at the full record instead
        print(f"{name}: {cost:.2f} >= {budget:.2f}: turning off {SERVICE} on {PROJECT}, still in progress "
              f"({op.get('name', '?')}). Every API it turns off is in the audit log: gcloud logging read "
              f"'protoPayload.methodName:\"DisableService\"' --project {PROJECT}")
        return
    off = sorted(before - _enabled(session)) or [SERVICE]
    print(f"{name}: {cost:.2f} >= {budget:.2f}: turned off {', '.join(off)} on {PROJECT}. "
          f"Revert: gcloud services enable {' '.join(off)} --project {PROJECT}")


def _enabled(session) -> set[str]:
    names, token = set(), ""
    while True:
        page = session.get(f"https://serviceusage.googleapis.com/v1/projects/{PROJECT}/services",
                           params={"filter": "state:ENABLED", "pageSize": 200, "pageToken": token}).json()
        names |= {s["config"]["name"] for s in page.get("services", [])}
        token = page.get("nextPageToken", "")
        if not token:
            return names


def _wait(session, op: dict, timeout_s: float = 90) -> bool:
    """The disable is a long-running operation: wait for it (the function may run 120 s), so the services listed
    after it are accurate. False if it is still running when the wait ends."""
    deadline = time.monotonic() + timeout_s
    while not op.get("done") and op.get("name") and time.monotonic() < deadline:
        time.sleep(2)
        op = session.get(f"https://serviceusage.googleapis.com/v1/{op['name']}").json()
    if op.get("error"):
        raise RuntimeError(f"disabling {SERVICE} failed: {op['error']}")
    return bool(op.get("done")) or not op.get("name")
