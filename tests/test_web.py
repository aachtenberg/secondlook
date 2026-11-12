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
    with pytest.raises(HTTPError) as e:  # a pasted result is gated the same way
        get(f"{server}/api/snapshots/{SID}/chat/review/answer", method="POST", data=b'{"proposal": 1, "result": "x"}')
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


def test_proposed_read_answer_route_and_panel_markup(tmp_path, monkeypatch):
    """A proposed read in the model's answer renders as a block with Copy and a paste form; the answer route stores
    the pasted result masked, and the thread renders it as supplied by a person. Same gates as a question."""
    from dbtriage import llm

    class Proposes:
        name = "fake"
        def __init__(self, cfg): pass
        def run(self, system, task, tools, max_iterations=25, require_tool=None, stop_when=None):
            return llm.AgentResult(text="Look for a seq scan:\n\n```proposed-read\nEXPLAIN SELECT 1 FROM t WHERE a = 'b';\n```\n\nthen tell me.",
                                   stop_reason="end_turn", model="m")

    monkeypatch.setattr(llm, "make_backend", lambda cfg: Proposes(cfg))
    st = store(tmp_path)
    off = App(config(tmp_path), st, allow_run=False, token=None)
    assert off.chat_answer(SID, "review", 1, "x")[0] == 403
    app = App(config(tmp_path), st, allow_run=False, token=None, allow_chat=True)
    assert app.chat_answer(SID, "review", 1, "x") == (400, {"error": "this thread has no proposed read 1"})
    code, ans = app.chat_ask(SID, "review", "what should I run?")
    assert code == 200 and ans["proposals"][0]["n"] == 1
    h = ans["html"]
    assert h.startswith("<p>Look for a seq scan:</p>\n<div class='pread' data-proposal='1'><div class='plbl'>Proposed read 1 · run it yourself; nothing runs from here</div>"
                        "<pre><code>EXPLAIN SELECT 1 FROM t WHERE a = &#x27;b&#x27;;</code></pre>")
    assert "<button type='button' class='quiet' data-copy='1'>Copy</button>" in h and "data-answer='1' aria-expanded='false'>Paste the result</button>" in h
    assert "<form class='panswer' data-proposal='1' hidden><label><span class='sr'>Result of proposed read 1</span><textarea name='result'" in h
    assert "<button type='submit' class='ai'>Save result</button>" in h and h.endswith("</form></div>\n<p>then tell me.</p>") and "```" not in h
    code, r = app.chat_answer(SID, "review", "1", "Seq Scan on t\n  Filter: (a = 'b')")
    assert code == 200 and r["role"] == "operator" and r["text"] == "Seq Scan on t\n  Filter: (a = '?')"
    assert r["html"] == ("<div class='plbl'>Result for proposed read 1 · supplied by a person, not measured by Second Look</div>"
                         "<pre class='presult'>Seq Scan on t\n  Filter: (a = &#x27;?&#x27;)</pre>")
    t = app.chat_thread(SID, "review")
    assert [m["role"] for m in t["messages"]] == ["user", "assistant", "operator"] and t["messages"][2]["html"] == r["html"] and "pread" in t["messages"][1]["html"]
    assert app.chat_answer(SID, "review", "one", "x")[0] == 400 and app.chat_answer(SID, "review", 1, "")[0] == 400
    assert app.chat_answer("postgres/demo/2026-11-05/nope", "review", 1, "x")[0] == 404
    assert "'proposed-read" not in app.snapshot(SID).decode()  # the page is untouched; the panel renders threads from the API


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


def test_finding_card_with_and_without_a_change_proposal(tmp_path):
    st = store(tmp_path)
    older = "postgres/demo/2026-11-05/20261105T090000Z"
    p = {"statement": "CREATE INDEX CONCURRENTLY t_a_idx ON public.t (a) <b>;", "rollback": "DROP INDEX CONCURRENTLY t_a_idx;",
         "runner": "DBA in a change window", "window": "tonight", "justified_by": [SID, older]}
    findings = [{"severity": "high", "kind": "seq_scans", "subject": "public.t", "where": "table public.t", "summary": "Full scans", "impact": "i", "evidence": ["e"], "fix": "add an index", "proposal": p},
                {"severity": "low", "kind": "sql_regressed", "subject": "q1", "where": "sql_id q1", "summary": "Slower", "impact": "i", "evidence": [], "fix": "look at q1"}]
    st.put_json(f"{SID}/triage.json", {"severity": "high", "title": "Full scans", "source": "rules", "confidence": "low", "findings": findings})
    page = App(config(tmp_path), st, False, None).snapshot(SID).decode()
    card = page.split("<ol class='flist'>")[1].split("</ol>")[0]
    first, second = card.split("<li><div class='fh'>")[1:]
    assert "<dt>Fix</dt><dd>add an index</dd><dt>Change proposal</dt><dd class='proposal'>" in first and "Change proposal" not in second
    assert "<pre id='proposal-1' class='stmt'>CREATE INDEX CONCURRENTLY t_a_idx ON public.t (a) &lt;b&gt;;</pre>" in first  # escaped, never trusted
    assert "<button type='button' class='copy' data-copy='proposal-1' aria-label='Copy the statement'>Copy</button>" in first
    assert "<dt>Undo</dt><dd>DROP INDEX CONCURRENTLY t_a_idx;</dd>" in first and "<dt>Run by</dt><dd>DBA in a change window · tonight</dd>" in first
    assert f"<a class='mono' href='/s/{SID}' title='{SID}'>20261105T100000Z</a>" in first and f"href='/s/{older}'" in first
    assert "Second Look never applies changes" in first and "style=" not in card and "Apply" not in card  # no apply button, ever
    assert "button[data-copy]" in page and second.endswith("<dt>Fix</dt><dd>look at q1</dd></dl></li>")  # the copy script; the plain card as it was


# --------------------------------------------------------------------------- every knob on the Settings pages
def test_delivery_card_has_gone_after_runs_and_a_gate_per_channel(tmp_path):
    from dbtriage.settings import CHANNEL_FIELDS

    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    s = app.settings_page().decode()
    assert 'name="gone_after_runs" type="number" min="1" max="1000" value=""' in s and "blank = 3" in s
    for ch in CHANNEL_FIELDS:
        if ch != "stdout":
            assert f"name='{ch}.min_severity'" in s, ch
    assert "<option value='true' selected>true</option>" in s.split("name='email.starttls'")[1] and "name='plane.project_identifier'" in s
    form = {"channels": ["stdout", "ntfy", "email"], "min_severity": ["info"], "gone_after_runs": ["5"], "ntfy.min_severity": ["low"],
            "email.starttls": ["false"], "email.to": ["a@x"], "plane.project_identifier": ["OPS"]}
    assert app.settings.put_delivery(app.parse_delivery_form(form), base=app.seed.delivery) == []
    app.reload()
    d = app.cfg.delivery
    assert d["gone_after_runs"] == 5 and d["ntfy"]["min_severity"] == "low" and d["email"]["starttls"] is False and d["plane"]["project_identifier"] == "OPS"
    s = app.settings_page().decode()
    assert 'name="gone_after_runs" type="number" min="1" max="1000" value="5"' in s and "<option value='false' selected>" in s.split("name='email.starttls'")[1]
    assert "<option value='low' selected>" in s.split("name='ntfy.min_severity'")[1].split("</select>")[0]
    # the rendered card round-trips: saving it unchanged stores nothing new
    from tests.test_overlay_semantics import form_as_rendered

    before = json.dumps(app.settings.data["delivery"], sort_keys=True)
    assert app.settings.put_delivery(app.parse_delivery_form(form_as_rendered(s, "/settings/delivery")), base=app.seed.delivery) == []
    assert json.dumps(app.settings.data["delivery"], sort_keys=True) == before
    # blank = the default (3); bad values are refused, not stored
    assert app.settings.put_delivery({"gone_after_runs": ""}) == [] and "gone_after_runs" not in app.settings.data["delivery"]
    assert app.settings.put_delivery({"gone_after_runs": "0"}) == ["gone_after_runs: 1 to 1000"]
    assert app.settings.put_delivery({"gone_after_runs": "x"}) == ["gone_after_runs must be a whole number"]
    assert app.settings.put_delivery({"email": {"starttls": "maybe"}}) == ["email.starttls: true or false"]
    assert app.api_settings()["delivery"]["ntfy"]["min_severity"] == "low"


def test_change_feed_on_the_target_form(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    form = app.target_form("demo").decode()
    assert "<legend>Change feed</legend>" in form and "<option value='' selected>none</option>" in form and 'name="changes.repo"' in form and "Test feed" in form
    # github: the table is saved, changes_file is dropped; fields of other sources are not kept
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["postgres"], "dsn": ["x"], "changes.source": ["github"], "changes.repo": ["acme/billing"],
                                          "changes.environment": ["production"], "changes.token_env": ["GH_TOKEN"], "changes.releases": ["on"],
                                          "changes.lookback_hours": ["24"], "changes.project": ["ignored"], "changes.path": ["ignored"]})
    assert fields["changes"] == {"source": "github", "repo": "acme/billing", "environment": "production", "token_env": "GH_TOKEN", "releases": True, "lookback_hours": 24}
    assert fields["changes_file"] == "" and app.save_target("demo", fields) == []
    t = app.cfg.targets["demo"]
    assert t.extra["changes"]["repo"] == "acme/billing" and t.changes_file is None
    form = app.target_form("demo").decode()
    assert "<option value='github' selected>" in form and 'name="changes.repo" value="acme/billing"' in form and 'name="changes.releases" checked' in form
    assert 'name="changes.lookback_hours" type="number" min="1" max="8760" value="24"' in form
    # a file feed with the default numbers stays the short form, changes_file; with a number of its own it is the table
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["postgres"], "dsn": ["x"], "changes.source": ["file"], "changes.path": ["changes/demo.json"]})
    assert fields["changes_file"] == "changes/demo.json" and fields["changes"] == {}
    assert app.save_target("demo", fields) == [] and app.cfg.targets["demo"].changes_file == "changes/demo.json" and not app.cfg.targets["demo"].extra.get("changes")
    assert "<option value='file' selected>" in app.target_form("demo").decode() and 'name="changes.path" value="changes/demo.json"' in app.target_form("demo").decode()
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["postgres"], "dsn": ["x"], "changes.source": ["file"], "changes.path": ["changes/demo.json"],
                                          "changes.correlate_minutes": ["5"]})
    assert fields["changes_file"] == "" and fields["changes"] == {"source": "file", "path": "changes/demo.json", "correlate_minutes": 5}
    assert app.save_target("demo", fields) == [] and app.cfg.targets["demo"].extra["changes"]["correlate_minutes"] == 5 and app.cfg.targets["demo"].changes_file is None
    # none clears both
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["postgres"], "dsn": ["x"], "changes.source": [""], "changes.path": ["left over"]})
    assert app.save_target("demo", fields) == [] and app.cfg.targets["demo"].changes_file is None and not app.cfg.targets["demo"].extra.get("changes")
    # what the API may send is checked the same way
    put = lambda c: app.settings.put_target("demo", {"engine": "postgres", "dsn": "x", "changes": c})  # noqa: E731
    assert put({"source": "github"}) == ["changes: a github feed needs repo"]
    assert put({"source": "gitlab", "project": "g/p", "token_env": "lower"}) == ["changes.token_env: an environment variable name like GITHUB_TOKEN"]
    assert put({"source": "file", "path": "x", "lookback_hours": 0}) == ["changes.lookback_hours: 1 to 8760 hours"]
    assert put({"source": "jenkins"}) == ["changes.source: one of file, github, gitlab"]
    assert put("x") == ["changes must be a table"]
    assert put({"source": "github", "repo": "a/b", "base_url": "ftp://x"}) == ["changes.base_url must start with http(s)://"]
    assert put({"source": "gitlab", "project": "g/p", "base_url": "https://gitlab.example.com", "correlate_minutes": "15"}) == []
    app.reload()
    assert app.api_settings()["targets"]["demo"]["changes"] == {"source": "gitlab", "project": "g/p", "base_url": "https://gitlab.example.com", "correlate_minutes": 15}


def test_seeded_feed_round_trips_unchanged(tmp_path):
    """A changes_file from targets.toml shows as a file feed, a changes table as its source; saving either form as
    rendered stores nothing, so the file keeps feeding through."""
    from tests.test_overlay_semantics import form_as_rendered

    t = Target("demo", "postgres", "dsn", changes_file=str(tmp_path / "changes.json"))
    g = Target("gh", "postgres", "dsn", extra={"changes": {"source": "github", "repo": "acme/billing", "lookback_hours": 24}})
    app = App(Config(storage_url="x", targets={"demo": t, "gh": g}, base_dir=str(tmp_path)), store(tmp_path), False, None, allow_edit=True)
    for name, source in (("demo", "file"), ("gh", "github")):
        form = form_as_rendered(app.target_form(name).decode(), "/targets/save")
        assert form["changes.source"] == [source] and form["mask_report_literals"] == ["on"] and form["baseline"] == [""]
        n, fields = App.parse_target_form(form)
        assert app.save_target(n, fields) == [] and app.settings.data["targets"][name] == {}, name
    assert app.cfg.targets["demo"].changes_file == str(tmp_path / "changes.json") and app.cfg.targets["gh"].extra["changes"]["lookback_hours"] == 24


def test_test_feed_reads_the_feed_as_the_form_describes_it(tmp_path):
    from datetime import datetime, timedelta, timezone

    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    now = datetime.now(timezone.utc)
    (tmp_path / "changes.json").write_text(json.dumps([
        {"time": (now - timedelta(hours=2)).isoformat(), "kind": "deploy", "description": "billing@1.4.2 to production", "link": "https://ci/1"},
        {"time": (now - timedelta(days=4)).isoformat(), "kind": "deploy", "description": "ancient deploy"}]))
    ok, out = app.test_feed("demo", {"engine": "postgres", "changes_file": "changes.json", "changes": {}})  # relative to targets.toml
    assert ok and "billing@1.4.2 to production" in out and "href='https://ci/1'" in out and "ancient" not in out and "<span class='pill2'>deploy</span>" in out
    ok, out = app.test_feed("demo", {"engine": "postgres", "changes_file": "", "changes": {"source": "file", "path": "missing.json"}})
    assert not ok and "<li class=err>" in out and "missing.json" in out
    ok, out = app.test_feed("demo", {"engine": "postgres", "changes_file": "", "changes": {}})
    assert ok and "no change feed configured" in out
    (tmp_path / "empty.json").write_text("[]")
    ok, out = app.test_feed("new", {"engine": "postgres", "changes_file": "empty.json", "changes": {}})
    assert ok and "no changes in the last 48 hours" in out
    assert "changes" not in app.settings.data["targets"].get("demo", {})  # nothing was saved


def test_server_feed_button_and_files_list(server, tmp_path):
    (tmp_path / "changes.json").write_text("[]")
    form = "name=demo&engine=postgres&changes.source=file&changes.path=changes.json&action=feed"
    status, body = _req(f"{server}/targets/save", "POST", form.encode())
    assert status == 200 and b"change feed read: see below" in body and b"<ul class='feed'>" in body and b"<option value='file' selected>" in body
    status, body = _req(f"{server}/targets/save", "POST", b"name=demo&engine=postgres&changes.source=file&changes.path=nope.json&action=feed")
    assert status == 200 and b"change feed failed" in body and b"<li class=err>" in body and b"nope.json" in body
    assert "changes_file" not in json.loads(_req(f"{server}/api/settings")[1])["targets"]["demo"]  # a test never saves
    status, body = _req(f"{server}/api/settings", "PUT", json.dumps({"web": {"files": ["triage.md", "awr.txt"]}}).encode())
    assert status == 200 and json.loads(body)["web"]["files"] == ["triage.md", "awr.txt"]
    assert json.loads(_req(f"{server}/api/snapshots/{SID}")[1])["files"] == ["triage.md", "awr.txt"]


def test_pinned_baseline_is_picked_from_stored_snapshots(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    pick = lambda page: page.split('<select name="baseline">')[1].split("</select>")[0]  # noqa: E731
    sel = pick(app.target_form("demo").decode())
    assert sel.index("20261105T100000Z") < sel.index("20261105T090000Z") and "<option value='' selected>" in sel  # newest first
    assert "2026-11-05 10:00:00 · postgres/demo/2026-11-05/20261105T100000Z</option>" in sel
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["postgres"], "dsn": ["x"], "baseline_policy": ["pinned"], "baseline": ["postgres/demo/2026-11-05/20261105T090000Z"]})
    assert app.save_target("demo", fields) == [] and app.cfg.targets["demo"].extra["baseline"].endswith("090000Z")
    sel = pick(app.target_form("demo").decode())
    assert "value='postgres/demo/2026-11-05/20261105T090000Z' selected" in sel and sel.count(" selected") == 1
    assert app.save_target("demo", {"baseline": "postgres/demo/2026-01-01/20260101T000000Z"}) == []  # the API may pin a snapshot since deleted
    sel = pick(app.target_form("demo").decode())
    assert "20260101T000000Z (not stored)</option>" in sel and sel.count(" selected") == 1
    assert pick(app.target_form(None).decode()) == "<option value='' selected>(none: pick one for the pinned policy)</option>"  # a new target has none yet


def test_engine_limits_save_and_reload(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    form = app.target_form("demo").decode()
    assert "<legend>Engine limits</legend>" in form and 'name="mask_report_literals" checked' in form
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["oracle"], "dsn": ["h/x"], "call_timeout_ms": ["30000"], "connect_timeout_s": ["5"], "min_snapshot_age_min": ["0"]})
    assert fields["mask_report_literals"] is False and app.save_target("demo", fields) == []
    t = app.cfg.targets["demo"]
    assert t.extra["call_timeout_ms"] == 30000 and t.extra["connect_timeout_s"] == 5 and t.extra["min_snapshot_age_min"] == 0 and t.extra["mask_report_literals"] is False
    t = App(config(tmp_path), store(tmp_path), False, None).cfg.targets["demo"]  # a restart reads the same overlay
    assert t.extra["mask_report_literals"] is False and t.extra["call_timeout_ms"] == 30000
    form = app.target_form("demo").decode()
    assert 'name="call_timeout_ms" type="number" min="1000" max="3600000" step="1000" value="30000"' in form and 'name="mask_report_literals" >' in form
    # ticking the box again and blanking the numbers drops them from the overlay
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["oracle"], "dsn": ["h/x"], "mask_report_literals": ["on"]})
    assert app.save_target("demo", fields) == [] and not any(k in app.settings.data["targets"]["demo"] for k in ("mask_report_literals", "call_timeout_ms", "connect_timeout_s"))
    assert app.settings.put_target("demo", {"engine": "oracle", "dsn": "x", "call_timeout_ms": 10}) == ["call_timeout_ms: 1000 to 3600000 ms"]
    assert app.settings.put_target("demo", {"engine": "oracle", "dsn": "x", "connect_timeout_s": "x"}) == ["connect_timeout_s must be a whole number"]
    assert app.settings.put_target("demo", {"engine": "oracle", "dsn": "x", "mask_report_literals": "yes"}) == ["mask_report_literals must be true or false"]


def test_files_max_tokens_and_fallback_models_on_the_cards(tmp_path, monkeypatch):
    import dbtriage.llm as llm
    from dbtriage.web import DEFAULTS

    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    base = {**DEFAULTS, **app.seed.web}
    s = app.settings_page().decode()
    assert 'name="files" value="triage.md, compare.json, summary.json, manifest.json, raw.json, tools.json, changes.json, awr.txt, addm.txt, statspack.txt"' in s
    assert 'name="max_tokens" type="number" min="1" step="1000" value=""' in s and 'name="fallback_models" value=""' in s and "$files" not in s
    app.settings.put_web({"files": "triage.md, awr.txt"}, base=base); app.reload()
    assert app.web["files"] == ["triage.md", "awr.txt"] and app.snapshot_detail(SID)["files"] == ["triage.md", "awr.txt"]
    assert 'name="files" value="triage.md, awr.txt"' in app.settings_page().decode()
    app.settings.put_web({"files": "*"}, base=base); app.reload()
    files = app.snapshot_detail(SID)["files"]
    assert app.web["files"] == ["*"] and "awr.txt" in files  # every stored report
    assert "triage.md" in files and files.index("triage.md") < files.index("awr.txt")  # and the built-in list first, not only reports
    app.settings.put_web({"files": ""}, base=base); app.reload()
    assert app.web["files"] == DEFAULTS["files"]  # blank: the built-in list
    # the Agent card: max tokens per call, and the fallback chain as a comma-separated line
    assert app.settings.put_agent({"max_tokens": "8000", "fallback_models": "claude-opus-4-8, claude-sonnet-4-5"}) == []
    app.reload()
    assert app.cfg.agent["max_tokens"] == 8000 and app.cfg.agent["fallback_models"] == ["claude-opus-4-8", "claude-sonnet-4-5"]
    s = app.settings_page().decode()
    assert 'name="max_tokens" type="number" min="1" step="1000" value="8000"' in s and 'name="fallback_models" value="claude-opus-4-8, claude-sonnet-4-5"' in s
    assert app.settings.put_agent({"fallback_models": "none"}) == [] and app.settings.data["agent"]["fallback_models"] == []
    app.reload()
    assert 'name="fallback_models" value="none"' in app.settings_page().decode()
    assert app.settings.put_agent({"fallback_models": "", "max_tokens": ""}) == [] and not {"fallback_models", "max_tokens"} & set(app.settings.data["agent"])
    assert app.settings.put_agent({"fallback_models": 5}) == ["fallback_models: model ids separated by commas"]
    seen = {}
    monkeypatch.setattr(llm, "test_model", lambda cfg: (seen.update(cfg) or (True, "ok")))
    app.test_agent({"fallback_models": "a, b", "max_tokens": "100"})
    assert seen["fallback_models"] == ["a", "b"] and seen["max_tokens"] == "100"  # the line is a list by the time a backend sees it


def test_target_page_has_the_diagnosis_card_and_the_ask_dock(tmp_path, monkeypatch):
    app = App(config(tmp_path), store(tmp_path), False, None)
    page = app.target("demo", 0).decode()
    assert "<section class='card diag' aria-labelledby='diag-h'>" in page and page.index("diag-h") < page.index("<div class=\"tiles\">")
    assert "<span class='gaps'><i class='dot'></i>2 setup gaps</span>" in page and "stored data, no model" in page
    assert page.count("<div class='dcell") == 5 and "<i class='dot'></i>Health</span><span class='head'>" in page
    assert "<span class='head'>Nothing tracked yet</span>" in page  # no ledger in the fixture store
    assert "<div class='dcell medium gap'><span class='lbl'><i class='dot'></i>Changes</span><span class='head'>No change feed</span>" in page
    assert "<a href='/targets/demo'>Set one</a>" in page and "<a href='/settings#schedule'>Add schedule</a>" in page
    assert "$diagnosis" not in page and "$ask" not in page and "ask-fab" not in page.split("</style>")[-1]  # chat is off: no dock, no button
    chat = App(config(tmp_path), store(tmp_path), False, None, allow_chat=True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False); monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    off = chat.target("demo", 0).decode()
    assert "<a class='ask-fab off' href='/settings#agent'" in off and "Ask AI: configure a model</a>" in off and "id='discuss'" not in off
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    on = chat.target("demo", 0).decode()
    assert f"<aside id='discuss' class='discuss dock' aria-label='Ask AI about demo' data-base='/api/snapshots/{SID}/chat/' data-key='target' data-dock='1' data-ready='1' hidden>" in on
    assert "<h2>Ask about demo</h2>" in on and "2 snapshots · diagnosis · open findings" in on and "<button type='button' class='ask-fab' id='discuss-open'" in on
    assert "<button type='button' class='ask' data-chat='target'>" in on and "data-key='target'>What deserves attention first" in on or "data-key='target'>Suggest a snapshot schedule" in on
    assert ">R</span><div class='dtext'><p>demo: " in on  # the synopsis comes from the rules
    snap = chat.snapshot(SID).decode()
    assert "ask-fab" not in snap.split("</style>")[-1]  # the snapshot page keeps its Discuss button, no second floating one
    # the ledger and a change feed, when present, feed the cells
    from dbtriage import ledger as led
    st = app.store
    led.save(st, "postgres", "demo", {"entries": [{"kind": "sql_regressed", "subject": "q1", "title": "q1 slow", "severity": "medium", "state": "still_open",
                                                   "first_seen": SID, "first_seen_at": "2026-11-05T09:00:00+00:00", "tickets": {"jira": {"key": "DBA-7"}}}],
                                      "tombstones": [], "updated": SID, "prior": None, "generation": 0})
    st.put_json(f"{SID}/changes.json", [{"time": "2026-11-05T09:55:00+00:00", "kind": "deploy", "description": "billing@2 to production", "link": None, "source": "webhook"}])
    page = app.target("demo", 0).decode()
    assert "<span class='head'>1 finding, worst <span class='sv medium'>medium</span></span>" in page and "ticket DBA-7" in page and f"<a href='/s/{SID}'>oldest since 2026-11-05</a>" in page
    assert "<span class='head'>deploy billing@2 to production</span>" in page and "1 setup gap</span>" in page


def test_band_weeks_is_on_the_target_form_and_the_compare_page_uses_it(tmp_path, monkeypatch):
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["postgres"], "dsn": ["postgresql://x"], "band_weeks": ["8"]})
    assert app.save_target("demo", fields) == []
    assert app.cfg.targets["demo"].extra["band_weeks"] == 8
    form = app.target_form("demo").decode()
    assert 'name="band_weeks" type="number" min="0" max="52" value="8"' in form and "$band_weeks" not in form
    assert app.save_target("demo", dict(fields, band_weeks="99")) != []  # out of range is refused
    seen = {}
    import dbtriage.cli as cli

    def fake_compare_ids(store, engine, target, a, b, how=None, band_weeks=6):
        seen["band_weeks"] = band_weeks
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "compare_ids", fake_compare_ids)  # the page imports it from cli at call time
    app.compare_page("demo", "x", "y")
    assert seen["band_weeks"] == 8  # the compare page honours the target's setting, like the CLI


def test_fleet_digest_schedule_and_channels_save_from_the_delivery_card(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    base = app.seed.delivery
    form = {"channels": ["stdout"], "min_severity": ["info"], "gone_after_runs": [""], "digest_schedule": ["weekly"], "digest_channels": ["stdout, slack"]}
    assert app.settings.put_delivery(app.parse_delivery_form(form), base=base) == []
    app.reload()
    assert app.cfg.delivery["digest"] == {"schedule": "weekly", "channels": ["stdout", "slack"]}
    page = app.settings_page().decode()
    assert 'name="digest_schedule" value="weekly"' in page and 'value="stdout, slack"' in page and "$digest" not in page
    assert app.settings.put_delivery({"digest": {"schedule": "fortnightly", "channels": "stdout"}}, base=base) == ["digest schedule: weekly, daily, or a cron expression"]
    assert app.settings.put_delivery({"digest": {"schedule": "daily", "channels": "pager"}}, base=base) == ["digest channels: unknown channel(s): pager"]
    assert app.settings.put_delivery({"digest": {"schedule": "", "channels": ""}}, base=base) == []
    app.reload()
    assert "digest" not in app.cfg.delivery  # blank = off, and nothing is stored when the file is silent too
    assert app.scheduler is None or app.scheduler.digest is not None  # the web view's scheduler sends it when scheduling is on


def test_query_store_top_sql_counts_as_the_window_on_a_first_snapshot(tmp_path):
    """SQL Server with Query Store keeps a cumulative load profile but its statements are the window's own figures,
    so a first snapshot's top SQL is labelled the window and enters the statement history."""
    from dbtriage.web import _window_top_sql

    assert _window_top_sql({"metrics_kind": "interval"}) and _window_top_sql({"top_sql_kind": "interval"})
    assert not _window_top_sql({}) and not _window_top_sql({"metrics_kind": "cumulative"})


def test_statement_sources_are_on_the_target_form(tmp_path):
    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    form = app.target_form("demo").decode()
    assert "<legend>Where statements come from</legend>" in form and "$top_sql_source_opts" not in form and "$oracle_source_opts" not in form
    assert "<option value='auto' selected>Auto: Query Store when it is on, else the plan cache</option>" in form
    assert "<option value='7' selected>7: and segment statistics</option>" in form  # blank is level 7
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["oracle"], "dsn": ["h/x"], "top_sql_source": ["query_store"],
                                          "oracle_source": ["statspack"], "statspack_level": ["6"]})
    assert app.save_target("demo", fields) == []
    t = app.cfg.targets["demo"]
    assert t.extra["top_sql_source"] == "query_store" and t.extra["oracle_source"] == "statspack" and t.extra["statspack_level"] == 6
    form = app.target_form("demo").decode()
    assert "<option value='statspack' selected>Statspack only</option>" in form and "<option value='6' selected>6: and SQL plans</option>" in form
    for bad, msg in ((("oracle_source", "awr2"), "oracle_source: one of"), (("top_sql_source", "x"), "top_sql_source: one of"),
                     (("statspack_level", "3"), "statspack_level: one of 0, 5, 6, 7, 10")):
        assert any(p.startswith(msg) for p in app.save_target("demo", dict(fields, **dict([bad]))))
    # auto and blank mean the defaults, so going back to them drops the fields from the overlay
    name, fields = app.parse_target_form({"name": ["demo"], "engine": ["oracle"], "dsn": ["h/x"], "top_sql_source": ["auto"], "oracle_source": ["auto"], "statspack_level": [""]})
    assert app.save_target("demo", fields) == []
    t = app.cfg.targets["demo"]
    assert not t.extra.get("top_sql_source") and not t.extra.get("oracle_source") and t.extra.get("statspack_level") in (None, 7)


def test_the_settings_page_lists_the_collectors_own_source_values():
    """The settings module keeps its own copy so the page loads without a database driver; they must not drift."""
    import pytest
    from dbtriage import settings
    from dbtriage.collectors import oracle

    assert settings.ORACLE_SOURCES == oracle.SOURCES and settings.STATSPACK_LEVELS == oracle.STATSPACK_LEVELS
    sqlserver = pytest.importorskip("dbtriage.collectors.sqlserver")
    assert settings.TOP_SQL_SOURCES == sqlserver.TOP_SQL_SOURCES
