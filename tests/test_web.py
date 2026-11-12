import base64
import json
import threading
import time
import urllib.request
from urllib.error import HTTPError

import pytest

from dbtriage.config import Config, Target
from dbtriage.storage import LocalStorage
from dbtriage.web import App, md_to_html, serve, sort_value

SID = "postgres/demo/2026-11-05/20261105T100000Z"


def store(tmp_path):
    st = LocalStorage(tmp_path / "data")
    for i, ts in enumerate(("20261105T090000Z", "20261105T100000Z")):
        sid = f"postgres/demo/2026-11-05/{ts}"
        st.put_json(f"{sid}/manifest.json", {"id": sid, "target": "demo", "engine": "postgres", "captured_at": f"2026-11-05T{9 + i:02d}:00:00+00:00",
                                             "window": {"mode": "adhoc"}, "trigger": {"source": "cli", "reason": "r"}, "reports": ["awr.txt"]})
        st.put_json(f"{sid}/summary.json", {"captured_at": f"2026-11-05T{9 + i:02d}:00:00+00:00", "metrics_kind": "interval", "mode": "adhoc",
                                            "load_profile": {"elapsed_s": 60, "xact_commit": 10 + i, "avg_active_sessions": 1.0 + i},
                                            "top_sql": [{"sql_id": "q1", "calls": 3, "total_time_ms": 30 + i, "mean_time_ms": 1.0 + i, "plan_hash": 100 + i, "text": "select 1"}],
                                            "wait_events": [], "tables": [], "sessions": [], "findings": [],
                                            "settings": {"work_mem": "4MB" if i == 0 else "64MB", "jit": "on"}})
        st.put_json(f"{sid}/triage.json", {"severity": "high", "title": "Bad query", "source": "rules"})
        st.put_text(f"{sid}/triage.md", "**[HIGH] demo** - Bad query\n- evidence")
        st.put_text(f"{sid}/awr.txt", "WORKLOAD REPOSITORY")
        st.put_json(f"{sid}/compare.json", {"findings": [{"severity": "high", "title": "x"}], "current": {"mode": "adhoc"},
                                            "load_profile": {"xact_commit": {"current": 10 + i, "current_per_s": 1.0 + i, "reference": 5, "pct": 50.0}},
                                            "sql": {"top": [{"sql_id": "q1", "share_of_time": 0.9, "calls": 3, "mean_time_ms": 1.0 + i, "plan_hash": 100 + i, "text": "select 1"}]}})
    return st


def config(tmp_path, **web):
    t = Target("demo", "postgres", "dsn", extra={"links": [{"label": "Grafana", "url": "https://g/x"}]})
    return Config(storage_url="x", targets={"demo": t}, web=web, base_dir=str(tmp_path))


def test_md_to_html():
    h = md_to_html("**[HIGH] x** - t\n\n**Evidence**\n- a `b`\n- c\n\n1. one\n2. two\n\n_snapshot x_").replace("\n", "")
    assert "<b>[HIGH] x</b>" in h and "<ul><li>a <code>b</code></li>" in h and "<ol><li>one</li><li>two</li></ol>" in h and "<i>snapshot x</i>" in h
    review = md_to_html("**2 findings**\n\n1. one\n   - **Impact:** a\n   - **Fix:** b\n\n2. two\n   - **Impact:** c\n\n**Checked:** x").replace("\n", "")
    assert review.count("<ol>") == 1 and "<li>one<ul><li><b>Impact:</b> a</li><li><b>Fix:</b> b</li></ul></li><li>two<ul>" in review and review.endswith("<p><b>Checked:</b> x</p>")
    assert "<script" not in md_to_html("<script>alert(1)</script>")


def test_pages_and_branding(tmp_path):
    (tmp_path / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "theme.css").write_text("body{letter-spacing:1px}")
    cfg = config(tmp_path, brand="Acme SRE", title="DB Review", logo="logo.svg", accent="#123456", footer="internal", custom_css="theme.css", page_size=1)
    app = App(cfg, store(tmp_path), allow_run=False, token=None)
    idx = app.index().decode()
    assert "Acme SRE" in idx and "<title>overview · DB Review</title>" in idx and "src='/brand/logo'" in idx
    assert "--accent:#123456" in idx and "letter-spacing:1px" in idx and "<footer>internal</footer>" in idx
    assert "Bad query" in idx and "class='sev high'" in idx and "Run now" not in idx and "href='https://g/x'" in idx
    page = app.target("demo", 0).decode()
    assert "2026-11-05 10:00:00" in page and "1-1 of 2" in page and "older" in page and "09:00:00" not in page
    assert "09:00:00" in app.target("demo", 1).decode()
    snap = app.snapshot(SID).decode()
    assert "<li>evidence</li>" in snap and "awr.txt" in snap and "href='/t/demo/sql/q1' title='q1'>q1</a>" in snap and "<h1>Snapshot 10:00:00</h1>" in snap
    assert App(cfg, store(tmp_path), allow_run=True, token=None).index().decode().count("<option>demo</option>") == 1


def test_template_override(tmp_path):
    (tmp_path / "tpl").mkdir()
    (tmp_path / "tpl" / "base.html").write_text("<html><body class=custom>$brand|$body</body></html>")
    app = App(config(tmp_path, templates_dir="tpl", brand="B"), store(tmp_path), False, None)
    out = app.index().decode()
    assert out.startswith("<html><body class=custom>B|") and "<h1>Overview</h1>" in out


def test_nav_charts_and_drawer(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None)
    idx = app.index().decode()
    assert "<nav class=\"side\"" in idx and "<i class='dot high'></i><span>demo</span><span class='cnt'>2</span>" in idx and "class='tile'" in idx and "Settings</a>" in idx
    assert "form class=\"search\"" in idx and "<a class='add' href='/targets/new'" not in idx
    page = app.target("demo", 0).decode()
    assert "<svg class='chart trend'" in page and page.count("<circle cx='") == 2 and "2 snapshots" in page and "<option value='xact_commit' selected>" in page
    assert "class='t active'" in page and "<div class='sub'>" in page and "<span class='status high'>" in page and "incident</span>" in page
    assert "Open incidents</span><span class='num high'>2</span>" in page and "<a class='btn' href='/t/demo/compare'>Compare</a>" in page
    assert "<a href='/t/demo?filter=incidents'>Incidents</a>" in page and "<a href='/t/demo?filter=all' class=on>All</a>" in page
    assert "<a class='add' href='/targets/new'" in App(config(tmp_path), store(tmp_path), False, None, allow_edit=True).index().decode()
    snap = app.snapshot(SID).decode()
    assert "data-panel href='/f/postgres/demo/2026-11-05/20261105T100000Z/awr.txt'" in snap and "id=\"panel\"" in snap and "90%" in snap
    assert "Load profile vs reference" in snap and "Wait time by class" not in snap  # no waits in the fixture -> no empty figure
    assert "<a class='mono' href='/t/demo/sql/q1' title='q1'>q1</a>" in snap and "Change reference</a>" in snap and "id='discuss'" not in snap


def test_trend_and_sql_history(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None)
    pts, used = app.trend_points("demo", None)
    assert used == "xact_commit/s" and [p["value"] for p in pts] == [1.0, 2.0] and pts[0]["severity"] == "high"
    pts, used = app.trend_points("demo", "xact_commit")
    assert used == "xact_commit/s" and app.metrics_of("demo") == ["xact_commit"]
    hist = app.sql_history("demo", "q1")
    assert [h["plan_changed"] for h in hist] == [False, True] and hist[1]["mean_time_ms"] == 2.0 and app.sql_history("demo", "nope") == []
    page = app.sql_page("demo", "q1").decode()
    assert "<code>q1</code>" in page and "select 1" in page and "2 distinct plan(s)" in page and "<polygon" in page and "<circle" in page and "changed</span>" in page


def test_settings_pages(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    s = app.settings_page().decode()
    assert "<option selected>auto</option>" in s and "href='/targets/demo'" in s and "+ add target" in s and "<td class=mut>file</td>" in s
    form = app.target_form("demo").decode()
    assert "Edit target demo" in form and "readonly" in form and "<option selected>postgres</option>" in form and "Grafana https://g/x" in form
    assert "Delete" in form and "value='dsn'" not in form  # secrets never echoed
    assert "Read-only" in App(config(tmp_path), store(tmp_path), False, None).target_form(None).decode()
    name, fields = app.parse_target_form({"name": ["ora"], "engine": ["oracle"], "dsn": ["h/x"], "links": ["g https://g\nk https://k"], "diagnostics_pack": ["on"]})
    assert name == "ora" and fields["links"] == [{"label": "g", "url": "https://g"}, {"label": "k", "url": "https://k"}] and fields["diagnostics_pack"]
    assert app.save_target("ora", fields) == [] and "ora" in app.cfg.targets and app.cfg.targets["ora"].diagnostics_pack
    assert app.save_target("ora", {**fields, "dsn": ""}) == [] and app.cfg.targets["ora"].dsn == "h/x"  # blank keeps the stored secret
    ok, msg = app.probe_target("ora")
    assert not ok and msg  # no listener on that host, but the probe returns a message rather than raising
    assert app.api_settings()["targets"]["ora"]["origin"] == "ui" and "password" not in app.api_settings()["targets"]["ora"]


def test_baseline_in_forms_and_pages(tmp_path):
    app = App(config(tmp_path), store(tmp_path), allow_run=True, token=None, allow_edit=True)
    idx = app.index().decode()
    assert "<dialog id='run'>" in idx and "<select name=baseline_policy>" in idx and "<option value='same-time-last-week'>same time last week</option>" in idx
    assert app.start_run("demo", "adhoc", "", False, "bogus") == (400, {"error": "baseline_policy must be one of ('previous', 'same-time-yesterday', 'same-time-last-week', 'pinned')"})
    form = app.target_form("demo").decode()
    assert "<option value='previous' selected>previous snapshot</option>" in form and 'name="baseline"' in form
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["postgres"], "dsn": ["x"], "baseline_policy": ["pinned"], "baseline": ["postgres/demo/2026-11-05/20261105T090000Z"]})
    assert fields["baseline_policy"] == "pinned" and app.save_target("demo", fields) == []
    assert app.cfg.targets["demo"].extra["baseline_policy"] == "pinned" and app.cfg.targets["demo"].extra["baseline"].endswith("090000Z")
    assert app.settings.put_target("x", {"engine": "postgres", "dsn": "d", "baseline_policy": "weekly"}) == ["baseline_policy: one of previous, same-time-yesterday, same-time-last-week, pinned"]
    # snapshot header shows the baseline that was used
    st = store(tmp_path)
    m = st.get_json(f"{SID}/manifest.json")
    m["baseline"] = {"snapshot": "postgres/demo/2026-11-05/20261105T090000Z", "policy": "same-time-yesterday", "reason": "same time yesterday (20261105T090000Z, 3 min off target)"}
    st.put_json(f"{SID}/manifest.json", m)
    snap = App(config(tmp_path), st, False, None).snapshot(SID).decode()
    assert "vs <a class='mono' href='/s/postgres/demo/2026-11-05/20261105T090000Z' title='same time yesterday (20261105T090000Z, 3 min off target)'>09:00:00</a> (same time yesterday)" in snap


def test_run_lock_and_gates(tmp_path):
    app = App(config(tmp_path), store(tmp_path), allow_run=False, token=None)
    assert app.start_run("demo", "adhoc", "", True)[0] == 403
    app.allow_run = True
    assert app.start_run("nope", "adhoc", "", True)[0] == 400
    assert app.start_run("demo", "weird", "", True)[0] == 400
    gate = threading.Event()
    app._run = lambda req, use_llm: gate.wait(5)  # pretend a run is in flight
    assert app.start_run("demo", "adhoc", "r", False)[0] == 202
    assert app.start_run("demo", "adhoc", "r", False)[0] == 409
    gate.set()


@pytest.fixture
def server(tmp_path):
    from dbtriage.web import make_server
    from tests.conftest import free_port, wait_for

    srv = make_server(config(tmp_path, page_size=10), store(tmp_path), "127.0.0.1", free_port(), False, "s3cret", True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    yield url
    srv.shutdown()
    srv.server_close()


def get(url, auth=True, method="GET", data=None):
    headers = {"Authorization": "Basic " + base64.b64encode(b"x:s3cret").decode()} if auth else {}
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    with urllib.request.urlopen(req) as r:
        return r.status, r.headers, r.read()


def test_server_auth_api_and_errors(server):
    with pytest.raises(HTTPError) as e:
        get(server + "/", auth=False)
    assert e.value.code == 401
    assert get(server + "/health", auth=False)[0] == 200  # health needs no auth
    status, headers, body = get(server + "/")
    assert status == 200 and headers["Cache-Control"] == "no-store" and b"Targets" in body
    assert get(server + "/", method="HEAD")[2] == b""
    targets = json.loads(get(server + "/api/targets")[2])
    assert targets[0]["name"] == "demo" and targets[0]["latest"]["severity"] == "high" and targets[0]["links"][0]["label"] == "Grafana"
    snaps = json.loads(get(server + "/api/targets/demo/snapshots?limit=1")[2])
    assert snaps["total"] == 2 and len(snaps["snapshots"]) == 1 and snaps["snapshots"][0]["id"] == SID
    detail = json.loads(get(server + f"/api/snapshots/{SID}")[2])
    assert detail["triage"]["title"] == "Bad query" and detail["top_sql"][0]["sql_id"] == "q1" and "awr.txt" in detail["files"]
    assert get(server + f"/api/snapshots/{SID}/files/awr.txt")[2] == b"WORKLOAD REPOSITORY"
    with pytest.raises(HTTPError) as e:
        get(server + f"/api/snapshots/{SID}/files/../../etc/passwd")
    assert e.value.code in (400, 404)
    with pytest.raises(HTTPError) as e:
        get(server + "/api/snapshots/nope/zzz")
    assert e.value.code == 404 and json.loads(e.value.read())["error"] == "not found"
    with pytest.raises(HTTPError) as e:
        get(server + "/api/run", method="POST", data=b'{"target": "demo"}')
    assert e.value.code == 403
    with pytest.raises(HTTPError) as e:
        get(server + "/s/does/not/exist")
    assert e.value.code == 404 and b"<html" in e.value.read()


def test_server_settings_api(server):
    status, _, body = get(f"{server}/api/settings")
    assert status == 200 and json.loads(body)["targets"]["demo"]["dsn"] == "dsn" and json.loads(body)["editable"]
    status, _, body = get(f"{server}/api/targets/my2", method="PUT", data=json.dumps({"engine": "mysql", "dsn": "mysql://u:p@h/d", "links": [{"label": "x", "url": "https://x"}]}).encode())
    assert status == 200 and json.loads(body)["dsn"] == "mysql://u:***@h/d"
    assert "my2" in json.loads(get(f"{server}/api/targets")[2]).__repr__()
    with pytest.raises(HTTPError) as e:
        get(f"{server}/api/targets/my3", method="PUT", data=b'{"engine": "mysql"}')
    assert e.value.code == 400
    status, _, body = get(f"{server}/api/targets/my2/probe", method="POST", data=b"")
    assert status == 200 and json.loads(body)["ok"] is False
    status, _, _ = get(f"{server}/api/targets/my2", method="DELETE")
    assert status == 200 and "my2" not in json.loads(get(f"{server}/api/settings")[2])["targets"]
    status, _, body = get(f"{server}/settings")
    assert status == 200 and b"Look and feel" in body
    assert json.loads(get(f"{server}/api/targets/demo/trend?metric=xact_commit")[2])["metric"] == "xact_commit/s"
    assert len(json.loads(get(f"{server}/api/targets/demo/sql/q1")[2])["history"]) == 2 and b"<polygon" in get(f"{server}/t/demo/sql/q1")[2]
    status, _, body = get(f"{server}/api/settings", method="PUT", data=b'{"brand": "Via API"}')
    assert json.loads(body)["web"]["brand"] == "Via API" and b"<span>Via API</span>" in get(f"{server}/")[2]


def test_settings_agent_and_delivery_cards(tmp_path, monkeypatch):
    import dbtriage.deliver as dl
    import dbtriage.llm as llm

    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    s = app.settings_page().decode()
    assert "Agent (model" in s and "<option value='bedrock-converse'>" in s and "Test model" in s
    assert "<legend>ntfy</legend>" in s and "name='slack.webhook_url' type=password" in s and "Send test via teams" in s
    # delivery form round trip: secrets masked on display, kept when blank
    data = app.parse_delivery_form({"channels": ["stdout", "slack"], "min_severity": ["low"], "slack.webhook_url": ["https://hooks.slack.com/a/b"], "email.to": ["x@y"]})
    assert app.settings.put_delivery(data) == []
    app.reload()
    assert app.cfg.delivery["channels"] == ["stdout", "slack"] and app.api_settings()["delivery"]["slack"]["webhook_url"] == "https://hooks.slack.com/***"
    s = app.settings_page().decode()
    assert "placeholder='https://hooks.slack.com/***'" in s and "hooks.slack.com/a/b" not in s
    # send test goes through the channel with the latest stored review
    sent = []
    monkeypatch.setitem(dl.CHANNELS, "slack", lambda cfg, t, md: sent.append((cfg, t["title"], md)))
    assert app.test_delivery("slack") == (True, "sent via slack") and sent[0][1] == "Bad query" and "evidence" in sent[0][2]
    monkeypatch.setitem(dl.CHANNELS, "slack", lambda cfg, t, md: (_ for _ in ()).throw(OSError("boom")))
    assert app.test_delivery("slack") == (False, "slack: OSError: boom")
    # test model uses the effective agent config plus form overrides
    seen = {}

    class Fake:
        name = "fake"
        def __init__(self, cfg): seen.update(cfg)
        def run(self, *a, **k): return llm.AgentResult(text="OK", stop_reason="stop", model="m")

    monkeypatch.setattr(llm, "make_backend", lambda cfg: Fake(cfg))
    assert app.settings.put_agent({"provider": "ollama", "model": "qwen3:8b"}) == []
    app.reload()
    ok, msg = app.test_agent({"model": "other", "region": ""})
    assert ok and seen == {"provider": "ollama", "model": "other"} and "answered 'OK'" in msg


def test_server_agent_delivery_api(server, monkeypatch):
    status, _, body = get(f"{server}/api/settings", method="PUT",
                          data=json.dumps({"agent": {"provider": "groq", "model": "llama"}, "delivery": {"channels": ["stdout", "ntfy"], "ntfy": {"url": "https://ntfy.sh", "topic": "t"}}}).encode())
    d = json.loads(body)
    assert status == 200 and d["agent"] == {"provider": "groq", "model": "llama"} and d["delivery"]["ntfy"] == {"topic": "t"}  # the ntfy.sh default is not stored
    with pytest.raises(HTTPError) as e:
        get(f"{server}/api/settings", method="PUT", data=b'{"agent": {"provider": "nope"}}')
    assert e.value.code == 400
    status, _, body = get(f"{server}/api/settings/delivery/nope/test", method="POST", data=b"")
    assert status == 200 and json.loads(body) == {"ok": False, "message": "unknown channel 'nope'"}
    status, _, body = get(f"{server}/api/settings/agent/test", method="POST", data=b'{"provider": "openrouter", "api_key_env": "DBTRIAGE_TEST_NO_SUCH_KEY"}')
    assert status == 200 and json.loads(body)["ok"] is False and "DBTRIAGE_TEST_NO_SUCH_KEY" in json.loads(body)["message"]
    status, _, body = get(f"{server}/settings")
    assert b"<option value='groq' selected>" in body and b"<legend>ntfy</legend>" in body


def test_discuss_links_and_chat_api(tmp_path, monkeypatch):
    from dbtriage import llm

    class Fake:
        name = "fake"
        def __init__(self, cfg): pass
        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            assert all(t.name != "submit_review" for t in tools)
            return llm.AgentResult(text="**yes** - because `q1`", stop_reason="end_turn", model="m", usage={"input_tokens": 3, "output_tokens": 2}, tool_calls=0)

    monkeypatch.setattr(llm, "make_backend", lambda cfg: Fake(cfg))
    st = store(tmp_path)
    st.put_json(f"{SID}/triage.json", {"severity": "high", "title": "Bad query", "source": "rules",
                                       "findings": [{"severity": "high", "where": "q1", "summary": "Bad query", "impact": "", "evidence": [], "fix": ""}]})
    st.put_text(f"{SID}/triage.md", "**1 finding**\n\n1. Bad query\n   - **Fix:** x\n")
    off = App(config(tmp_path), st, allow_run=False, token=None)
    assert "data-chat='finding-1'" not in off.snapshot(SID).decode() and off.chat_ask(SID, "review", "hi")[0] == 403
    app = App(config(tmp_path), st, allow_run=False, token=None, allow_chat=True)
    page = app.snapshot(SID).decode()
    assert f"data-chat='finding-1' href='/api/snapshots/{SID}/chat/finding-1'" in page and "id='discuss'" in page and "data-chat='finding-2'" not in page
    assert f"data-chat='sql-q1' href='/api/snapshots/{SID}/chat/sql-q1'" in page  # every top-SQL row gets its own thread
    assert app.chat_thread(SID, "finding-1")["messages"] == []
    code, ans = app.chat_ask(SID, "finding-1", "is it bad?")
    assert code == 200 and ans["html"] == "<p><b>yes</b> - because <code>q1</code></p>" and "3 in / 2 out tokens" in ans["footer"]
    assert "Discuss · 1</a>" in App(config(tmp_path), st, allow_run=False, token=None, allow_chat=True).snapshot(SID).decode()
    t = app.chat_thread(SID, "finding-1")
    assert [m["role"] for m in t["messages"]] == ["user", "assistant"] and t["messages"][1]["html"].startswith("<p><b>yes</b>")
    assert app.chat_ask(SID, "finding-5", "x")[0] == 400 and app.chat_ask(SID, "review", "")[0] == 400
    assert "no_such_key" not in page


def test_server_chat_routes(server):
    status, _, body = get(f"{server}/api/snapshots/{SID}/chat/review")
    assert status == 200 and json.loads(body)["messages"] == []
    with pytest.raises(HTTPError) as e:
        get(f"{server}/api/snapshots/{SID}/chat/../x")
    assert e.value.code in (400, 404)
    with pytest.raises(HTTPError) as e:  # the fixture server runs without --allow-chat
        get(f"{server}/api/snapshots/{SID}/chat/review", method="POST", data=b'{"question": "hi"}')
    assert e.value.code == 403
    status, _, body = get(f"{server}/health")
    assert json.loads(body)["chat_enabled"] is False


def test_schedule_card_and_api(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    app = App(cfg, store(tmp_path), allow_run=True, token=None, allow_edit=True)
    page = app.settings_page().decode()
    assert "<h2>Schedule</h2>" in page and "<option value='' selected>not scheduled</option>" in page and "no scheduler in this process" in page
    assert app.save_schedule("demo", {"schedule": "every 15m", "schedule_window": "30", "schedule_paused": False}) == []
    assert app.save_schedule("nope", {"schedule": "hourly"}) == ["unknown target 'nope'"]
    row = app.schedule_rows()[0]
    assert row["label"] == "every 15 minutes" and row["window"] == 30 and row["next"] is not None
    page = app.settings_page().decode()
    assert "<option value='*/15 * * * *' selected>every 15 minutes</option>" in page and "value=pause>Pause</button>" in page
    assert "every 15 minutes</a>" in app.target("demo", 0).decode()
    # editing the target in its form keeps the schedule
    assert app.save_target("demo", {"engine": "postgres", "dsn": "dsn2", "top_n": 10}) == []
    assert app.cfg.targets["demo"].extra["schedule"] == "*/15 * * * *" and app.cfg.targets["demo"].dsn == "dsn2"
    # custom cron shows the text box, paused shows resume
    assert app.save_schedule("demo", {"schedule": "7 4 * * *", "schedule_paused": True}) == []
    page = app.settings_page().decode()
    assert "<option value='custom' selected>" in page and "value='7 4 * * *'" in page and "value=resume>Resume</button>" in page and "<tr class=paused>" in page
    assert "paused" in app.target("demo", 0).decode()
    # in-process scheduler fires through start_run
    seen = []
    monkeypatch.setattr(app, "start_run", lambda *a, **k: seen.append((a, k)) or (202, {"accepted": True}))
    app.start_scheduler(); app.scheduler.stop.set()
    # resume and reset the window explicitly: a schedule save changes only what it is given (the card sends all three)
    assert app.save_schedule("demo", {"schedule": "every 1m", "schedule_window": "", "schedule_paused": False}) == []
    from datetime import datetime, timedelta
    app.scheduler.tick(datetime.now().astimezone() + timedelta(minutes=2))
    assert seen and seen[0][0][:2] == ("demo", "scheduled") and seen[0][1] == {"window_minutes": 60, "source": "schedule"}
    assert "scheduler running in this process" in app.settings_page().decode()


def test_server_schedule_routes(server):
    status, _, body = get(f"{server}/api/schedule")
    d = json.loads(body)
    assert status == 200 and d["scheduler"] is False and d["targets"][0] == {"target": "demo", "schedule": "", "label": "not scheduled", "window": 60,
                                                                           "paused": False, "next": None, "last": None, "last_status": ""}
    status, _, body = get(f"{server}/api/schedule/demo", method="POST", data=b'{"schedule": "every 6h", "schedule_window": 120}')
    assert status == 200 and json.loads(body)["label"] == "every 6 hours" and json.loads(body)["window"] == 120
    with pytest.raises(HTTPError) as e:
        get(f"{server}/api/schedule/demo", method="POST", data=b'{"schedule": "every 7h"}')
    assert e.value.code == 400
    with pytest.raises(HTTPError) as e:  # fixture server has no --allow-run
        get(f"{server}/api/schedule/demo/run", method="POST", data=b"")
    assert e.value.code == 403
    status, _, body = get(f"{server}/schedule/save", method="POST", data=b"name=demo&schedule=custom&schedule_custom=0+9+*+*+1&schedule_window=45&action=save")
    assert status == 200 and b"demo: cron 0 9 * * 1" in body
    status, _, body = get(f"{server}/schedule/save", method="POST", data=b"name=demo&schedule=0+9+*+*+1&schedule_window=45&action=pause")
    assert status == 200 and b"(paused)" in body and b"value=resume>Resume</button>" in body


def test_finding_cell_status_and_compare_search(tmp_path):
    from dbtriage.web import finding_cell, status_of

    assert finding_cell("New heavy SQL takes 53% of SQL time", "sql_id d8vkav3yzdfuz") == "<div class='finding'><span>New heavy SQL takes <b>53%</b> of SQL time</span><code class='idchip' title='d8vkav3yzdfuz'>d8vkav3yzdfuz</code></div>"
    assert "293bbb76…905968b" in finding_cell("New heavy SQL 293bbb76350e7dfa6f6db911f2905968b") and "293bbb76350e7dfa6f6db911f2905968b</span>" not in finding_cell("New heavy SQL 293bbb76350e7dfa6f6db911f2905968b")
    assert finding_cell(None) == "<span class='mut'>not triaged</span>" and "<script" not in finding_cell("<script>x</script> 10%")
    assert status_of("info") == "<span class='status info'><i class='dot'></i>healthy</span>" and "incident" in status_of("high") and "degraded" in status_of("medium")
    st = store(tmp_path)
    st.put_json(f"{SID}/changes.json", [  # the window is the minute to 10:00; a deploy 10 minutes before it sits beside the settings change
        {"time": "2026-11-05T09:50:00+00:00", "kind": "deploy", "description": "billing@1.4.2 to production", "link": "https://github.com/acme/billing/commit/abc", "source": "github"},
        {"time": "2026-11-05T09:59:00+00:00", "kind": "other", "description": "github: HTTP Error 401", "link": None, "source": "github", "error": True},
        {"time": "2026-11-05T08:00:00+00:00", "kind": "deploy", "description": "<early>", "link": "javascript:alert(1)", "source": "file"}])
    app = App(config(tmp_path), st, False, None)
    page = app.compare_page("demo", None, None).decode()
    assert "<h1>Compare</h1>" in page and page.count("<option value='postgres/demo/2026-11-05/20261105T100000Z'") == 2 and "selected>2026-11-05 10:00:00" in page
    assert "Heuristic findings" in page and "<figure class='chart'>" in page and "Baseline</span>" in page
    assert "<h2>Settings changed</h2>" in page and "<td><code>work_mem</code></td><td class=mono>4MB</td><td class=mono>64MB</td>" in page and "<code>jit</code>" not in page
    assert "<h2>Changes</h2><span class='mut'>1</span>" in page and "<td class=mono>2026-11-05 09:50:00</td><td>deploy</td>" in page
    assert "<a href='https://github.com/acme/billing/commit/abc' rel='noopener' target='_blank'>billing@1.4.2 to production</a>" in page
    assert "HTTP Error 401" not in page and "early" not in page  # a feed error and an old item are not changes beside this window
    assert "changes.json" in app.snapshot_detail(SID)["files"] and f"href='/f/{SID}/changes.json'" in app.snapshot(SID).decode()  # the file drawer offers it
    assert "pick two different snapshots" in app.compare_page("demo", SID, SID).decode()
    s = app.search("dem").decode()
    assert "<a class='btn' href='/t/demo'>demo</a>" in s and "Nothing matched" not in s
    assert "<code>q1</code>" not in app.search("q1").decode() and "Type at least 3 characters" in app.search("q1").decode()
    assert "Nothing matched" in app.search("zzz").decode()
    assert "<h2>Snapshots</h2>" in app.search("20261105T1000").decode()


def test_server_compare_search_and_filter(server):
    assert get(f"{server}/t/demo/compare")[0] == 200 and get(f"{server}/search?q=demo")[0] == 200
    status, _, body = get(f"{server}/t/demo?filter=scheduled")
    assert status == 200 and b"class=ts href='/s/postgres/demo/2026-11-05/20261105T100000Z'" not in body and b"class=on>Scheduled</a>" in body
    with pytest.raises(HTTPError) as e:
        get(f"{server}/t/nope/compare")
    assert e.value.code == 404


def test_chart_text_is_themed(tmp_path):
    from dbtriage.web import CSS
    # every chart svg sits in figure.chart; its text must take theme colours, never the browser default black
    assert "figure.chart svg text{" in CSS.replace("svg.chart text,", "") and "figure.chart svg text.lbl" in CSS and "figure.chart svg tspan.mut" in CSS


def test_theme_toggle_and_palettes(tmp_path):
    from dbtriage.web import CSS
    idx = App(config(tmp_path), store(tmp_path), False, None).index().decode()
    assert "id=\"theme-toggle\"" in idx and "localStorage.getItem('dbtriage.theme')" in idx and "class=\"sun\"" in idx and "class=\"moon\"" in idx
    assert "--bg:#f5f6f8" in idx and "--card:#ffffff" in idx and "--high:#c42f2f" in idx  # the design's light palette
    assert ":root[data-theme=dark]{--bg:#0f1115;--card:#13161c" in CSS



def test_snapshot_detail_review_card_and_discuss_panel(tmp_path, monkeypatch):
    from dbtriage import llm

    st = store(tmp_path)
    st.put_json(f"{SID}/triage.json", {"severity": "info", "title": "no significant change", "source": "rules", "confidence": "medium",
                                       "summary": "Nothing crossed a rule threshold.", "findings": [],
                                       "checked": "load profile, top SQL, wait classes, sessions, tables against the previous snapshot (rules only)",
                                       "agent_error": "TypeError: \"Could not resolve authentication method. Expected one of api_key\""})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    app = App(config(tmp_path), st, allow_run=False, token=None, allow_chat=True)
    page = app.snapshot(SID).decode()
    assert "<h2 id='rev-h'>no significant change</h2>" in page and "<span class='ricon info'" in page and "<span class='tag'>rules-based</span>" in page
    assert "<span class='tag'>confidence medium</span>" in page and "AI reviewer unavailable, rules-based review shown." in page and "<details><summary>details</summary>" in page
    assert "<span class='lbl'>Checked</span><span class='pill2'>load profile</span>" in page and "<span class='pill2'>tables</span>" in page and "rules only" not in page.split("Checked")[1].split("</div>")[0]
    assert "<div class='filesrow'><span>Files</span>" in page and "Discuss with AI</button>" in page and "data-ready='0'" in page and " disabled>Send" in page
    assert "Model provider not configured (no ANTHROPIC_API_KEY" in page and "<template id='discuss-intro'>" in page and "data-key='sql-q1'" in page
    assert "<a class='askbtn' data-chat='sql-q1'" in page and "10:00:00 vs" not in page and "10:00:00, no baseline" in page
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert "data-ready='1'" in app.snapshot(SID).decode() and "Model provider not configured" not in app.snapshot(SID).decode()
    assert llm.configured({"provider": "ollama"}) == (True, "") and llm.configured({"provider": "groq", "api_key_env": "NOPE_NOT_SET"})[0] is False
    assert llm.configured({"provider": "bedrock-converse"})[0] is True

    class Fake:
        name = "fake"
        def __init__(self, cfg): pass
        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            assert "The question is about SQL id q1" in task and '"text": "select 1"' in task
            return llm.AgentResult(text="it selects one", stop_reason="end_turn", model="m")

    monkeypatch.setattr(llm, "make_backend", lambda cfg: Fake(cfg))
    code, ans = app.chat_ask(SID, "sql-q1", "what is it?")
    assert code == 200 and ans["text"] == "it selects one" and app.chat_thread(SID, "sql-q1")["messages"][0]["text"] == "what is it?"
    assert app.chat_ask(SID, "sql-nope", "x")[0] == 400


def test_base_script_has_no_duplicate_function_names(tmp_path):
    import re as _re
    page = App(config(tmp_path), store(tmp_path), False, None, allow_chat=True).snapshot(SID).decode()
    names = _re.findall(r"function (\w+)\(", page)
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)


def test_agent_reason_is_plain():
    from dbtriage.web import agent_reason
    assert agent_reason('TypeError: "Could not resolve authentication method. Expected one of api_key"') == "No credentials configured for the model provider."
    assert agent_reason("ConnectError: connection refused") == "The model provider could not be reached."
    assert agent_reason("weird\nsecond line") == "weird"


def test_agent_model_picker(tmp_path, monkeypatch):
    from dbtriage import llm

    seen = []

    def fake(cfg, ttl_s=300):
        seen.append(dict(cfg))
        return (["openai/gpt-oss-120b", "llama-3.3-70b-versatile"], "") if cfg["provider"] == "groq" else ([], "no list")

    monkeypatch.setattr(llm, "list_models", fake)
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    assert app.settings.put_agent({"provider": "groq", "model": "gpt-oss-120b", "api_key_env": "GROQ_API_KEY"}) == []
    app.reload()
    ids, note, prov = app.agent_models({"provider": "groq"})
    assert prov == "groq" and "openai/gpt-oss-120b" in ids and seen[-1]["api_key_env"] == "GROQ_API_KEY"
    app.agent_models({"provider": "openrouter", "api_key_env": "GROQ_API_KEY"})  # carried over from the saved provider
    assert seen[-1] == {"provider": "openrouter"}
    app.agent_models({"provider": "openrouter", "api_key_env": "MY_OR_KEY"})   # typed by the user: kept
    assert seen[-1] == {"provider": "openrouter", "api_key_env": "MY_OR_KEY"}
    page = app.settings_page().decode()
    assert 'list="model-list"' in page and 'id="a-provider"' in page and "/api/settings/agent/models?" in page


def test_list_models_filters_and_caches(monkeypatch):
    from dbtriage import llm
    from dbtriage.llm import openai_backend

    calls = []

    class FakeModels:
        def list(self):
            calls.append(1)
            return [type("M", (), {"id": i})() for i in ("whisper-large-v3", "openai/gpt-oss-120b", "llama-guard-4", "qwen3-32b")]

    class FakeClient:
        models = FakeModels()
        def with_options(self, **kw): return self

    monkeypatch.setattr(openai_backend.OpenAIBackend, "__init__", lambda self, cfg, client=None: setattr(self, "client", FakeClient()))
    llm._MODEL_CACHE.clear()
    assert llm.list_models({"provider": "groq"}) == (["openai/gpt-oss-120b", "qwen3-32b"], "")
    assert llm.list_models({"provider": "groq"})[0] == ["openai/gpt-oss-120b", "qwen3-32b"] and len(calls) == 1  # cached
    assert llm.list_models({"provider": "vertex-gemini"})[0] == [] and "type an id" in llm.list_models({"provider": "vertex-gemini"})[1]
    monkeypatch.setattr(openai_backend.OpenAIBackend, "__init__", lambda self, cfg, client=None: (_ for _ in ()).throw(SystemExit("[agent] X is not set")))
    assert llm.list_models({"provider": "openrouter"}) == ([], "[agent] X is not set")


def test_settings_forms_save_in_place(tmp_path):
    app = App(config(tmp_path), store(tmp_path), True, None, allow_edit=True)
    page = app.settings_page().decode()
    assert 'action="/settings/save" class="grid" data-ajax="reload"' in page and 'action="/settings/agent" class="grid" data-ajax>' in page
    assert 'action="/settings/delivery" class="grid" data-ajax>' in page and "class=inline data-ajax=refresh-card>" in page and "form='sched-demo' name=schedule" in page
    assert 'id="toasts"' in page and "new FormData(form, btn)" in page
    # the response the script reads: a notice inside main, with good/bad on it
    ok = app.settings_page("agent settings saved", "good").decode()
    assert "<div class='notice good'>agent settings saved</div>" in ok


def test_old_review_shapes_first_snapshots_and_error_page(tmp_path):
    from dbtriage.web import normalize_finding

    old = {"severity": "low", "kind": "engine_finding", "title": "There was no significant database activity", "detail": "",
           "evidence": {"engine_finding": {"finding": "normal, successful completion"}}, "score": 1}
    f = normalize_finding(old)
    assert f["summary"] == "There was no significant database activity" and f["where"] == "engine finding" and f["evidence"][0].startswith("engine_finding: {")
    assert normalize_finding({"summary": "s", "where": "w", "evidence": ["a"], "detail": "d"})["evidence"] == ["d", "a"]
    st = store(tmp_path)
    first = "postgres/demo/2026-11-05/20261105T090000Z"
    st.put_json(f"{first}/triage.json", {"severity": "info", "title": "no significant change", "source": "rules", "findings": [old]})
    st._p(f"{first}/compare.json").unlink()
    st.put_json(f"{first}/summary.json", {"captured_at": "2026-11-05T09:00:00+00:00", "metrics_kind": "cumulative",
                                          "top_sql": [{"sql_id": "a1", "total_time_ms": 30, "calls": 1, "text": "select a"},
                                                      {"sql_id": "b2", "total_time_ms": 70, "calls": 2, "text": "select b"}]})
    page = App(config(tmp_path), st, False, None).snapshot(first).decode()
    assert "There was no significant database activity" in page and "engine_finding:" in page  # an old review renders
    assert "<h2 id='sql-h'>Top SQL</h2><span class='mut'>since statistics were last reset; no baseline yet</span>" in page
    assert page.index("select b") < page.index("select a") and "70%" in page  # ranked by time, share computed
    assert "<table class='list topsql sortable'>" in page and "data-v='0.7'" in page and "data-v='2.0'" in page  # sorts by the raw values
    err = App(config(tmp_path), st, False, None).error_page(404, "nothing here").decode()
    assert "<h1>Not found</h1><span class='chip'>404</span>" in err and "nothing here" in err and "Back to the overview" in err


def test_sort_value_is_the_raw_number_or_empty():
    assert sort_value(31337) == "31337.0" and sort_value(0.54) == "0.54" and sort_value("12.5") == "12.5" and sort_value(0) == "0.0"
    assert [sort_value(v) for v in (None, True, "n/a", float("nan"), float("inf"), {})] == [""] * 6


def _req(url, method="GET", data=None, headers=None, auth=True):
    h = {"Authorization": "Basic " + base64.b64encode(b"x:s3cret").decode()} if auth else {}
    h.update(headers or {})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h)) as r:
            return r.status, r.read()
    except HTTPError as e:
        return e.code, e.read()


def test_cross_site_requests_are_refused(server):
    body = json.dumps({"provider": "openrouter", "base_url": "http://127.0.0.1:9/v1"}).encode()
    foreign = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site", "Content-Type": "text/plain"}
    # a page on another site cannot make the server call out with the provider key, change settings or start work
    assert _req(f"{server}/api/settings/agent/test", "POST", body, foreign)[0] == 403
    assert _req(f"{server}/api/settings/agent/models?provider=openrouter&base_url=http://127.0.0.1:9/v1", headers=foreign)[0] == 403
    assert _req(f"{server}/api/settings", "PUT", b'{"web": {"brand": "pwned"}}', foreign)[0] == 403
    assert _req(f"{server}/settings/save", "POST", b"brand=pwned", {"Origin": "https://evil.example"})[0] == 403
    assert _req(f"{server}/api/settings", "PUT", b'{"web": {"brand": "pwned"}}', {"Origin": "null"})[0] == 403
    assert _req(f"{server}/api/settings", "PUT", b'{"web": {"brand": "x"}}', {"Sec-Fetch-Site": "same-site"})[0] == 403  # another port on this host
    # a forged Host (DNS rebinding) is refused even for reads
    assert _req(f"{server}/api/targets", headers={"Host": "evil.example:8081"})[0] == 403
    # the site itself and command-line clients still work
    host = server.split("//", 1)[1]
    assert _req(f"{server}/api/settings", "PUT", b'{"web": {"brand": "ok"}}', {"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})[0] == 200
    assert _req(f"{server}/api/settings", "PUT", b'{"web": {"brand": "ok2"}}')[0] == 200
    # links into dbtriage from Slack or email still open
    assert _req(f"{server}/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[0] == 200


def test_health_hides_locations_without_the_password(server):
    code, body = _req(f"{server}/health", auth=False)
    assert code == 200 and set(json.loads(body)) == {"ok", "store_ok", "settings_ok", "spooled"}
    code, body = _req(f"{server}/health")
    assert code == 200 and "store" in json.loads(body) and "settings" in json.loads(body)


def test_model_test_needs_edit_rights(tmp_path):
    from dbtriage.web import make_server
    from tests.conftest import free_port, wait_for

    srv = make_server(config(tmp_path), store(tmp_path), "127.0.0.1", free_port(), False, None, False)  # read-only
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    try:
        assert _req(f"{url}/api/settings/agent/test", "POST", b'{"base_url": "http://127.0.0.1:9/v1"}', auth=False)[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_env_file_permissions_warning(tmp_path, capsys, monkeypatch):
    from dbtriage import envfile

    monkeypatch.delenv("DBTRIAGE_ENV_FILE", raising=False)
    monkeypatch.delenv("DBTRIAGE_NO_ENV_FILE", raising=False)
    (tmp_path / "targets.toml").write_text("")
    f = tmp_path / ".env"
    f.write_text("DBT_PERM_TEST=1\n")
    f.chmod(0o644)
    envfile.init(tmp_path / "targets.toml")
    assert "can be read by other users (mode 644)" in capsys.readouterr().err
    f.chmod(0o600)
    envfile.init(tmp_path / "targets.toml")
    assert "can be read by other users" not in capsys.readouterr().err
    envfile._active = None
    monkeypatch.delenv("DBT_PERM_TEST", raising=False)


def test_in_process_scheduler_thread_runs(tmp_path, monkeypatch):
    """`dbtriage web --schedule` must start its scheduler thread (it once never did) and fire due slots by itself."""
    from dbtriage import scheduler as sch

    monkeypatch.setattr(sch.Scheduler, "__init__", (lambda orig: lambda self, *a, **k: orig(self, *a, **dict(k, tick_s=0.05)))(sch.Scheduler.__init__))
    app = App(config(tmp_path), store(tmp_path), allow_run=True, token=None, allow_edit=True)
    fired = []
    monkeypatch.setattr(app, "start_run", lambda *a, **k: fired.append(a[0]) or (202, {"accepted": True}))
    app.start_scheduler()
    try:
        assert app.scheduler.is_alive()
        assert app.save_schedule("demo", {"schedule": "every 1m"}) == []
        st = app.scheduler.state["demo"]
        from datetime import datetime, timedelta
        st.next = datetime.now().astimezone() - timedelta(seconds=1)  # due now
        for _ in range(100):
            if fired:
                break
            time.sleep(0.02)
        assert fired == ["demo"]
    finally:
        app.scheduler.stop.set()


def test_test_buttons_and_api_key_cmd_need_edit_rights(tmp_path):
    from dbtriage.settings import AGENT_FIELDS

    assert "api_key_cmd" not in AGENT_FIELDS  # runs a command on the server: targets.toml only
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    assert "api_key_cmd" not in app.settings_page().decode()
    seen = {}
    import dbtriage.llm as llm_mod
    orig = llm_mod.test_model
    llm_mod.test_model = lambda cfg: (seen.update(cfg) or (True, "ok"))
    try:
        app.test_agent({"api_key_cmd": "touch /tmp/pwned", "model": "m2"})
    finally:
        llm_mod.test_model = orig
    assert "api_key_cmd" not in seen and seen["model"] == "m2"


def test_read_only_view_refuses_delivery_test(tmp_path):
    from dbtriage.web import make_server
    from tests.conftest import free_port, wait_for

    srv = make_server(config(tmp_path), store(tmp_path), "127.0.0.1", free_port(), False, None, False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    try:
        assert _req(f"{url}/api/settings/delivery/stdout/test", "POST", b"", auth=False)[0] == 403
    finally:
        srv.shutdown()
        srv.server_close()


def test_agent_card_save_keeps_fields_the_card_does_not_have(server):
    """The Agent card has no max_tokens input; saving it must not clear a max_tokens set through the API
    (agent review on PR #1, 45628d8)."""
    assert _req(f"{server}/api/settings", "PUT", json.dumps({"agent": {"provider": "ollama", "model": "m", "max_tokens": 9000}}).encode())[0] == 200
    form = "provider=ollama&model=m2&region=&project_id=&resource=&base_url=&api_key_env=&effort=high&max_iterations=25"
    assert _req(f"{server}/settings/agent", "POST", form.encode())[0] == 200
    agent = json.loads(_req(f"{server}/api/settings")[1])["agent"]
    assert agent["model"] == "m2" and agent["max_tokens"] == 9000


def test_agreement_marks_and_dropped_rules_findings(tmp_path):
    """Both / model only on the model's findings, and the rules findings the model dropped stay on the page."""
    from dbtriage.findings import agreement

    st = store(tmp_path)
    heur = [{"severity": "high", "kind": "blocking", "title": "3 sessions blocked by pid 42", "detail": "", "evidence": {"sessions": [{"pid": 42}]}},
            {"severity": "medium", "kind": "sql_regressed", "title": "q1 regressed 4x", "evidence": {"sql_id": "q1"}},
            {"severity": "low", "kind": "bloat", "title": "public.t has 12% dead rows", "evidence": {"table": "public.t"}}]
    findings = [{"severity": "medium", "kind": "sql_regressed", "subject": "q1", "where": "sql_id q1", "summary": "q1 slowed down", "impact": "i", "evidence": ["e"], "fix": "f"},
                {"severity": "low", "kind": "other", "subject": "", "where": "general", "summary": "Load is up", "impact": "i", "evidence": [], "fix": "f"}]
    tr = {"severity": "medium", "title": "q1 slowed down", "source": "agent", "provider": "ollama", "model": "m", "confidence": "high",
          "findings": findings, "heuristics": heur}
    tr["agreement"] = agreement(tr, heur)
    st.put_json(f"{SID}/triage.json", tr)
    app = App(config(tmp_path), st, False, None)
    page = app.snapshot(SID).decode()
    assert "<span class='mark both'" in page and "<span class='mark model_only'" in page
    assert page.index("<span class='mark both'") < page.index("sql_id q1") < page.index("<span class='mark model_only'") < page.index("Load is up")
    below = page.split("<span class='lbl'>Rules found, not in the review</span>")[1]
    assert "3 sessions blocked by pid 42" in below and "<span class='sev high'>" in below and "<code class='where'>blocking</code>" in below
    assert "<summary>1 low-severity rules finding not in the review</summary>" in below and "public.t has 12% dead rows" in below
    assert "Agreement: 1 both · 1 model only · 2 rules only" in below and "q1 regressed 4x" not in below  # matched: not dropped
    assert "style=" not in page.split("<ol class='flist'>")[1].split("filesrow")[0]  # colours come from the theme tokens
    rows, _ = app.snapshot_rows("demo", 10, 0)
    assert rows[0]["rules_dropped"] == 1 and rows[1]["rules_dropped"] is None  # the older snapshot is a rules-only review
    # a rules-only review says so; a review stored before the marks existed renders with none of this
    st.put_json(f"{SID}/triage.json", {"severity": "medium", "title": "x", "source": "rules", "findings": findings, "heuristics": heur, "agreement": None})
    page = app.snapshot(SID).decode()
    assert "Rules-only review; there is no model review to compare with." in page and "class='mark" not in page and "Rules found, not in the review" not in page
    st.put_json(f"{SID}/triage.json", {"severity": "medium", "title": "x", "source": "agent", "findings": findings, "heuristics": heur})
    page = app.snapshot(SID).decode()
    assert "q1 slowed down" in page and "class='mark" not in page and "class='dropped'" not in page and "Agreement:" not in page
