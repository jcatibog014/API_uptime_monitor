"""Failure handling: confirm before calling it down, recover with hysteresis, never count a
check twice, don't blame a service for our own connection, and survive a stale monitor."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from pulsecheck import canary, checking, db, incidents, probe, scheduler, timers, worker
from pulsecheck.models import CanaryCheck, Check, Incident, Monitor

from .conftest import make_monitor, post, words

NINE = datetime(2026, 11, 18, 9, 0)
# A scripted service: SCRIPT[monitor name] is a function(moment) -> True (up) or False (down).
SCRIPT = {}


def scripted(monitor, config, moment):
    up = SCRIPT[monitor.name](moment)
    result = probe.Result(status_code=200 if up else 503, total_ms=50.0, ttfb_ms=40.0)
    if up:
        result.ok, result.message = True, "200 OK"
    else:
        result.error_kind, result.message = "http_status", "Answered 503 Service Unavailable; expected 200-299."
    return result


@pytest.fixture(autouse=True)
def scripted_kind(monkeypatch):
    monkeypatch.setitem(checking.PROBERS, "script", scripted)
    SCRIPT.clear()


def scripted_monitor(app, workspace_id, up, name="Payments API", **fields):
    SCRIPT[name] = up
    monitor_id = make_monitor(app, workspace_id, name=name, kind="script", url="https://api.example.com/",
                              next_check_at=NINE, **fields)
    return monitor_id


def play(app, minutes, start=NINE):
    with app.app_context():
        worker.simulate(db.session(), start, start + timedelta(minutes=minutes))


def get(app, model, **where):
    with app.app_context():
        return db.session().scalars(select(model).filter_by(**where)).all()


def monitor(app, monitor_id):
    with app.app_context():
        return db.session().get(Monitor, monitor_id)


def down_between(start, end):
    return lambda moment: not (start <= moment < end)


def test_one_failed_check_is_not_an_outage(app, owner):
    _, workspace_id = owner
    blip = NINE + timedelta(minutes=2)
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: moment.replace(microsecond=0) != blip)
    with app.app_context():  # put a check exactly on the blip
        s = db.session()
        s.get(Monitor, monitor_id).next_check_at = blip
        s.commit()
    play(app, 5, start=blip)
    assert get(app, Incident, monitor_id=monitor_id) == []
    checks = get(app, Check, monitor_id=monitor_id)
    assert [check.ok for check in sorted(checks, key=lambda c: c.scheduled_at)][:2] == [False, True]
    # The re-check came 20 seconds after the failure, not a minute later.
    first, second = sorted(checks, key=lambda c: c.scheduled_at)[:2]
    assert (second.scheduled_at - first.scheduled_at).total_seconds() == 20
    assert monitor(app, monitor_id).state == "steady"


def test_failures_in_a_row_open_one_incident_from_the_first_failure(app, owner):
    _, workspace_id = owner
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 10)
    [incident] = get(app, Incident, monitor_id=monitor_id)
    assert incident.opened_at == NINE  # when it started, not when it was confirmed
    assert incident.confirmed_at > NINE and incident.cause == "http_status" and incident.is_open
    found = monitor(app, monitor_id)
    assert found.state == "flatline" and found.state_since == NINE
    checks = sorted(get(app, Check, monitor_id=monitor_id), key=lambda c: c.scheduled_at)
    assert incident.confirmed_at == checks[2].scheduled_at  # the third failure in a row confirmed it
    assert incident.failed_checks == len(checks)


def test_it_is_up_again_only_after_good_checks_in_a_row(app, owner):
    _, workspace_id = owner
    back = NINE + timedelta(minutes=5)
    flicker = back + timedelta(seconds=30)  # one good answer, then down again for a moment
    monitor_id = scripted_monitor(
        app, workspace_id,
        lambda moment: moment >= back and not (flicker <= moment < flicker + timedelta(seconds=40)))
    play(app, 15)
    [incident] = get(app, Incident, monitor_id=monitor_id)
    assert incident.resolved_at is not None and incident.resolved_at > flicker
    assert monitor(app, monitor_id).state == "steady"


def test_recovery_is_confirmed_quickly(app, owner):
    _, workspace_id = owner
    back = NINE + timedelta(minutes=5)
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: moment >= back)
    play(app, 12)
    [incident] = get(app, Incident, monitor_id=monitor_id)
    first_good = min(check.scheduled_at for check in get(app, Check, monitor_id=monitor_id) if check.ok)
    assert (incident.resolved_at - first_good).total_seconds() == 20  # a re-check, not the next minute


def test_a_check_is_never_counted_twice(app, owner):
    _, workspace_id = owner
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    with app.app_context():
        s = db.session()
        found = s.get(Monitor, monitor_id)
        check, _ = checking.run_check(s, found, NINE, NINE)
        incidents.judge(s, found, check, NINE)
        incidents.judge(s, found, check, NINE)  # the same check again (a job that ran twice)
        older = checking.record(s, found, scripted(found, {}, NINE), NINE - timedelta(minutes=1), NINE)
        incidents.judge(s, found, older, NINE)  # a slow re-check that arrived late
        s.commit()
        assert found.fail_streak == 1


def test_our_own_connection_failing_is_not_blamed_on_the_service(app, owner):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url="https://api.example.com/", next_check_at=NINE)
    with app.app_context():
        s = db.session()
        s.add(CanaryCheck(target="outside", at=NINE - timedelta(seconds=30), ok=False, message="No answer."))
        found = s.get(Monitor, monitor_id)
        failed = probe.Result(error_kind="timeout", message="No answer within 10 s.", total_ms=10000.0)
        check = checking.record(s, found, failed, NINE, NINE)
        incidents.judge(s, found, check, NINE)
        s.commit()
        assert check.our_side and found.fail_streak == 0 and found.state == "new"
        # An old canary result says nothing: a failure then counts.
        later = NINE + timedelta(minutes=10)
        check = checking.record(s, found, failed, later, later)
        incidents.judge(s, found, check, later)
        assert not check.our_side and found.state == "irregular"


def test_a_monitor_changed_during_its_check_is_judged_again(app, owner, monkeypatch):
    """A page changes the monitor while the crew waits for the answer: the crew's save from its
    stale copy fails (version_id_col), so it reads the monitor again and judges once."""
    _, workspace_id = owner
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    real_probe = checking.probe_monitor

    def probe_while_someone_renames_it(found, moment, config=None):
        other = db.new_session()
        try:
            other.get(Monitor, monitor_id).name = "Payments API (live)"
            other.commit()
        finally:
            other.close()
        SCRIPT["Payments API (live)"] = SCRIPT["Payments API"]
        return real_probe(found, moment, config)

    monkeypatch.setattr(checking, "probe_monitor", probe_while_someone_renames_it)
    with app.app_context():
        s = db.session()
        scheduler.run_due(s, NINE)
        worker.run_until_idle(s, now_fn=lambda: NINE)
        found = s.get(Monitor, monitor_id)
        s.refresh(found)
        assert found.name == "Payments API (live)" and found.fail_streak == 1 and found.state == "irregular"
    assert len(get(app, Check, monitor_id=monitor_id)) == 1


def test_resuming_starts_the_runs_again(app, owner):
    client, workspace_id = owner
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 1)
    assert monitor(app, monitor_id).fail_streak >= 1
    post(client, f"/monitors/{monitor_id}/pause")
    post(client, f"/monitors/{monitor_id}/pause")
    assert monitor(app, monitor_id).fail_streak == 0


# ---------- The canary ----------

def test_the_canary_checks_itself_and_the_outside_once_a_minute(app, owner, site):
    app.config.update(SELF_CHECK=True, BASE_URL=site, CANARY_URL=f"{site}/down")
    with app.app_context():
        s = db.session()
        worker.simulate(s, NINE, NINE + timedelta(minutes=2, seconds=30))
        rows = s.scalars(select(CanaryCheck).order_by(CanaryCheck.at)).all()
        assert sorted({row.target for row in rows}) == ["outside", "self"]
        assert len([row for row in rows if row.target == "self"]) == 3  # 9:00, 9:01, 9:02
        assert all(row.ok for row in rows if row.target == "self")
        assert canary.our_side_down(s, NINE + timedelta(minutes=2, seconds=30))


def test_only_one_worker_wins_a_timer(app):
    with app.app_context():
        first, second = db.new_session(), db.new_session()
        try:
            assert timers.claim(first, "canary", NINE, 60)
            first.commit()
            # Both read the timer as due; whoever writes second finds it already moved on.
            later = NINE + timedelta(seconds=61)
            wins = []
            for session in (first, second):
                wins.append(timers.claim(session, "canary", later, 60))
                session.commit()
        finally:
            first.close()
            second.close()
    assert wins.count(True) == 1


# ---------- The pages ----------

def test_the_incident_pages_tell_the_story(app, owner):
    client, workspace_id = owner
    monitor_id = scripted_monitor(app, workspace_id, down_between(NINE, NINE + timedelta(minutes=4)))
    other_id = scripted_monitor(app, workspace_id, lambda moment: False, name="Search API")
    play(app, 10)
    page = words(client.get("/incidents"))
    assert "Payments API" in page and "Search API" in page and "Resolved" in page and "Ongoing" in page
    assert "Wrong status code" in page
    assert "Search API" in words(client.get("/incidents?tab=open")) and \
        "Payments API" not in words(client.get("/incidents?tab=open"))
    [incident] = get(app, Incident, monitor_id=monitor_id)
    page = words(client.get(f"/incidents/{incident.id}"))
    assert "The first check failed" in page and "Back up: 2 good checks in a row." in page
    assert "Checks During the Incident" in page
    page = words(client.get(f"/monitors/{other_id}"))
    assert "Down since" in page and "Flatline" in page and "Recent Incidents" in page
    assert "Checking Again" in words(client.get("/dashboard"))


def test_the_menu_counts_open_incidents_and_the_trace_goes_flat(app, owner):
    client, workspace_id = owner
    scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 5)
    page = client.get("/dashboard").get_data(as_text=True)
    assert 'title="Open incidents">1<' in page and "trace is-flat" in page


def test_someone_elses_incident_is_hidden(app, owner):
    from .conftest import make_owner
    client, _ = owner
    _, other = make_owner(app, name="Other", email="other@example.com")
    scripted_monitor(app, other, lambda moment: False)
    play(app, 5)
    [incident] = get(app, Incident)
    assert client.get(f"/incidents/{incident.id}").status_code == 404


def test_the_crew_page_shows_the_self_check(app, owner, site):
    client, _ = owner
    app.config.update(SELF_CHECK=True, BASE_URL=site, CANARY_URL=f"{site}/ok")
    with app.app_context():
        worker.simulate(db.session(), NINE, NINE + timedelta(seconds=30))
    page = words(client.get("/crew"))
    assert "Self-Check" in page and "Our own address" in page and "Keeps the free plan awake" in page
