r"""Take the README's screenshots (docs/screenshots/), all from Try Demo, so every
picture shows made-up people, a made-up company and practice services.

1. Run the app with --debug (templates reload, and the demo limit is off):

       cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
       .venv\Scripts\flask --app pulsecheck run --debug --port 5014

2. In another window (Playwright isn't in requirements.txt: borrow another project's):

       cd C:\Users\USER\Documents\Workspace\API_uptime_monitor
       ..\expense_tracker\.venv\Scripts\python docs\take_screenshots.py

It starts a demo, waits for its past to finish replaying, then visits each page.
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5014"
OUT = Path(__file__).parent / "screenshots"
WIDE = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


def shot(page, name, full=True):
    page.wait_for_load_state("networkidle")
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / name), full_page=full)
    print("saved", name)


def start_demo(page):
    page.goto(BASE + "/login")
    page.click("text=Try the demo")
    for _ in range(180):  # the replay of its past: about 10 seconds locally
        if not page.request.get(BASE + "/demo/replay.json").json()["replaying"]:
            page.wait_for_timeout(2500)  # the Dashboard reloads itself when the replay ends: let it
            return
        time.sleep(1)
    raise SystemExit("The demo's replay didn't finish. Is the app running with its crew?")


def open_monitor(page, name):
    page.goto(BASE + "/monitors")
    page.click(f"a.row-link:text-is('{name}')")
    page.wait_for_load_state()
    return page.url


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        wide = browser.new_context(viewport=WIDE, reduced_motion="reduce")
        page = wide.new_page()
        page.goto(BASE + "/login")
        shot(page, "login.png", full=False)
        start_demo(page)
        page.goto(BASE + "/dashboard")
        shot(page, "dashboard.png")
        page.goto(BASE + "/monitors")
        shot(page, "monitors.png")
        open_monitor(page, "Payments API")
        shot(page, "monitor.png")
        page.goto(page.url + "?period=90d#numbers")
        page.locator("#numbers").screenshot(path=str(OUT / "monitor-90-days.png"))
        print("saved monitor-90-days.png")
        shipping = open_monitor(page, "Shipping API")
        page.goto(shipping + "/alerts")
        shot(page, "alert-rules.png")
        open_monitor(page, "Nightly backup")
        shot(page, "heartbeat.png")
        page.goto(BASE + "/monitors/new")
        shot(page, "add-monitor.png", full=False)
        page.goto(BASE + "/incidents")
        shot(page, "incidents.png")
        page.click("table a.row-link:has-text('Shipping API')")
        shot(page, "incident.png")
        page.goto(BASE + "/alerts")
        shot(page, "alerts.png")
        page.goto(BASE + "/alerts/sent")
        shot(page, "alerts-sent.png")
        page.goto(BASE + "/alerts/inbox")
        shot(page, "practice-inbox.png", full=False)
        page.goto(BASE + "/status-pages")
        page.click("text=Open the page")
        shot(page, "status-page.png")
        page.goto(BASE + "/maintenance")
        shot(page, "maintenance.png")
        page.goto(BASE + "/insights")
        shot(page, "insights.png")
        page.goto(BASE + "/crew")
        shot(page, "crew.png")
        page.goto(BASE + "/settings")
        shot(page, "settings.png")

        dark = browser.new_context(viewport=WIDE, color_scheme="dark", reduced_motion="reduce",
                                   storage_state=wide.storage_state())
        page = dark.new_page()
        page.goto(BASE + "/dashboard")
        shot(page, "dashboard-dark.png", full=False)
        open_monitor(page, "Search API")
        shot(page, "monitor-dark.png", full=False)

        phone = browser.new_context(viewport=PHONE, is_mobile=True, has_touch=True, reduced_motion="reduce",
                                    storage_state=wide.storage_state())
        page = phone.new_page()
        page.goto(BASE + "/dashboard")
        shot(page, "phone-dashboard.png")
        page.goto(BASE + "/status-pages")
        page.click("text=Open the page")
        shot(page, "phone-status-page.png")
        browser.close()


if __name__ == "__main__":
    main()
