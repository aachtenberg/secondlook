# Azure (Timer trigger) and GCP (Cloud Scheduler -> Cloud Run job)

Both call the same Python entry point as the CLI. Azure Functions, timer trigger, Python v2 model:

```python
import azure.functions as func
from dbtriage.triggers import TriageRequest
from dbtriage.cli import run_request

app = func.FunctionApp()

@app.timer_trigger(schedule="0 0 * * * *", arg_name="timer")   # hourly
def hourly(timer: func.TimerRequest):
    run_request(TriageRequest(target="prod-db-3", mode="scheduled", window_minutes=60, source="azure-timer"))
```

Storage can stay S3-compatible (`DBTRIAGE_STORAGE=s3://...` with `AWS_ENDPOINT_URL`) on any cloud; Azure Blob
works as `DBTRIAGE_STORAGE=az://container/prefix` (install the extra: `uv sync --extra azure`). Teams delivery: `[delivery.teams]`.
GCP: build the image, create a Cloud Run job with `run -t prod-db-3 --mode scheduled`, trigger it from Cloud Scheduler.
