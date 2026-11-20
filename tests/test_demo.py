"""The practice services and Try Demo: a sandbox with a replayed past, on every page."""
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from pulsecheck import db, practice, seed, worker
from pulsecheck.dates import utcnow
from pulsecheck.models import Check, Incident, Job, Monitor, User, Workspace

from .conftest import CSRF, post, text, words

NINE = datetime(2026, 11, 18, 9, 0)


# ---------- Practice services ----------

def test_the_same_check_always_gets_the_same_answer():
    for key in practice.SERVICES:
        url = practice.address(key)
        assert practice.answer(url, NINE, 3) == practice.answer(url, NINE, 3), key


def test_payments_has_a_short_outage_every_few_days():
    url = practice.address("payments-api")
    minutes = [NINE + timedelta(minutes=n) for n in range(9 * 24 * 60)]
    down = [moment for moment in minutes if not practice.answer(url, moment).ok]
    assert len(down) % 7 == 0 and 2 * 7 <= len(down) <= 4 * 7  # one 7-minute outage in each 3-day block
    assert all(practice.answer(url, moment).error_kind == "timeout" for moment in down)


def test_the_partner_api_is_flaky_and_search_is_often_slow():
    partner = [practice.answer(practice.address("partner-api"), NINE + timedelta(minutes=n)) for n in range(600)]
    assert 0.03 < sum(not result.ok for result in partner) / 600 < 0.1
    assert {result.status_code for result in partner if not result.ok} == {502}
    search = [practice.answer(practice.address("search-api"), NINE + timedelta(minutes=n)) for n in range(600)]
    assert sum(result.total_ms > 1400 for result in search) > 40


def test_shipping_is_down_for_the_window_in_its_address():
    url = practice.address("shipping-api", down="2026-11-18T09:00", **{"for": 30})
    assert practice.answer(url, NINE - timedelta(minutes=1)).ok
    assert practice.answer(url, NINE).status_code == 503
    assert not practice.answer(url, NINE + timedelta(minutes=29)).ok
    assert practice.answer(url, NINE + timedelta(minutes=30)).ok


def test_a_practice_timeout_takes_no_real_time():
    url = practice.address("payments-api")
    started = time.perf_counter()
    for n in range(24 * 60):
        practice.answer(url, NINE + timedelta(minutes=n))
    assert time.perf_counter() - started < 2


# ---------- Try Demo ----------

def start_demo(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    response = post(client, "/demo")
    assert response.status_code == 302 and response.headers["Location"].endswith("/dashboard")
    return client


def replay(app):
    """The crew picks up the demo's replay job (the live site's in-app workers do this)."""
    started = time.perf_counter()
    with app.app_context():
        worker.run_until_idle(db.session())
    return time.perf_counter() - started


def the_workspace(app):
    with app.app_context():
        return db.session().scalar(select(Workspace).where(Workspace.is_demo.is_(True)))


def test_a_demo_starts_replaying_and_then_has_a_past(app):
    client = start_demo(app)
    assert the_workspace(app).replaying
    assert "Getting your demo ready" in text(client.get("/dashboard"))
    seconds = replay(app)
    workspace = the_workspace(app)
    assert not workspace.replaying
    with app.app_context():
        s = db.session()
        monitors = s.scalars(select(Monitor).where(Monitor.workspace_id == workspace.id)).all()
        assert sorted(m.kind for m in monitors) == ["heartbeat"] + ["practice"] * len(seed.MONITORS)
        checks = s.scalar(select(func.count()).select_from(Check).join(Monitor)
                          .where(Monitor.workspace_id == workspace.id))
        assert checks > 3 * 60 * 5  # hours of checks
        shipping = next(m for m in monitors if m.name == "Shipping API")
        assert shipping.state == "flatline"
        incidents = s.scalars(select(Incident).where(Incident.workspace_id == workspace.id)).all()
        assert any(i.is_open and i.monitor_id == shipping.id for i in incidents)
        assert any(not i.is_open for i in incidents)  # a payments outage or a flaky partner, already over
        # The live crew carries on from now.
        assert all(m.next_check_at >= utcnow() - timedelta(minutes=2) for m in monitors if m.kind == "practice")
    assert seconds < 60, f"The replay took {seconds:.1f} s"
    print(f"\nreplay: {seconds:.1f} s for {checks} checks")


def test_every_page_of_the_demo_shows_an_example(app):
    client = start_demo(app)
    replay(app)
    workspace = the_workspace(app)
    with app.app_context():
        s = db.session()
        monitor = s.scalar(select(Monitor).where(Monitor.workspace_id == workspace.id, Monitor.name == "Shipping API"))
        incident = s.scalar(select(Incident).where(Incident.workspace_id == workspace.id))
    pages = {
        "/dashboard": ["Shipping API", "Flatline", "Website", "Steady"],
        "/monitors": ["Old Partner API", "Every 2 minutes"],
        f"/monitors/{monitor.id}": ["Down since", "Recent Incidents", "Recent Checks", "practice://shipping-api"],
        "/incidents": ["Ongoing", "Resolved"],
        f"/incidents/{incident.id}": ["What Happened", "Checks During the Incident"],
        "/crew": ["Workers", "Dead Letters"],
        "/settings": ["Lakbay Travel", "in a demo"],
        "/monitors/new": ["Practice ones: the demo never checks real sites"],
    }
    for url, expected in pages.items():
        response = client.get(url)
        assert response.status_code == 200, url
        page = words(response)
        for phrase in expected:
            assert phrase in page, (url, phrase)
        assert "No Monitors Yet" not in page and "No Incidents Yet" not in page, url
    assert "?down=" not in text(client.get(f"/monitors/{monitor.id}"))  # the script's settings stay hidden


def test_the_demo_only_checks_practice_services(app):
    client = start_demo(app)
    replay(app)
    response = post(client, "/monitors/new", {"name": "Real site", "url": "https://example.com/", "service": "nope",
                                              "method": "GET", "expected_statuses": "200-299", "timeout_s": "10",
                                              "interval_s": "60"})
    assert response.status_code == 400 and "Choose a practice service" in text(response)
    response = post(client, "/monitors/new", {"name": "Images", "url": "https://example.com/", "service": "images-cdn",
                                              "method": "GET", "expected_statuses": "200-299", "timeout_s": "10",
                                              "interval_s": "60"})
    assert response.status_code == 302
    with app.app_context():
        added = db.session().scalar(select(Monitor).where(Monitor.name == "Images"))
        assert (added.kind, added.url) == ("practice", "practice://images-cdn")


def test_the_demo_cant_change_its_account(app):
    client = start_demo(app)
    response = post(client, "/settings", {"action": "name", "user_name": "Someone"})
    assert response.status_code == 403


def test_demos_are_deleted_after_a_day(app):
    start_demo(app)
    replay(app)
    with app.app_context():
        s = db.session()
        assert seed.purge(s, utcnow() + timedelta(hours=23)) == 0
        assert seed.purge(s, utcnow() + seed.LIFETIME + timedelta(minutes=1)) == 1
        assert s.scalar(select(func.count()).select_from(Monitor)) == 0
        assert s.scalar(select(func.count()).select_from(Check)) == 0
        assert s.scalar(select(func.count()).select_from(User)) == 0
        assert s.scalar(select(func.count()).select_from(Job).where(Job.workspace_id.is_not(None))) == 0


def test_the_live_crew_leaves_a_replaying_demo_alone(app):
    start_demo(app)
    with app.app_context():
        s = db.session()
        from pulsecheck import scheduler
        assert scheduler.run_due(s, utcnow()) == 0  # its monitors are due, but it is replaying


def test_there_is_a_limit_on_demos(app, monkeypatch):
    monkeypatch.setattr(seed, "MAX_DEMOS", 1)
    start_demo(app)
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    response = post(client, "/demo", follow_redirects=True)
    assert "The demo is very busy right now" in text(response)


def test_the_login_page_offers_the_demo(client):
    page = text(client.get("/login"))
    assert "Explore the Demo" in page and "Try the demo" in page


def test_practice_parts_are_never_negative():
    for key in practice.SERVICES:
        for n in range(300):
            result = practice.answer(practice.address(key), NINE + timedelta(minutes=n))
            assert all(part is None or part >= 0 for part in (result.dns_ms, result.connect_ms, result.tls_ms,
                                                              result.ttfb_ms)), (key, n)
