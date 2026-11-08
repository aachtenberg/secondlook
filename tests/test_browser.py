"""The web view in a real browser (Playwright driving Chrome): every page, every interactive piece.

Each test gets its own web server over a freshly seeded store, with the model pointed at the fake OpenAI-compatible
server from conftest, so nothing here touches a real model, database or network service. Every page load is checked
for console errors and uncaught exceptions.
"""
from __future__ import annotations

import re

import pytest

from tests.conftest import LONG_SQL, free_port, wait_for

pytestmark = pytest.mark.browser
pw = pytest.importorskip("playwright.sync_api", reason="Playwright not installed: uv sync --extra dev")
expect = pw.expect

INCIDENT = "postgres/demo/2026-11-05/20261105T100000Z"
PAGES = ["/", "/t/demo", "/t/quiet", f"/s/{INCIDENT}", "/t/demo/compare", f"/t/demo/sql/{LONG_SQL}", "/search?q=demo", "/settings",
         "/targets/demo", "/targets/new"]


@pytest.fixture(scope="session")
def browser():
    with pw.sync_playwright() as p:
        b = None
        for kw in ({"channel": "chrome"}, {}):  # the system Chrome, else Playwright's own Chromium
            try:
                b = p.chromium.launch(headless=True, **kw)
                break
            except Exception:  # noqa: BLE001
                continue
        if b is None:
            pytest.skip("no browser: install Chrome or run `uv run playwright install chromium`")
        yield b
        b.close()


@pytest.fixture
def page(browser, request):
    """A fresh browser context per test (clean localStorage), dark colour scheme, console errors collected and
    checked after the test unless it is marked allow_console_errors."""
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, color_scheme="dark")
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    errors: list[str] = []
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.errors = errors
    yield pg
    ctx.close()
    if not request.node.get_closest_marker("allow_console_errors"):
        assert errors == [], errors


# --------------------------------------------------------------------------- every page
@pytest.mark.parametrize("width", [1400, 390])
def test_every_page_renders_cleanly(page, web_server, width):
    page.set_viewport_size({"width": width, "height": 900})
    for path in PAGES:
        resp = page.goto(web_server + path)
        assert resp.status == 200, path
        assert page.locator("h1").count() == 1, f"{path}: one h1"
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"{path} scrolls sideways by {overflow}px at {width}px wide"
        unnamed = page.evaluate("""[...document.querySelectorAll('button, a[href]')]
            .filter(e => (e.offsetParent !== null || e instanceof SVGElement) && !(e.getAttribute('aria-label') || e.getAttribute('title')
                    || (e.innerText || e.textContent || '').trim() || (e.querySelector('title') || {}).textContent))
            .map(e => e.outerHTML.slice(0, 80))""")
        assert unnamed == [], f"{path}: controls without an accessible name: {unnamed}"
    assert page.errors == []


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_select_options_are_readable(page, web_server, theme):
    """An open select's list is drawn on the option's background, else the select's, else the browser's white.
    The Trend and Compare selects are transparent pills, so without their own option colours the dark theme drew
    near-white text on white."""
    page.add_init_script(f"try{{localStorage.setItem('dbtriage.theme','{theme}')}}catch(e){{}}")
    for path in PAGES:
        page.goto(web_server + path)
        bad = page.evaluate("""() => {
            const rgba = s => (s.match(/[\\d.]+/g) || []).map(Number);
            const opaque = c => c.length === 3 || c[3] === 1;
            const lum = c => { const [r, g, b] = c.slice(0, 3).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
                               return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
            const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
            return [...document.querySelectorAll('select option')].map(o => {
                const sel = o.closest('select'), ob = rgba(getComputedStyle(o).backgroundColor), sb = rgba(getComputedStyle(sel).backgroundColor);
                const bg = opaque(ob) ? ob : opaque(sb) ? sb : [255, 255, 255];
                const r = ratio(rgba(getComputedStyle(o).color), bg);
                return r < 4.5 ? `${sel.name}/${o.value}: ${r.toFixed(2)}:1` : null;
            }).filter(Boolean);
        }""")
        assert bad == [], f"{path} ({theme}): option text below 4.5:1 on its list background: {bad[:5]}"


def test_overview_and_navigation(page, web_server):
    page.goto(web_server)
    expect(page.locator("h1")).to_have_text(re.compile("Overview"))
    expect(page.locator("main table.list")).to_contain_text("demo")
    page.locator("nav.side a.t", has_text="demo").click()
    expect(page).to_have_url(web_server + "/t/demo")
    expect(page.locator("nav.side a.t.active")).to_contain_text("demo")
    page.locator("nav.side .sub a").first.click()
    expect(page).to_have_url(re.compile(r"/s/postgres/demo/"))
    page.locator(".crumbs a", has_text="demo").click()
    expect(page).to_have_url(web_server + "/t/demo")


def test_theme_toggle_and_nav_collapse_persist(page, web_server):
    page.goto(web_server)
    html = page.locator("html")
    page.click("#theme-toggle")
    expect(html).to_have_attribute("data-theme", "light")  # dark scheme emulated, so the first click goes light
    bg_light = page.evaluate("getComputedStyle(document.body).backgroundColor")
    page.reload()
    expect(html).to_have_attribute("data-theme", "light")
    page.click("#theme-toggle")
    expect(html).to_have_attribute("data-theme", "dark")
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") != bg_light
    page.click("#nav-toggle")
    expect(page.locator("nav.side")).to_be_hidden()
    page.reload()
    expect(page.locator("nav.side")).to_be_hidden()
    page.click("#nav-toggle")
    expect(page.locator("nav.side")).to_be_visible()


# --------------------------------------------------------------------------- target page
def test_trend_metric_and_filter_chips(page, web_server):
    page.goto(web_server + "/t/demo")
    expect(page.locator("svg.trend circle").first).to_be_visible()  # one marker per snapshot that has a comparison
    page.select_option("select[name=metric]", "tup_returned")
    expect(page).to_have_url(re.compile(r"metric=tup_returned"))
    expect(page.locator("select[name=metric]")).to_have_value("tup_returned")
    rows = page.locator("section.card.flush table.list tbody tr")
    expect(rows).to_have_count(2)
    page.locator(".chips a", has_text="Incidents").click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("10:00:00")
    page.locator(".chips a", has_text="Scheduled").click()
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("09:00:00")


def test_capture_now_dialog(page, web_server):
    page.goto(web_server + "/t/demo")
    dialog = page.locator("dialog#run")
    page.get_by_role("button", name="Capture now").click()
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()
    page.get_by_role("button", name="Capture now").click()
    dialog.locator("input[name=reason]").fill("browser test")
    dialog.get_by_role("button", name="Start run").click()  # the target's DSN points nowhere; the run fails quietly in the background
    expect(page).to_have_url(web_server + "/t/demo")


# --------------------------------------------------------------------------- snapshot page
def test_snapshot_review_card_and_file_drawer(page, web_server):
    page.goto(web_server + f"/s/{INCIDENT}")
    expect(page.locator("h1")).to_have_text("Snapshot 10:00:00")
    banner = page.locator(".banner.warn")
    expect(banner).to_contain_text("AI reviewer unavailable")
    expect(banner).to_contain_text("No credentials configured for the model provider.")
    expect(banner.locator("details pre")).to_be_hidden()
    banner.locator("summary").click()
    expect(banner.locator("details pre")).to_contain_text("Could not resolve authentication")
    expect(page.locator(".checked .pill2")).to_have_count(3)
    # long SQL ids are shortened everywhere and link to the SQL page
    short = f"{LONG_SQL[:8]}…{LONG_SQL[-4:]}"
    expect(page.locator("table.topsql").get_by_role("link", name=short)).to_have_attribute("href", f"/t/demo/sql/{LONG_SQL}")
    assert LONG_SQL not in page.locator("h2#rev-h").inner_text()
    expect(page.locator(".bars.waits .pair")).to_have_count(2)
    expect(page.locator(".bars.delta .one")).to_have_count(2)
    # files open in the side drawer, Escape closes it
    page.locator("a[data-panel]", has_text="manifest.json").click()
    expect(page.locator("#panel")).to_be_visible()
    expect(page.locator("#panel-body")).to_contain_text('"target": "demo"')
    page.keyboard.press("Escape")
    expect(page.locator("#panel")).to_be_hidden()


def test_top_sql_sorts_by_its_headers(page, web_server):
    """Each step starts from q2short on top (by calls, largest first), so every click must move the rows."""
    page.goto(web_server + f"/s/{INCIDENT}")
    first = page.locator("table.topsql tbody tr").first.locator("td").first.locator("a")
    th = lambda name: page.locator("table.topsql th").filter(has=page.get_by_role("button", name=name, exact=True))  # noqa: E731
    sort = lambda name: page.get_by_role("button", name=name, exact=True).click()  # noqa: E731
    expect(first).to_have_attribute("title", LONG_SQL)  # as collected: most time first
    sort("Calls")  # numbers: largest first (18674 calls, then 2628)
    expect(th("Calls")).to_have_attribute("aria-sort", "descending")
    expect(first).to_have_attribute("title", "q2short")
    sort("Calls")  # again: reversed
    expect(th("Calls")).to_have_attribute("aria-sort", "ascending")
    expect(first).to_have_attribute("title", LONG_SQL)
    for name, direction in (("Share", "descending"), ("Mean ms", "descending"), ("SQL id", "ascending")):
        sort("Calls")
        if th("Calls").get_attribute("aria-sort") != "descending":
            sort("Calls")
        expect(first).to_have_attribute("title", "q2short")
        sort(name)  # Share by data-v (0.54 over 0.29), not its "54%" text; Mean ms 17.47 over 1.318; ids A to Z
        expect(th(name)).to_have_attribute("aria-sort", direction)
        expect(first).to_have_attribute("title", LONG_SQL)
        assert th("Calls").get_attribute("aria-sort") is None  # one sorted column at a time
    expect(page.locator("table.topsql tbody tr")).to_have_count(2)  # sorting moves rows, never drops them


def test_discuss_panel_round_trip(page, web_server, fake_llm):
    page.goto(web_server + f"/s/{INCIDENT}")
    box, log, q = page.locator("#discuss"), page.locator("#chat-log"), page.locator("#chat-q")
    expect(box).to_be_visible()
    expect(page.locator("#discuss-focus")).to_have_text("whole review")
    expect(log).to_contain_text("1 heuristic finding")
    sugg = log.locator(".sugg button[data-key=review]").first
    n_before = len(fake_llm.requests)
    sugg.click()  # fills the box, sends nothing
    expect(q).to_have_value(sugg.get_attribute("data-ask"))
    assert len(fake_llm.requests) == n_before
    finding = log.locator(".sugg button[data-key=finding-1]")
    if finding.count():  # a suggestion about a finding switches to that finding's thread
        finding.click()
        expect(page.locator("#discuss-focus")).to_have_text("finding 1")
        expect(q).to_have_value(finding.get_attribute("data-ask"))
        page.locator("a.chat-link[data-chat=finding-1]").click()
        page.goto(page.url)  # back to the whole-review thread for the round trip below
        expect(page.locator("#discuss-focus")).to_have_text("whole review")
    q.fill("what changed?")
    q.press("Enter")
    expect(log.locator(".dmsg.user")).to_have_text("what changed?")
    answer = log.locator(".dmsg.assistant .dtext").last
    expect(answer).to_contain_text("Fake answer to: what changed?")
    expect(answer.locator(".foot")).to_contain_text("fake-model")
    expect(answer.locator("b")).to_have_text("Fake answer")  # Markdown rendered
    page.reload()  # the thread was stored with the snapshot
    expect(page.locator("#chat-log .dmsg.user")).to_have_text("what changed?")


def test_discuss_threads_per_finding_and_query(page, web_server):
    page.goto(web_server + f"/s/{INCIDENT}")
    focus = page.locator("#discuss-focus")
    page.locator("a.chat-link[data-chat=finding-1]").click()
    expect(focus).to_have_text("finding 1")
    page.locator(f"a.askbtn[data-chat=sql-{LONG_SQL}]").click()
    expect(focus).to_have_text(f"sql {LONG_SQL[:8]}…{LONG_SQL[-4:]}")
    page.locator("#chat-q").fill("is it worth an index?")
    page.locator("#chat-q").press("Enter")
    expect(page.locator("#chat-log .dmsg.assistant .dtext").last).to_contain_text("Fake answer to: is it worth an index?")
    page.click("#discuss-close")
    expect(page.locator("#discuss")).to_be_hidden()
    page.reload()
    expect(page.locator("#discuss")).to_be_hidden()  # remembered
    page.get_by_role("button", name="Discuss with AI").click()
    expect(page.locator("#discuss")).to_be_visible()


# --------------------------------------------------------------------------- settings page
def test_settings_save_in_place_with_toasts(page, web_server):
    page.goto(web_server + "/settings")
    agent = page.locator("form[action='/settings/agent']")
    agent.scroll_into_view_if_needed()
    y = page.evaluate("scrollY")
    agent.get_by_role("button", name="Save", exact=True).click()
    toast = page.locator(".toast").last
    expect(toast).to_contain_text("agent settings saved")
    assert page.evaluate("scrollY") == y and page.url == web_server + "/settings"
    agent.get_by_role("button", name="Test model").click()
    expect(page.locator(".toast").last).to_contain_text("answered 'OK'")
    expect(page.locator(".toast").first).to_be_hidden(timeout=6000)  # toasts fade on their own


def test_schedule_rows_update_in_place(page, web_server):
    page.goto(web_server + "/settings")
    row = page.locator("tr", has=page.locator("input[name=name][value=demo]"))
    row.locator("select.sched").select_option(label="hourly")
    row.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator(".toast").last).to_contain_text("demo: hourly")
    row = page.locator("tr", has=page.locator("input[name=name][value=demo]"))
    expect(row.get_by_role("button", name="Pause")).to_be_visible()
    expect(row.locator("td.ts").first).not_to_have_text("–")
    row.locator("select.sched").select_option("custom")
    expect(row.locator("input.cron")).to_be_visible()
    row.locator("input.cron").fill("every 7m")
    row.get_by_role("button", name="Save", exact=True).click()
    bad = page.locator(".toast.bad").last
    expect(bad).to_contain_text("every Nm")
    # the refused save answers 400 on purpose and the browser logs that; nothing else may be logged
    page.errors[:] = [e for e in page.errors if "status of 400" not in e]
    page.goto(web_server + "/t/demo")
    expect(page.locator(".meta")).to_contain_text("hourly")


def test_model_picker(page, web_server, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)  # never reach the real Groq from a test
    page.goto(web_server + "/settings")
    status = page.locator("#model-status")
    expect(status).to_have_text(re.compile(r"^2 models available"))  # fake-model, fake-large; whisper filtered out
    expect(page.locator("#model-list option")).to_have_count(2)
    page.fill("#a-model", "not-a-model")
    page.locator("#a-model").dispatch_event("change")
    expect(status).to_contain_text("is not in the list")
    page.select_option("#a-provider", "groq")
    expect(status).to_contain_text("GROQ_API_KEY is not set")
    expect(page.locator("#a-model")).to_have_value("")


def test_look_and_feel_reloads_with_toast(page, web_server):
    page.goto(web_server + "/settings")
    form = page.locator("form[action='/settings/save']")
    form.locator("input[name=brand]").fill("Brand E2E")
    form.get_by_role("button", name="Save").click()
    expect(page.locator("a.brand")).to_contain_text("Brand E2E")
    expect(page.locator(".toast").last).to_contain_text("saved")


# --------------------------------------------------------------------------- search, SQL, compare
def test_search_to_sql_page(page, web_server):
    page.goto(web_server)
    page.fill("form.search input", "293bbb")
    page.press("form.search input", "Enter")
    expect(page).to_have_url(re.compile(r"/search\?q=293bbb"))
    page.get_by_role("link", name=LONG_SQL).click()
    expect(page.locator("h1 code")).to_have_text(LONG_SQL)
    expect(page.locator("pre.sqltext")).to_contain_text("SELECT COUNT")
    expect(page.locator("main table.list tbody tr")).to_have_count(2)


def test_compare_page(page, web_server):
    page.goto(web_server + "/t/demo/compare")
    expect(page.locator(".tile .lbl", has_text="Heuristic findings")).to_be_visible()
    page.select_option("select[name=b]", INCIDENT)
    page.get_by_role("button", name="Compare").click()
    expect(page.locator(".notice")).to_contain_text("pick two different snapshots")


# --------------------------------------------------------------------------- outage
@pytest.mark.allow_console_errors  # the 503 for a store-only page is logged by the browser
def test_store_outage_banner(page, tmp_path, fake_llm):
    from dbtriage.config import Config, Target
    from dbtriage.storage import open_storage
    from dbtriage.web import make_server

    store = open_storage("postgres://u@127.0.0.1:1/x?connect_timeout=1", tmp_path / "spool")
    cfg = Config(storage_url="postgres://u@127.0.0.1:1/x", base_dir=str(tmp_path), targets={"demo": Target("demo", "postgres", "x")},
                 agent={"provider": "ollama", "model": "fake-model", "base_url": fake_llm.url})
    import threading

    srv = make_server(cfg, store, "127.0.0.1", free_port(), False, None, False, False, False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    request_cleanup = srv
    url = f"http://127.0.0.1:{srv.server_port}"
    wait_for(url + "/health")
    page.goto(url)
    expect(page.locator(".notice.degraded")).to_contain_text("is unreachable")
    expect(page.locator("h1")).to_have_text(re.compile("Overview"))
    assert page.goto(url + f"/s/{INCIDENT}").status == 503
    health = page.goto(url + "/health").json()
    assert health["ok"] is False and health["store_ok"] is False
    request_cleanup.shutdown()
    request_cleanup.server_close()
