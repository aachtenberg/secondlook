"""The Settings page must be able to turn off, clear or unschedule what targets.toml sets, and keep doing so
after a restart; a partial API update must not wipe what it did not mention (review findings F2, F4, F5)."""
from __future__ import annotations

import json

import pytest

from dbtriage.config import load_config
from dbtriage.settings import Settings
from dbtriage.web import App
from dbtriage.storage import LocalStorage

TOML = """
[storage]
url = "file://./data"
[delivery]
channels = ["stdout", "slack"]
min_severity = "low"
[delivery.slack]
webhook_url_env = "SLACK_URL"
[targets.ora]
engine = "oracle"
dsn = "u/p@h:1521/S"
diagnostics_pack = true
create_snapshot = true
notes = "batch at 02:00"
schedule = "0 * * * *"
links = [{label = "Grafana", url = "https://g/x"}]
"""


@pytest.fixture
def conf(tmp_path, monkeypatch):
    monkeypatch.delenv("DBTRIAGE_STORAGE", raising=False)
    monkeypatch.delenv("DBTRIAGE_NO_OVERLAY", raising=False)
    monkeypatch.delenv("DBTRIAGE_SETTINGS", raising=False)
    (tmp_path / "targets.toml").write_text(TOML)
    return tmp_path / "targets.toml"


def app_for(conf):
    """What `dbtriage web` does at start: load_config (overlay applied), then App."""
    cfg = load_config(conf)
    return App(cfg, LocalStorage(conf.parent / "data"), True, None, allow_edit=True)


def form_as_rendered(page: str, action: str) -> dict:
    """What a browser posts from the form with this action as the page renders it: every text input with its value
    (secrets render without one, so ""), each select's selected option (or its first), each textarea's text, and
    the checked boxes."""
    from html.parser import HTMLParser

    card = page[page.index(f'action="{action}"'):]
    card = card[:card.index("</form>")]

    class Form(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fields: dict = {}
            self.select = None  # [name, first value, selected value]
            self.option = None  # attrs of the open <option>, until its text arrives
            self.textarea = None  # [name, text so far]

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "input" and a.get("name"):
                if a.get("type") == "checkbox":
                    if "checked" in a:
                        self.fields.setdefault(a["name"], []).append(a.get("value", "on"))
                else:
                    self.fields[a["name"]] = [a.get("value") or ""]
            elif tag == "select":
                self.select = [a.get("name"), None, None]
            elif tag == "option" and self.select:
                self.option = a
            elif tag == "textarea":
                self.textarea = [a.get("name"), ""]

        def handle_data(self, data):
            if self.textarea is not None:
                self.textarea[1] += data
            elif self.option is not None and self.select:
                value = self.option.get("value", data.strip())
                if self.select[1] is None:
                    self.select[1] = value
                if "selected" in self.option:
                    self.select[2] = value
                self.option = None

        def handle_endtag(self, tag):
            if tag == "select" and self.select:
                name, first, chosen = self.select
                self.fields[name] = [chosen if chosen is not None else first]
                self.select = None
            elif tag == "textarea" and self.textarea:
                self.fields[self.textarea[0]] = [self.textarea[1]]
                self.textarea = None

    p = Form()
    p.feed(card)
    return p.fields


def delivery_form_as_rendered(app) -> dict:
    return form_as_rendered(app.settings_page().decode(), "/settings/delivery")


def form_save(app, name, **changes):
    """Save the target form the way the browser does: the edit page as rendered, with these changes made (False
    unchecks a box or empties a select)."""
    form = form_as_rendered(app.target_form(name).decode(), "/targets/save")
    for k, v in changes.items():
        if v is False:
            form.pop(k, None)
        else:
            form[k] = [str(v)]
    n, fields = App.parse_target_form(form)
    return app.save_target(n, fields)


def test_unchecking_and_clearing_seeded_values_sticks_across_restarts(conf):
    app = app_for(conf)
    assert form_save(app, "ora", diagnostics_pack=False, create_snapshot=False, notes="") == []
    t = app.cfg.targets["ora"]
    assert t.diagnostics_pack is False and not t.extra.get("create_snapshot") and not t.extra.get("notes")
    app = app_for(conf)  # restart
    t = app.cfg.targets["ora"]
    assert t.diagnostics_pack is False and not t.extra.get("create_snapshot") and not t.extra.get("notes")
    # and turning it back on after a restart works too (the seed must be the file, not file + old overlay)
    assert form_save(app, "ora", diagnostics_pack=True) == []
    assert app_for(conf).cfg.targets["ora"].diagnostics_pack is True
    assert form_save(app_for(conf), "ora", diagnostics_pack=False) == []
    assert app_for(conf).cfg.targets["ora"].diagnostics_pack is False


def test_app_seed_is_the_file_alone(conf):
    app = app_for(conf)
    app.settings.put_web({"brand": "From UI"})
    app = app_for(conf)
    assert app.cfg.web.get("brand") == "From UI" and app.seed.web.get("brand") is None


def test_unscheduling_a_toml_schedule(conf):
    app = app_for(conf)
    assert app.cfg.targets["ora"].extra["schedule"] == "0 * * * *"
    assert app.save_schedule("ora", {"schedule": ""}) == []
    assert not app.cfg.targets["ora"].extra.get("schedule")
    assert not app_for(conf).cfg.targets["ora"].extra.get("schedule")  # after a restart too
    rows = {r["target"]: r for r in app_for(conf).schedule_rows()}
    assert rows["ora"]["label"] == "not scheduled"


def test_partial_delivery_update_keeps_the_rest(conf):
    s = Settings(conf.parent / "dbtriage-settings.json")
    assert s.put_delivery({"channels": ["stdout", "ntfy"], "ntfy": {"url": "https://ntfy.sh", "topic": "t"}}) == []
    assert s.put_delivery({"email": {"smtp_host": "smtp.x", "from": "a@x", "to": "b@x"}}) == []  # API: only email
    assert s.data["delivery"]["channels"] == ["stdout", "ntfy"] and s.data["delivery"]["ntfy"]["topic"] == "t"
    assert s.data["delivery"]["email"]["to"] == ["b@x"]
    assert s.put_delivery({"channels": []}) == []  # unchecking every channel means none, not "back to the file"
    assert s.data["delivery"]["channels"] == []


def test_unchecking_all_channels_in_the_form_overrides_the_file(conf):
    app = app_for(conf)
    # browsers post no 'channels' field when every box is unchecked; the channel fields come back pre-filled
    form = {"min_severity": ["low"], "slack.webhook_url_env": ["SLACK_URL"]}
    assert app.settings.put_delivery(App.parse_delivery_form(form), base=app.seed.delivery) == []
    app.reload()
    assert app.cfg.delivery["channels"] == [] and app.cfg.delivery["slack"]["webhook_url_env"] == "SLACK_URL"


def test_editing_a_target_keeps_it_unscheduled(conf):
    """Unscheduling, then saving the target's edit form (or PUT /api/targets/<name>), must not bring back the
    targets.toml schedule (agent review on PR #1)."""
    app = app_for(conf)
    assert app.save_schedule("ora", {"schedule": ""}) == []
    assert form_save(app, "ora", notes="edited") == []
    assert not app.cfg.targets["ora"].extra.get("schedule") and app.cfg.targets["ora"].extra["notes"] == "edited"
    assert app.save_target("ora", {"engine": "oracle", "dsn": "u/p@h:1521/S"}) == []  # the API's partial PUT
    assert not app_for(conf).cfg.targets["ora"].extra.get("schedule")


def test_partial_schedule_updates_keep_the_rest(conf):
    """POST /api/schedule/<name> with only some fields must change only those (agent review on PR #1, 2nd pass)."""
    app = app_for(conf)  # targets.toml schedules ora "0 * * * *"
    assert app.save_schedule("ora", {"schedule": None, "schedule_window": None, "schedule_paused": True}) == []
    t = app.cfg.targets["ora"].extra
    assert t["schedule"] == "0 * * * *" and t["schedule_paused"] is True  # paused, the toml cron kept
    assert app.save_schedule("ora", {"schedule": None, "schedule_window": 120, "schedule_paused": None}) == []
    t = app.cfg.targets["ora"].extra
    assert t["schedule"] == "0 * * * *" and t["schedule_window"] == 120 and t["schedule_paused"] is True  # pause kept
    assert app.save_schedule("ora", {"schedule": None, "schedule_window": None, "schedule_paused": False}) == []
    assert not app_for(conf).cfg.targets["ora"].extra.get("schedule_paused")  # resumed, across a restart
    assert app.save_schedule("ora", {"schedule": "every 15m", "schedule_window": None, "schedule_paused": None}) == []
    assert app.cfg.targets["ora"].extra["schedule"] == "*/15 * * * *" and app.cfg.targets["ora"].extra["schedule_window"] == 120
    assert app.save_schedule("ora", {"schedule": "", "schedule_window": None, "schedule_paused": None}) == []  # unschedule
    assert not app_for(conf).cfg.targets["ora"].extra.get("schedule")


def test_partial_schedule_update_over_http(conf, monkeypatch):
    import json
    import threading
    import urllib.request

    from dbtriage.web import make_server
    from tests.conftest import free_port, wait_for

    cfg = load_config(conf)
    srv = make_server(cfg, LocalStorage(conf.parent / "data"), "127.0.0.1", free_port(), False, None, True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    try:
        req = urllib.request.Request(f"{url}/api/schedule/ora", data=json.dumps({"schedule_paused": True}).encode(), method="POST")
        row = json.loads(urllib.request.urlopen(req).read())
        assert row["schedule"] == "0 * * * *" and row["paused"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_partial_target_put_keeps_every_explicit_override(conf):
    """A partial PUT /api/targets/<name> changes only what it sends; earlier unchecks and clears all survive
    (Cursor review on PR #1)."""
    app = app_for(conf)
    assert form_save(app, "ora", diagnostics_pack=False, create_snapshot=False, notes="") == []
    assert app.save_schedule("ora", {"schedule": ""}) == []
    assert app.save_target("ora", {"engine": "oracle", "dsn": "u/p@h:1521/S"}) == []  # partial body
    t = app_for(conf).cfg.targets["ora"]
    assert not t.extra.get("schedule") and t.diagnostics_pack is False and not t.extra.get("create_snapshot") and t.extra.get("notes") == ""
    assert app.save_target("ora", {"notes": "only notes"}) == []  # even without engine/dsn: they come from what is there
    t = app_for(conf).cfg.targets["ora"]
    assert t.extra["notes"] == "only notes" and t.diagnostics_pack is False and t.engine == "oracle"
    stored = Settings(conf.parent / "dbtriage-settings.json").data["targets"]["ora"]
    assert "links" not in stored and "engine" not in stored  # equal to targets.toml, so not stored: the file still applies
    assert app.save_target("ora", {"notes": "again"}) == []
    assert app_for(conf).cfg.targets["ora"].extra["links"][0]["label"] == "Grafana"
    assert Settings(conf.parent / "dbtriage-settings.json").data["targets"]["ora"].get("changes_file") in (None, "")


def test_clearing_web_and_agent_fields_beats_the_file(conf):
    """A cleared footer or base_url must not let targets.toml's value show through (Cursor review nit)."""
    conf.write_text(conf.read_text() + '\n[web]\nfooter = "from the file"\nbrand = "File brand"\n[agent]\nprovider = "ollama"\nmodel = "m"\nbase_url = "http://file-host:11434/v1"\n')
    app = app_for(conf)
    from dbtriage.web import DEFAULTS

    app.settings.put_web({"footer": "", "brand": "UI brand", "title": ""}, base={**DEFAULTS, **app.seed.web})  # as the web view does
    assert app.settings.put_agent({"provider": "ollama", "model": "m", "base_url": "", "effort": ""}, base=app.seed.agent) == []
    app = app_for(conf)
    assert app.web["footer"] == "" and app.web["brand"] == "UI brand" and app.web["title"] == "dbtriage"  # cleared title -> default
    assert not app.cfg.agent.get("base_url") and app.cfg.agent.get("effort") == ""


def test_a_deleted_target_comes_back_only_by_full_definition(conf):
    """A partial PUT must not silently resurrect a deleted target (agent review on PR #1, 90e97a5)."""
    app = app_for(conf)
    app.settings.delete_target("ora", seeded=True)
    app.reload()
    assert "ora" not in app.cfg.targets
    problems = app.save_target("ora", {"notes": "hello"})
    assert problems and "deleted" in problems[0]
    assert app.save_schedule("ora", {"schedule": "hourly"}) and "ora" not in app_for(conf).cfg.targets
    assert app.save_target("ora", {"engine": "oracle", "dsn": "u/p@h:1521/S2"}) == []  # an explicit re-create
    t = app_for(conf).cfg.targets["ora"]
    assert t.dsn == "u/p@h:1521/S2" and t.diagnostics_pack is True  # the file's other settings apply again


def test_partial_channel_update_keeps_the_channels_other_fields(conf):
    """{"email": {"smtp_port": 25}} must not drop the stored recipients (agent review on PR #1, 63054b5)."""
    s = Settings(conf.parent / "dbtriage-settings.json")
    assert s.put_delivery({"email": {"smtp_host": "smtp.x", "from": "a@x", "to": "b@x, c@x", "password_env": "SMTP_PW"},
                           "slack": {"webhook_url": "https://hooks.slack.com/services/T/B/secret"}}) == []
    assert s.put_delivery({"email": {"smtp_port": "25"}}) == []
    e = s.data["delivery"]["email"]
    assert e == {"smtp_host": "smtp.x", "from": "a@x", "to": ["b@x", "c@x"], "password_env": "SMTP_PW", "smtp_port": 25}
    assert s.put_delivery({"email": {"to": ""}}) == []  # cleared, and the file sets no recipient: nothing to store
    assert "to" not in s.data["delivery"]["email"] and s.data["delivery"]["email"]["smtp_host"] == "smtp.x"
    assert s.put_delivery({"slack": {"webhook_url": ""}}) == []  # a blank secret keeps the stored one
    assert s.data["delivery"]["slack"]["webhook_url"].endswith("/secret")


def test_cleared_delivery_fields_beat_the_file(conf, monkeypatch):
    """Blanking a channel field or min_severity must not let targets.toml's value come back (agent review, 9fb912f)."""
    conf.write_text(conf.read_text().replace("[delivery.slack]", '[delivery.email]\nto = ["ops@x"]\nsmtp_host = "smtp.file"\nsmtp_port = 2525\n[delivery.slack]'))
    app = app_for(conf)
    assert app.cfg.delivery["email"]["to"] == ["ops@x"] and app.cfg.delivery["min_severity"] == "low"
    assert app.settings.put_delivery({"email": {"to": "", "smtp_port": ""}, "slack": {"webhook_url_env": ""}, "min_severity": ""},
                                     base=app.seed.delivery) == []  # as the web view does
    d = app_for(conf).cfg.delivery
    assert d["email"]["to"] == [] and d["email"]["smtp_host"] == "smtp.file"  # cleared stays cleared; untouched stays the file's
    assert not d["slack"]["webhook_url_env"] and not d["min_severity"]
    # and the channels read an emptied field as their built-in default, not literally
    import smtplib

    from dbtriage import deliver

    sent = {}

    class SMTP:
        def __init__(self, host, port, timeout=None):
            sent.update(host=host, port=port)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self):
            pass
        def send_message(self, m):
            sent["to"] = m["To"]

    monkeypatch.setattr(smtplib, "SMTP", SMTP)
    deliver.send_email({"smtp_host": "h", "smtp_port": "", "from": "a@x", "to": ["b@x"]}, {"severity": "high", "target": "t", "title": "x"}, "md")
    assert sent["port"] == 587 and sent["to"] == "b@x"


def test_recreating_a_deleted_target_through_the_form_restores_the_files_settings(conf):
    """The add-target form comes up blank for a deleted target; its empty fields must not switch off what
    targets.toml sets (agent review on PR #1, 4d31c2e). Re-creating is an un-delete, through the form or the API."""
    app = app_for(conf)
    app.settings.delete_target("ora", seeded=True)
    app.reload()
    blank_form = {"name": ["ora"], "engine": ["oracle"], "dsn": ["u/p@h:1521/S2"]}  # /targets/new: nothing else filled
    n, fields = App.parse_target_form(blank_form)
    assert app.save_target(n, fields) == []
    t = app_for(conf).cfg.targets["ora"]
    assert t.diagnostics_pack is True and t.extra.get("create_snapshot") is True and t.extra["notes"] == "batch at 02:00"
    assert t.extra["links"][0]["label"] == "Grafana" and t.dsn == "u/p@h:1521/S2"
    assert form_save(app_for(conf), "ora", diagnostics_pack=False) == []  # after that, a normal edit can switch it off
    assert app_for(conf).cfg.targets["ora"].diagnostics_pack is False


def test_unscheduling_from_the_card_drops_the_pause(conf):
    """The card resends the pause with every save; unscheduling must still drop it, so a later schedule runs
    (agent review on PR #1, 4d31c2e)."""
    app = app_for(conf)
    assert app.save_schedule("ora", {"schedule": "0 * * * *", "schedule_window": "60", "schedule_paused": True}) == []  # Pause
    assert app.save_schedule("ora", {"schedule": "", "schedule_window": "60", "schedule_paused": True}) == []  # unschedule
    row = {r["target"]: r for r in app_for(conf).schedule_rows()}["ora"]
    assert row["schedule"] == "" and row["paused"] is False
    assert app.save_schedule("ora", {"schedule": "hourly", "schedule_window": "60", "schedule_paused": row["paused"]}) == []
    t = app_for(conf).cfg.targets["ora"].extra
    assert t["schedule"] == "0 * * * *" and not t.get("schedule_paused")


def test_form_saves_do_not_shadow_later_edits_to_the_file(conf):
    """Forms post every field. Fields left as they were must not be stored, or values added to targets.toml
    later would be ignored (agent review on PR #1, 37edc54: findings 1 and 2)."""
    app = app_for(conf)
    # one save of each form, changing nothing
    form = delivery_form_as_rendered(app)
    assert form["min_severity"] == ["low"] and form["slack.webhook_url_env"] == ["SLACK_URL"] and form["jira.min_severity"] == ["high"]
    assert app.settings.put_delivery(App.parse_delivery_form(form), base=app.seed.delivery) == []
    assert form_save(app, "ora") == []
    stored = Settings(conf.parent / "dbtriage-settings.json").data
    assert stored["delivery"] == {} and stored["targets"]["ora"] == {}  # nothing differs from the file
    # now the file grows: a Jira channel, a baseline policy, a changes file, another link
    conf.write_text(conf.read_text()
                    .replace('channels = ["stdout", "slack"]', 'channels = ["stdout", "slack", "jira"]')
                    .replace("[delivery.slack]", '[delivery.jira]\nbase_url = "https://jira.x"\nproject_key = "DBA"\n[delivery.slack]')
                    .replace('schedule = "0 * * * *"', 'schedule = "0 * * * *"\nbaseline_policy = "same-time-last-week"\nchanges_file = "changes.json"'))
    app = app_for(conf)
    assert app.cfg.delivery["channels"] == ["stdout", "slack", "jira"] and app.cfg.delivery["jira"]["project_key"] == "DBA"
    t = app.cfg.targets["ora"]
    assert t.extra["baseline_policy"] == "same-time-last-week" and t.changes_file.endswith("changes.json")


def test_clearing_required_agent_fields_falls_back_to_the_file(conf):
    """model and resource cannot be blank: clearing one must give targets.toml's value, not "" (finding 3)."""
    conf.write_text(conf.read_text() + '\n[agent]\nprovider = "foundry"\nmodel = "claude-opus-5-5"\nresource = "acme-res"\n')
    app = app_for(conf)
    assert app.settings.put_agent({"model": "claude-haiku-5-5", "resource": "other"}, base=app.seed.agent) == []
    assert app_for(conf).cfg.agent["model"] == "claude-haiku-5-5"
    assert app.settings.put_agent({"model": "", "resource": ""}, base=app.seed.agent) == []
    a = app_for(conf).cfg.agent
    assert a["model"] == "claude-opus-5-5" and a["resource"] == "acme-res"


def test_clearing_dsn_env_when_the_file_also_sets_an_inline_dsn(conf, monkeypatch):
    """With both dsn and dsn_env in targets.toml, clearing dsn_env leaves the inline dsn, and an edit keeps it in
    charge; the inline dsn is never shown unmasked by the API (agent review on PR #1, 45628d8)."""
    monkeypatch.delenv("ORA_DSN_UNSET", raising=False)
    conf.write_text(conf.read_text().replace('dsn = "u/p@h:1521/S"', 'dsn = "u/p@h:1521/S"\ndsn_env = "ORA_DSN_UNSET"'))
    app = app_for(conf)
    assert app.cfg.targets["ora"].dsn == "u/p@h:1521/S"
    assert form_save(app, "ora", notes="edited") == []
    assert app_for(conf).cfg.targets["ora"].dsn == "u/p@h:1521/S"  # an edit keeps the inline dsn in charge
    assert app.save_target("ora", {"dsn_env": ""}) == []
    t = app_for(conf).cfg.targets["ora"]
    assert t.dsn == "u/p@h:1521/S" and not t.extra.get("dsn_env")
    api = app_for(conf).api_settings()["targets"]["ora"]
    assert "u/p@" not in json.dumps(api) and api["dsn"] == "u/***@h:1521/S"


def test_wrongly_typed_channel_fields_are_refused_not_crashed(conf):
    """A list field given a number, or a number field given a list, is a 400 with a message, not a traceback
    (agent review on PR #1, 4b0c01e)."""
    s = Settings(conf.parent / "dbtriage-settings.json")
    assert s.put_delivery({"email": {"to": 5}}) == ["email.to: must be a list"]
    assert s.put_delivery({"ntfy": {"url": 5}}) == ["ntfy.url: must be text"]
    assert s.put_delivery({"email": {"smtp_port": [1]}}) == ["email.smtp_port: must be a number"]
    assert s.data["delivery"] == {}  # nothing saved


def test_unscheduling_through_a_target_put_drops_the_pause(conf):
    """PUT /api/targets/<name> {"schedule": ""} must drop the pause like the schedule card does, so a later
    schedule fires (agent review on PR #1, 4b0c01e)."""
    app = app_for(conf)
    assert app.save_schedule("ora", {"schedule_paused": True}) == []
    assert app.save_target("ora", {"schedule": "", "schedule_paused": True}) == []  # the resent pause is ignored too
    stored = Settings(conf.parent / "dbtriage-settings.json").data["targets"]["ora"]
    assert stored.get("schedule") == "" and "schedule_paused" not in stored and "schedule_window" not in stored
    assert app.save_target("ora", {"schedule": "hourly"}) == []
    t = app_for(conf).cfg.targets["ora"].extra
    assert t["schedule"] == "0 * * * *" and not t.get("schedule_paused")


def test_a_ticket_channels_global_choice_means_the_global_gate(conf, monkeypatch):
    """Jira/Plane default to high; choosing "(global)" on the card must mean the global gate, and the card must
    show "high" when nothing is set (agent review on PR #1, 78ffe06)."""
    from dbtriage import deliver as d

    sent = []
    monkeypatch.setitem(d.CHANNELS, "jira", lambda cfg, t, md: sent.append(t["severity"]))
    review = lambda sev: {"severity": sev, "target": "t", "engine": "e", "title": "x"}  # noqa: E731
    d.deliver({"channels": ["jira"], "min_severity": "low", "jira": {}}, review("low"), "md")
    assert sent == []  # unset: tickets for high only
    d.deliver({"channels": ["jira"], "min_severity": "low", "jira": {"min_severity": ""}}, review("low"), "md")
    assert sent == ["low"]  # "(global)": the global gate, low
    s = Settings(conf.parent / "dbtriage-settings.json")
    assert s.put_delivery({"jira": {"min_severity": ""}}) == [] and s.data["delivery"]["jira"] == {"min_severity": ""}
    assert s.put_delivery({"jira": {"min_severity": "high"}}) == [] and "jira" not in s.data["delivery"]  # the default
    page = app_for(conf).settings_page().decode()
    jira = page.split("<legend>jira</legend>")[1].split("</fieldset>")[0]
    assert "<option value='high' selected>" in jira
