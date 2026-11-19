"""Monitor pages: add, change, delete, one monitor's page, and Check now."""
import pytest
from sqlalchemy import func, select

from pulsecheck import db, monitors
from pulsecheck.models import Check, Monitor

from .conftest import browser, make_monitor, make_owner, post, text, words

GOOD = {"name": "Payments API", "url": "https://api.example.com/health", "method": "GET",
        "expected_statuses": "200-299", "timeout_s": "10", "interval_s": "60"}


def saved(app, monitor_id):
    with app.app_context():
        return db.session().get(Monitor, monitor_id)


def checks_of(app, monitor_id):
    with app.app_context():
        return db.session().scalars(select(Check).where(Check.monitor_id == monitor_id)).all()


def test_add_a_monitor(app, owner):
    client, workspace_id = owner
    response = post(client, "/monitors/new", {**GOOD, "name": "  Payments   API "})
    assert response.status_code == 302
    with app.app_context():
        monitor = db.session().scalar(select(Monitor))
    assert (monitor.name, monitor.url, monitor.workspace_id, monitor.kind) == (
        "Payments API", "https://api.example.com/health", workspace_id, "http")
    assert response.headers["Location"].endswith(f"/monitors/{monitor.id}")


def test_mistakes_are_explained_and_nothing_is_saved(app, owner):
    client, _ = owner
    response = post(client, "/monitors/new", {**GOOD, "name": "", "url": "api.example.com",
                                              "expected_statuses": "two hundred", "interval_s": "7"})
    page = words(response)
    assert response.status_code == 400
    assert "Give the monitor a name" in page and "Start the address with https://" in page
    assert "Use status codes like 200-299" in page and "Choose how often from the list." in page
    with app.app_context():
        assert db.session().scalar(select(func.count()).select_from(Monitor)) == 0


def test_the_wait_must_be_shorter_than_the_gap(app, owner):
    """A 30-second wait can't fit in a check every minute? It can; but not a wait as long as the gap."""
    values, errors = monitors.read_form({**GOOD, "timeout_s": "30", "interval_s": "60"})
    assert not errors
    assert monitors.read_form({**GOOD, "timeout_s": "60", "interval_s": "60"})[1]["timeout_s"]


def test_a_private_address_is_refused_on_the_live_site(app, owner):
    app.config["ALLOW_PRIVATE_TARGETS"] = False
    client, _ = owner
    response = post(client, "/monitors/new", {**GOOD, "url": "http://169.254.169.254/latest/meta-data/"})
    assert response.status_code == 400 and "inside a private network" in text(response)


def test_change_and_delete(app, owner):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    response = post(client, f"/monitors/{monitor_id}/edit", {**GOOD, "name": "Payments", "interval_s": "300"})
    assert response.status_code == 302
    assert (saved(app, monitor_id).name, saved(app, monitor_id).interval_s) == ("Payments", 300)
    response = post(client, f"/monitors/{monitor_id}/delete")
    assert response.status_code == 302 and saved(app, monitor_id) is None


def test_nobody_sees_another_workspaces_monitor(app, owner):
    client, _ = owner
    _other_user, other_workspace = make_owner(app, name="Other", email="other@example.com")
    monitor_id = make_monitor(app, other_workspace)
    assert client.get(f"/monitors/{monitor_id}").status_code == 404
    assert post(client, f"/monitors/{monitor_id}/delete").status_code == 404
    assert post(client, f"/monitors/{monitor_id}/check").status_code == 404
    assert saved(app, monitor_id) is not None


def test_check_now_records_the_check(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    response = post(client, f"/monitors/{monitor_id}/check", follow_redirects=True)
    page = words(response)
    assert "Up: 200 OK, in" in page
    [check] = checks_of(app, monitor_id)
    assert check.ok and check.manual and check.status_code == 200 and check.total_ms is not None
    assert "Latest Check" in page and "Recent Checks" in page and "Beat" in page


def test_check_now_on_a_broken_service_says_why(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/down")
    page = words(post(client, f"/monitors/{monitor_id}/check", follow_redirects=True))
    assert "Down: Answered 503 Service Unavailable; expected 200-299." in page
    assert "Missed Beat" in page and "Wrong status code" in page


def test_check_now_has_a_limit(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    for _ in range(6):
        post(client, f"/monitors/{monitor_id}/check")
    page = words(post(client, f"/monitors/{monitor_id}/check", follow_redirects=True))
    assert "a lot of checks in one minute" in page
    assert len(checks_of(app, monitor_id)) == 6


def test_the_pages_show_monitors(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    post(client, f"/monitors/{monitor_id}/check")
    for url in ("/dashboard", "/monitors", f"/monitors/{monitor_id}", f"/monitors/{monitor_id}/edit", "/monitors/new"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert "Payments API" in text(response) or url == "/monitors/new", url
    assert "Every minute" in text(client.get("/monitors"))


def test_empty_pages_say_what_to_do(owner):
    client, _ = owner
    assert "No Monitors Yet" in text(client.get("/dashboard"))
    assert "No Monitors Yet" in text(client.get("/monitors"))


def test_a_workspace_has_a_monitor_limit(app, owner, monkeypatch):
    client, workspace_id = owner
    monkeypatch.setattr(monitors, "MAX_MONITORS", 2)
    make_monitor(app, workspace_id)
    make_monitor(app, workspace_id, name="Search API")
    response = client.get("/monitors/new", follow_redirects=True)
    assert "the most a workspace can have" in words(response)


def test_a_check_run_twice_is_recorded_once(app, owner, site):
    from datetime import datetime

    from pulsecheck import checking
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    moment = datetime(2026, 11, 18, 9, 0)
    with app.app_context():
        s = db.session()
        monitor = s.get(Monitor, monitor_id)
        first, _ = checking.run_check(s, monitor, moment, moment)
        second, _ = checking.run_check(s, monitor, moment, moment)
        s.commit()
        assert first.id == second.id
    assert len(checks_of(app, monitor_id)) == 1


def test_logged_out_visitors_see_no_monitors(app):
    assert browser(app).get("/monitors").status_code == 302


@pytest.mark.parametrize("seconds, said", [
    (60, "Every minute"), (300, "Every 5 minutes"), (3600, "Every hour"), (21600, "Every 6 hours"),
    (86400, "Every day"), (172800, "Every 2 days"), (90, "Every 90 seconds")])
def test_how_often_is_said_in_words(seconds, said):
    assert monitors.interval_words(seconds) == said


def test_a_heartbeat_says_every_day_not_seconds(app, owner):
    client, workspace_id = owner
    make_monitor(app, workspace_id, name="Nightly backup", kind="heartbeat", url="", interval_s=86400)
    page = words(client.get("/monitors"))
    assert "Every day" in page and "86400" not in page


def test_pause_and_resume_from_the_list(app, owner):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    assert "Pause checks on Payments API" in text(client.get("/monitors"))
    response = post(client, f"/monitors/{monitor_id}/pause?back=list")
    assert response.status_code == 302 and response.headers["Location"].endswith("/monitors")
    assert saved(app, monitor_id).paused
    assert "Resume checks on Payments API" in text(client.get("/monitors"))
    post(client, f"/monitors/{monitor_id}/pause?back=list")
    assert not saved(app, monitor_id).paused
