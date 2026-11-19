"""Status pages (public, no login), incident notes, the RSS feed, and maintenance windows."""
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from pulsecheck import db, metrics, worker
from pulsecheck.models import Incident, Maintenance, MaintenanceMonitor, Monitor, StatusPage

from .conftest import CSRF, make_owner, post, text, words
from .test_incidents import SCRIPT, scripted_monitor

NINE = datetime(2026, 11, 18, 9, 0)


@pytest.fixture(autouse=True)
def scripted(monkeypatch):
    from pulsecheck import checking

    from .test_incidents import scripted as prober
    monkeypatch.setitem(checking.PROBERS, "script", prober)
    SCRIPT.clear()


def play(app, minutes, start=NINE):
    with app.app_context():
        worker.simulate(db.session(), start, start + timedelta(minutes=minutes))


def make_page(client, app):
    post(client, "/status-pages")
    with app.app_context():
        page = db.session().scalars(select(StatusPage).order_by(StatusPage.id.desc())).first()
        page.entry_count = len(page.entries)  # read now: the page leaves its session below
        return page


def test_a_status_page_starts_with_every_monitor(app, owner):
    client, workspace_id = owner
    scripted_monitor(app, workspace_id, lambda moment: True)
    scripted_monitor(app, workspace_id, lambda moment: True, name="Search API")
    page = make_page(client, app)
    assert page.entry_count == 2 and page.slug.startswith("maria-s-monitors-")
    stranger = app.test_client()
    public = words(stranger.get(f"/status/{page.slug}"))
    assert "Maria's Monitors Status" in public and "Payments API" in public and "Search API" in public
    assert stranger.get("/status/no-such-page").status_code == 404


def test_choose_monitors_names_and_the_address(app, owner):
    client, workspace_id = owner
    payments = scripted_monitor(app, workspace_id, lambda moment: True)
    search = scripted_monitor(app, workspace_id, lambda moment: True, name="Search API")
    page = make_page(client, app)
    form = {"name": "Acme Status", "slug": "acme", "intro": "Is it up?", f"show_{payments}": "1",
            f"label_{payments}": "Checkout", f"position_{payments}": "0"}
    assert post(client, f"/status-pages/{page.id}", form).status_code == 302
    public = words(app.test_client().get("/status/acme"))
    assert "Checkout" in public and "Payments API" not in public and "Search API" not in public and "Is it up?" in public
    for bad in ("A", "has space", "-dash", "x" * 70):
        response = post(client, f"/status-pages/{page.id}", {**form, "slug": bad})
        assert response.status_code == 400, bad
    response = post(client, f"/status-pages/{page.id}", {**form, f"show_{payments}": ""})
    assert "Choose at least one monitor" in text(response)
    assert search


def test_an_address_belongs_to_one_page(app, owner):
    client, workspace_id = owner
    scripted_monitor(app, workspace_id, lambda moment: True)
    first = make_page(client, app)
    post(client, "/status-pages")
    with app.app_context():
        second = db.session().scalars(select(StatusPage).order_by(StatusPage.id)).all()[1]
        monitor_id = db.session().scalar(select(Monitor.id))
    post(client, f"/status-pages/{first.id}", {"name": "A", "slug": "taken", f"show_{monitor_id}": "1"})
    response = post(client, f"/status-pages/{second.id}", {"name": "B", "slug": "taken", f"show_{monitor_id}": "1"})
    assert response.status_code == 400 and "Another status page has that address" in text(response)


def test_the_public_page_shows_trouble_and_the_notes(app, owner):
    client, workspace_id = owner
    scripted_monitor(app, workspace_id, lambda moment: False)
    page = make_page(client, app)
    assert "Everything Is Up" in words(app.test_client().get(f"/status/{page.slug}"))
    play(app, 0.5)  # failing, but still being checked again: the public sees nothing yet
    assert "Everything Is Up" in words(app.test_client().get(f"/status/{page.slug}"))
    play(app, 5, start=NINE + timedelta(minutes=1))
    [incident] = _all(app, Incident)
    post(client, f"/incidents/{incident.id}/updates", {"status": "identified", "message": "The database is full."})
    public = words(app.test_client().get(f"/status/{page.slug}"))
    assert "Some Services Are Down" in public and "Identified" in public and "The database is full." in public
    assert "The database is full." in words(client.get(f"/incidents/{incident.id}"))


def test_notes_need_a_stage_and_words(app, owner):
    client, workspace_id = owner
    scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 5)
    [incident] = _all(app, Incident)
    response = post(client, f"/incidents/{incident.id}/updates", {"status": "panicking", "message": "x"},
                    follow_redirects=True)
    assert "Choose a stage and write a note" in words(response)


def test_the_feed_is_rss(app, owner):
    client, workspace_id = owner
    scripted_monitor(app, workspace_id, lambda moment: False)
    page = make_page(client, app)
    play(app, 5)
    [incident] = _all(app, Incident)
    post(client, f"/incidents/{incident.id}/updates", {"status": "investigating", "message": "Looking <into> it & more."})
    response = app.test_client().get(f"/status/{page.slug}/feed.xml")
    assert response.content_type.startswith("application/rss+xml")
    root = ElementTree.fromstring(response.data)
    [item] = root.findall("./channel/item")
    assert item.findtext("title") == "Payments API: Investigating"
    assert item.findtext("description") == "Looking <into> it & more."  # escaped in the XML, read back whole


def test_someone_elses_page_is_hidden(app, owner):
    client, _ = owner
    other_user, other_workspace = make_owner(app, name="Other", email="other@example.com")
    from .conftest import browser
    other = browser(app, other_user)
    scripted_monitor(app, other_workspace, lambda moment: True)
    page = make_page(other, app)
    assert client.get(f"/status-pages/{page.id}").status_code == 404
    assert post(client, f"/status-pages/{page.id}/delete").status_code == 404


# ---------- Maintenance ----------

def plan(app, workspace_id, monitor_id, start, minutes):
    with app.app_context():
        s = db.session()
        window = Maintenance(workspace_id=workspace_id, title="Database upgrade", starts_at=start,
                             ends_at=start + timedelta(minutes=minutes))
        s.add(window)
        s.flush()
        s.add(MaintenanceMonitor(maintenance_id=window.id, monitor_id=monitor_id))
        s.commit()


def _all(app, model):
    with app.app_context():
        return db.session().scalars(select(model)).all()


def test_a_monitor_rests_during_maintenance(app, owner):
    _, workspace_id = owner
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    plan(app, workspace_id, monitor_id, NINE, 20)
    play(app, 15)
    assert _all(app, Incident) == []
    with app.app_context():
        s = db.session()
        assert s.get(Monitor, monitor_id).state == "resting"
        stats = metrics.raw_stats(s, [monitor_id], NINE, NINE + timedelta(minutes=15))[monitor_id]
        assert stats.counted == 0 and stats.resting >= 15 and stats.uptime is None
    play(app, 10, start=NINE + timedelta(minutes=15))  # the window ends at 9:20: still failing, so it is down
    [incident] = _all(app, Incident)
    assert incident.opened_at >= NINE + timedelta(minutes=20)


def test_planning_maintenance_from_the_page(app, owner):
    client, workspace_id = owner
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: True)
    form = {"title": "Database upgrade", "starts_at": "2026-12-01T02:00", "ends_at": "2026-12-01T02:30",
            "monitor": str(monitor_id), "note": "Bookings may fail."}
    assert post(client, "/maintenance", form).status_code == 302
    [window] = _all(app, Maintenance)
    assert window.starts_at == datetime(2026, 11, 30, 18, 0)  # 2 AM in Manila is 6 PM UTC the day before
    assert "Database upgrade" in words(client.get("/maintenance"))
    response = post(client, "/maintenance", {**form, "monitor": ""})
    assert "Choose the monitors that rest" in text(response)
    response = post(client, "/maintenance", {**form, "ends_at": "2026-12-01T01:00"})
    assert "The end must be after the start." in text(response)
    page = make_page(client, app)
    assert "Planned Maintenance: Database upgrade" in words(app.test_client().get(f"/status/{page.slug}"))


# ---------- The demo ----------

def test_the_demo_has_a_status_page_notes_and_maintenance(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    post(client, "/demo")
    with app.app_context():
        worker.run_until_idle(db.session())
        page = db.session().scalar(select(StatusPage))
    public = words(app.test_client().get(f"/status/{page.slug}"))
    assert "Lakbay Travel Status" in public and "Checkout" in public and "Shipping rates" in public
    assert "Shipping prices aren't showing" in public and "Planned Maintenance: Database upgrade" in public
    assert "Payments work again" in public  # the last 7 days
    assert "Search index rebuild" in words(client.get("/maintenance"))
