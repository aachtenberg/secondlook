"""Shared fixtures for the three tiers of tests.

    pytest                      everything; tiers whose prerequisites are missing skip with a reason
    pytest -m "not browser and not live"    unit tests only (seconds, no services)
    pytest -m live              the lab databases, S3 and Postgres (docker compose up -d)
    pytest -m browser           the web view in a real Chrome (Playwright, system Chrome or `playwright install chromium`)

Credentials for live tests come only from the environment or the repo's .env (loaded the same way the app
loads it); no test file carries one.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def pytest_configure(config):
    config.addinivalue_line("markers", "live: needs the lab services from docker compose (skips when they are down)")
    config.addinivalue_line("markers", "browser: drives the web view in a real browser (skips without Playwright/Chrome)")
    config.addinivalue_line("markers", "allow_console_errors: a browser test that expects the browser to log errors (an outage page)")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 10.0):
    import urllib.request

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    raise RuntimeError(f"{url} did not come up")


# --------------------------------------------------------------------------- fake model server
class FakeLLM:
    """An OpenAI-compatible endpoint that behaves like a small, obedient model: it lists models, answers the
    connectivity check, and in a review or discussion calls one data tool, then submit_review / answers."""

    MODELS = ["fake-model", "fake-large", "whisper-1"]

    def __init__(self):
        self.requests: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path.rstrip("/").endswith("/models"):
                    return self._send({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "fake"} for m in FakeLLM.MODELS]})
                self._send({"error": "not found"}, 404)

            def do_POST(self):  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(req)
                self._send(fake.reply(req))

        self.server = ThreadingHTTPServer(("127.0.0.1", free_port()), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def _msg(content=None, calls=None):
        m = {"role": "assistant", "content": content}
        if calls:
            m["tool_calls"] = [{"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
        return {"id": "fake", "object": "chat.completion", "created": int(time.time()), "model": "fake-model",
                "choices": [{"index": 0, "message": m, "finish_reason": "tool_calls" if calls else "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}

    def reply(self, req: dict) -> dict:
        tools = [t["function"]["name"] for t in req.get("tools") or []]
        msgs = req["messages"]
        text = "\n".join(m.get("content") or "" for m in msgs if m["role"] == "user")
        done_tools = sum(1 for m in msgs if m["role"] == "tool")
        if not tools:
            return self._msg("OK")
        if "submit_review" in tools:  # a review
            cur = re.search(r"Current snapshot: (\S+)", text)[1]
            base = re.search(r"Baseline snapshot: (\S+)", text)
            if done_tools == 0:
                if base and not base[1].startswith("none"):
                    return self._msg(calls=[("compare_snapshots", {"snapshot_id": cur, "baseline_id": base[1]})])
                return self._msg(calls=[("get_report", {"snapshot_id": cur, "section": "load_profile"})])
            if done_tools == 1:
                return self._msg(calls=[("submit_review", {
                    "findings": [{"severity": "medium", "kind": "other", "subject": "", "where": "fake finding", "summary": "Fake model saw the comparison",
                                  "impact": "None, this is a test.", "evidence": ["one tool call before submitting"], "fix": "Nothing to fix."}],
                    "summary": "Fake review written over the real tool loop.", "checked": "comparison", "confidence": "low"})])
            return self._msg("done")
        # a discussion: read the snapshot once, then answer
        if done_tools == 0:
            sid = re.search(r"snapshot (\S+), captured", text)[1]
            return self._msg(calls=[("get_report", {"snapshot_id": sid, "section": "top_sql"})])
        q = re.findall(r"Answer this question now, using tools for any number you cite: (.*)", text)
        ans = f"**Fake answer** to: {q[-1] if q else '?'}"
        if q and "explain" in q[-1].lower():  # a proposed read, as the real model writes one
            ans += "\n\nLook for a sequential scan on orders:\n\n```proposed-read\nEXPLAIN SELECT count(*) FROM orders WHERE status = 'open';\n```"
        return self._msg(ans)


@pytest.fixture(scope="session")
def fake_llm():
    f = FakeLLM()
    yield f
    f.server.shutdown()
    f.server.server_close()


# --------------------------------------------------------------------------- a store with realistic snapshots
LONG_SQL = "293bbb76350e7dfa6f6db911f24d57c6880394be55b2b0aa14bf1a20e905968b"


def seed_store(root: Path):
    """Two databases: 'demo' with a baseline and an incident (findings, waits, load, long SQL ids, an agent error)
    and 'quiet' with one healthy snapshot. Everything the pages and the reviewer's tools read is present."""
    from dbtriage.storage import LocalStorage

    st = LocalStorage(root)
    snaps = [("demo", "20261105T090000Z", "09", "scheduled", "info"), ("demo", "20261105T100000Z", "10", "incident", "high"),
             ("quiet", "20261105T100000Z", "10", "scheduled", "info")]
    for i, (t, ts, hh, mode, sev) in enumerate(snaps):
        sid = f"postgres/{t}/2026-11-05/{ts}"
        late = t == "demo" and hh == "10"
        top = [{"sql_id": LONG_SQL, "calls": 2628 if late else 100, "total_time_ms": 45900.0 if late else 900.0, "mean_time_ms": 17.47 if late else 9.0,
                "rows": 2628, "io_blocks_read": 12, "plan_hash": 2 if late else 1, "share_of_time": 0.54,
                "text": "SELECT COUNT ( * ) FROM orders WHERE note LIKE ? AND amount > ?"},
               {"sql_id": "q2short", "calls": 18674, "total_time_ms": 24600.0, "mean_time_ms": 1.318, "rows": 18674, "plan_hash": 7, "share_of_time": 0.29,
                "text": "UPDATE orders SET amount = amount + ? WHERE id = ?"}]
        summary = {"captured_at": f"2026-11-05T{hh}:00:00+00:00", "metrics_kind": "interval", "mode": mode,
                   "load_profile": {"elapsed_s": 3600, "xact_commit": 5000 if late else 9000, "avg_active_sessions": 3.2 if late else 1.1,
                                    "tup_returned": 2.3e7 if late else 1.6e7},
                   "top_sql": top, "wait_events": [{"class": "IO", "event": "DataFileRead", "time_ms": 900 if late else 300},
                                                   {"class": "Lock", "event": "relation", "time_ms": 100 if late else 1}],
                   "tables": [], "sessions": [], "findings": [], "settings": {}}
        manifest = {"id": sid, "target": t, "engine": "postgres", "captured_at": summary["captured_at"], "window": {"mode": mode, "minutes": 60},
                    "trigger": {"source": "cli", "reason": "latency alert" if late else ""}, "reports": [], "metrics_kind": "interval"}
        if late:
            manifest["baseline"] = {"snapshot": "postgres/demo/2026-11-05/20261105T090000Z", "policy": "previous", "reason": "previous snapshot"}
        st.put_json(f"{sid}/summary.json", summary)
        st.put_json(f"{sid}/manifest.json", manifest)
        st.put_json(f"{sid}/raw.json", {"note": "raw"})
        if late:
            st.put_json(f"{sid}/compare.json", {
                "findings": [{"severity": "high", "title": "New heavy SQL takes 54% of SQL time", "kind": "sql_new", "detail": "select count(*) …", "evidence": {"sql_id": LONG_SQL}}],
                "load_profile": {"xact_commit": {"current": 5000, "current_per_s": 1.39, "reference": 9000, "reference_per_s": 2.5, "pct": -44.4},
                                 "tup_returned": {"current": 2.3e7, "current_per_s": 6388.9, "reference": 1.6e7, "reference_per_s": 4444.4, "pct": 43.8}},
                "waits": {"current": {"IO": {"share": 0.9, "value": 900}, "Lock": {"share": 0.1, "value": 100}},
                          "reference": {"IO": {"share": 0.99, "value": 300}, "Lock": {"share": 0.01, "value": 1}}},
                "sql": {"top": [dict(q, plan_changed=(q["sql_id"] == LONG_SQL)) for q in top], "new": [], "changed": [], "gone": []},
                "reference": {"snapshot": "postgres/demo/2026-11-05/20261105T090000Z", "policy": "previous", "reason": "previous snapshot"}})
            triage = {"severity": "high", "title": f"New heavy SQL {LONG_SQL} takes 54% of SQL time", "source": "rules", "confidence": "low",
                      "summary": f"1 heuristic finding; the top one is new heavy SQL {LONG_SQL}.",
                      "checked": "load profile, top SQL, wait classes against the previous snapshot (rules only)",
                      "agent_error": 'TypeError: "Could not resolve authentication method. Expected one of api_key"',
                      "findings": [{"severity": "high", "where": f"sql_id {LONG_SQL}", "summary": f"New heavy SQL {LONG_SQL} takes 54% of SQL time",
                                    "impact": "A new statement is taking most of the database's SQL time.",
                                    "evidence": [f'sql_id: "{LONG_SQL}"', "mean_time_ms: 17.47"], "fix": "Find which release introduced it."}],
                      "target": t, "engine": "postgres", "snapshot": sid}
        else:
            triage = {"severity": sev, "title": "no significant change", "source": "rules", "confidence": "medium", "summary": "Nothing crossed a rule threshold.",
                      "checked": "load profile, top SQL", "findings": [], "target": t, "engine": "postgres", "snapshot": sid}
        st.put_json(f"{sid}/triage.json", triage)
        st.put_text(f"{sid}/triage.md", f"**review** {triage['title']}\n")
    return st


@pytest.fixture
def seeded(tmp_path):
    return seed_store(tmp_path / "data")


# --------------------------------------------------------------------------- the web view in a thread
@pytest.fixture
def web_server(tmp_path, fake_llm, seeded):
    """A full web view (runs, editing, discussion on) over the seeded store, its model pointed at the fake server."""
    from dbtriage.config import Config, Target
    from dbtriage.web import make_server

    cfg = Config(storage_url=f"file://{tmp_path / 'data'}", base_dir=str(tmp_path),
                 targets={"demo": Target("demo", "postgres", "postgresql://u@127.0.0.1:1/x", extra={"links": [{"label": "Grafana", "url": "https://grafana.example/d/x"}]}),
                          "quiet": Target("quiet", "postgres", "postgresql://u@127.0.0.1:1/y")},
                 agent={"provider": "ollama", "model": "fake-model", "base_url": fake_llm.url})
    srv = make_server(cfg, seeded, "127.0.0.1", free_port(), True, None, True, True, False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    yield url
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- the lab
@pytest.fixture(scope="session")
def lab_env():
    """Load the repo's .env (lab DSNs, S3 keys) the way the app does; skip live tests when it is absent."""
    from dbtriage import envfile

    if not (REPO / ".env").exists():
        pytest.skip("no .env in the repo: copy .env.example and start the lab (docker compose up -d)")
    envfile.init(REPO / "targets.toml")
    return REPO / "targets.toml"
