"""Smarter checks: rules about the answer, the speed limit, sleepy sites, TCP ports, heartbeats and
certificate warnings."""
import socket
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from pulsecheck import certificates, db, heartbeat, probe, rules, worker
from pulsecheck.dates import utcnow
from pulsecheck.models import Alert, Channel, Check, Incident, Monitor

from .conftest import make_monitor, post, text, words

NINE = datetime(2026, 11, 18, 9, 0)
JSON_ANSWER = probe.Result(ok=True, status_code=200, body=b'{"status": "ok", "db": {"up": true, "replicas": [3]}}',
                           headers={"content-type": "application/json", "x-version": "4"}, total_ms=120.0)


# ---------- Rules about the answer ----------

def test_a_json_value_is_found_by_its_path():
    data = {"data": {"items": [{"name": "a"}]}}
    assert rules.json_at(data, "data.items.0.name") == "a"
    with pytest.raises(KeyError):
        rules.json_at(data, "data.items.3")


@pytest.mark.parametrize("rule, problem", [
    ({"contains": "ok"}, None),
    ({"contains": "degraded"}, 'doesn\'t contain "degraded"'),
    ({"not_contains": "status"}, 'contains "status"'),
    ({"header": "X-Version"}, None),
    ({"header": "x-region"}, "has no x-region header"),
    ({"json_path": "db.up", "json_value": "true"}, None),
    ({"json_path": "db.replicas.0", "json_value": "3"}, None),
    ({"json_path": "status", "json_value": "down"}, "status is ok, not down"),
    ({"json_path": "db.disk"}, "nothing at db.disk"),
])
def test_rules_about_the_answer(rule, problem):
    found = rules.rule_problem(rule, JSON_ANSWER)
    assert found == problem if problem is None else problem in found


def test_an_answer_that_isnt_json():
    answer = probe.Result(ok=True, body=b"<html>", headers={})
    assert rules.rule_problem({"json_path": "status"}, answer) == "The answer isn't JSON."


def test_a_broken_rule_is_a_failed_check(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/json", assertions={"json_path": "status", "json_value": "down"})
    page = words(post(client, f"/monitors/{monitor_id}/check", follow_redirects=True))
    assert "Down: status is ok, not down." in page and "Wrong content" in page


def test_a_slow_answer_is_a_weak_pulse(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/slow?0.25", slow_ms=100)
    post(client, f"/monitors/{monitor_id}/check")
    with app.app_context():
        s = db.session()
        [check] = s.scalars(select(Check)).all()
        assert check.ok and check.slow and "the limit is 100 ms" in check.message
        assert s.get(Monitor, monitor_id).state == "weak"
    assert "Slow Beat" in words(client.get(f"/monitors/{monitor_id}"))


def test_a_sleepy_site_gets_ninety_seconds():
    assert rules.timeout_for(Monitor(timeout_s=10, sleepy=True)) == 90
    assert rules.timeout_for(Monitor(timeout_s=10, sleepy=False)) == 10


def test_the_form_takes_rules(app, owner):
    client, _ = owner
    form = {"kind": "http", "name": "Health", "url": "https://api.example.com/health", "method": "GET",
            "expected_statuses": "200", "timeout_s": "10", "interval_s": "300", "slow_ms": "1000",
            "contains": "ok", "json_path": "db.up", "json_value": "true", "sleepy": "1"}
    assert post(client, "/monitors/new", form).status_code == 302
    with app.app_context():
        monitor = db.session().scalar(select(Monitor))
        assert monitor.assertions == {"contains": "ok", "json_path": "db.up", "json_value": "true"}
        assert (monitor.slow_ms, monitor.sleepy) == (1000, True)
    response = post(client, "/monitors/new", {**form, "interval_s": "60"})
    assert response.status_code == 400 and "A sleepy site may need 90 seconds" in text(response)
    response = post(client, "/monitors/new", {**form, "json_path": "", "sleepy": ""})
    assert response.status_code == 400 and "Say where the value is" in text(response)


# ---------- TCP ports ----------

def test_a_tcp_port(app, owner, site):
    client, workspace_id = owner
    port = int(site.rsplit(":", 1)[1])
    response = post(client, "/monitors/new?kind=tcp", {"kind": "tcp", "name": "Test server", "host": "127.0.0.1",
                                                       "port": str(port), "timeout_s": "5", "interval_s": "60"})
    assert response.status_code == 302
    with app.app_context():
        monitor = db.session().scalar(select(Monitor))
        assert (monitor.kind, monitor.url) == ("tcp", f"tcp://127.0.0.1:{port}")
    page = words(post(client, f"/monitors/{monitor.id}/check", follow_redirects=True))
    assert f"Up: Port {port} is open." in page
    with socket.socket() as spare:
        spare.bind(("127.0.0.1", 0))
        closed = spare.getsockname()[1]
    assert probe.check_tcp("127.0.0.1", closed, allow_private=True).error_kind == "refused"
    assert probe.check_tcp("127.0.0.1", port).error_kind == "blocked"  # private, on the live site


def test_tcp_form_mistakes(owner):
    client, _ = owner
    response = post(client, "/monitors/new", {"kind": "tcp", "name": "Db", "host": "", "port": "99999",
                                              "timeout_s": "5", "interval_s": "60"})
    assert response.status_code == 400 and "Enter the server's name" in text(response)


# ---------- Heartbeats ----------

def heartbeat_monitor(client, app):
    response = post(client, "/monitors/new?kind=heartbeat", {"kind": "heartbeat", "name": "Nightly backup",
                                                             "interval_s": "3600", "grace_s": "300"})
    assert response.status_code == 302
    with app.app_context():
        return db.session().scalar(select(Monitor))


def test_a_heartbeat_gets_its_own_address_and_counts_pings(app, owner):
    client, _ = owner
    monitor = heartbeat_monitor(client, app)
    assert monitor.heartbeat_token and (monitor.confirm_tries, monitor.recover_tries) == (1, 1)
    page = text(client.get(f"/monitors/{monitor.id}"))
    assert f"/hb/{monitor.heartbeat_token}" in page and "Send a test ping" in page and "Check now" not in page
    response = app.test_client().get(f"/hb/{monitor.heartbeat_token}")  # a job: no login, no form token
    assert response.status_code == 200 and response.data == b"ok\n"
    assert app.test_client().post(f"/hb/{monitor.heartbeat_token}").status_code == 200
    with app.app_context():
        s = db.session()
        found = s.get(Monitor, monitor.id)
        assert found.state == "steady" and len(s.scalars(select(Check)).all()) == 2
        expected = found.last_ping_at + timedelta(seconds=3600 + 300)
        assert found.next_check_at == expected
    assert app.test_client().get("/hb/not-a-token").status_code == 404


def test_a_missing_ping_is_an_incident_and_the_next_ping_ends_it(app, owner):
    client, _ = owner
    monitor = heartbeat_monitor(client, app)
    app.test_client().get(f"/hb/{monitor.heartbeat_token}")
    with app.app_context():
        s = db.session()
        due = s.get(Monitor, monitor.id).next_check_at
        worker.simulate(s, due - timedelta(seconds=1), due + timedelta(seconds=5))
        found = s.get(Monitor, monitor.id)
        [incident] = s.scalars(select(Incident)).all()
        assert found.state == "flatline" and incident.cause == "missing" and "No ping since" in incident.message
        # The job runs again (the test's clock is an hour ahead now, so the ping comes from here).
        heartbeat.ping(s, found, due + timedelta(minutes=3))
        s.commit()
        assert s.get(Monitor, monitor.id).state == "steady"
        assert s.scalar(select(Incident)).resolved_at == due + timedelta(minutes=3)


def test_a_paused_heartbeat_ignores_pings(app, owner):
    client, _ = owner
    monitor = heartbeat_monitor(client, app)
    post(client, f"/monitors/{monitor.id}/pause")
    response = app.test_client().get(f"/hb/{monitor.heartbeat_token}")
    assert response.data == b"paused\n"


# ---------- Certificates ----------

def cert_result(days, now):
    return probe.Result(ok=True, cert_expires_at=now + timedelta(days=days), cert_issuer="Let's Encrypt")


def test_certificate_warnings_go_out_once_per_step(app, owner):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    with app.app_context():
        s = db.session()
        s.add(Channel(workspace_id=workspace_id, kind="practice", name="Inbox", settings={"looks_like": "email"}))
        monitor = s.get(Monitor, monitor_id)
        for days in (30, 13.5, 12, 10, 6.5, 6, 0.5):
            certificates.note(s, monitor, cert_result(days, NINE), NINE)
        s.commit()
        titles = [alert.title for alert in s.scalars(select(Alert).order_by(Alert.id))]
        assert titles == ["Payments API's certificate runs out in 13 days",
                          "Payments API's certificate runs out in 6 days",
                          "Payments API's certificate runs out in 0 days"]
        certificates.note(s, monitor, cert_result(90, NINE), NINE)  # renewed
        assert monitor.cert_warned_days is None


def test_the_monitor_page_warns_about_a_certificate(app, owner):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, cert_expires_at=utcnow() + timedelta(days=5),
                              cert_issuer="Let's Encrypt")
    page = words(client.get(f"/monitors/{monitor_id}"))
    assert "The certificate runs out in" in page and "Let's Encrypt" in page


# ---------- The demo ----------

def test_the_demo_shows_the_smarter_checks(app):
    from .conftest import CSRF
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    post(client, "/demo")
    with app.app_context():
        s = db.session()
        worker.run_until_idle(s)
        backup = s.scalar(select(Monitor).where(Monitor.kind == "heartbeat"))
        cdn = s.scalar(select(Monitor).where(Monitor.name == "Images CDN"))
        search = s.scalar(select(Monitor).where(Monitor.name == "Search API"))
        assert backup.state == "steady" and backup.last_ping_at is not None
        assert s.scalar(select(Incident).where(Incident.monitor_id == backup.id)).cause == "missing"
        assert cdn.cert_warned_days == 14
        assert s.scalar(select(Check).where(Check.monitor_id == search.id, Check.slow.is_(True))) is not None
    assert "Its Address" in words(client.get(f"/monitors/{backup.id}"))
    assert "certificate runs out in 9 days" in words(client.get("/alerts/inbox"))
    assert "Slow alert" in words(client.get(f"/monitors/{search.id}"))
