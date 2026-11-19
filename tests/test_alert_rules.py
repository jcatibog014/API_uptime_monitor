"""Alert rules: routing, reminders until someone is on it, escalation, and flapping."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from pulsecheck import db, worker
from pulsecheck.models import Alert, Channel, Incident, Monitor, PracticeMessage

from .conftest import CSRF, post, text, words
from .test_incidents import SCRIPT, scripted_monitor

NINE = datetime(2026, 11, 18, 9, 0)


@pytest.fixture(autouse=True)
def scripted(monkeypatch):
    from pulsecheck import checking

    from .test_incidents import scripted as prober
    monkeypatch.setitem(checking.PROBERS, "script", prober)
    SCRIPT.clear()


def channel(app, workspace_id, name):
    with app.app_context():
        s = db.session()
        row = Channel(workspace_id=workspace_id, kind="practice", name=name, settings={"looks_like": "email"})
        s.add(row)
        s.commit()
        return row.id


def play(app, minutes, start=NINE):
    with app.app_context():
        worker.simulate(db.session(), start, start + timedelta(minutes=minutes))


def sent(app, **where):
    with app.app_context():
        rows = db.session().scalars(select(Alert).filter_by(**where).order_by(Alert.id)).all()
        return [(row.event, row.seq, row.channel.name) for row in rows]


def set_rules(client, monitor_id, **form):
    return post(client, f"/monitors/{monitor_id}/alerts", form)


def test_a_monitor_can_choose_its_channels(app, owner):
    client, workspace_id = owner
    team, phone = channel(app, workspace_id, "Team"), channel(app, workspace_id, "Phone")
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    other_id = scripted_monitor(app, workspace_id, lambda moment: False, name="Search API")
    assert set_rules(client, monitor_id, channel=str(team)).status_code == 302
    play(app, 5)
    alerts = sent(app, event="down")
    assert ("down", 0, "Team") in alerts and ("down", 0, "Phone") in alerts  # Search API: every channel
    with app.app_context():
        payments = db.session().scalar(select(Incident).where(Incident.monitor_id == monitor_id))
    assert sent(app, incident_id=payments.id) == [("down", 0, "Team")]
    assert other_id and phone


def test_reminders_until_someone_is_on_it(app, owner):
    client, workspace_id = owner
    channel(app, workspace_id, "Team")
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    set_rules(client, monitor_id, remind_every_m="15")
    play(app, 50)
    assert [event for event, *_ in sent(app)] == ["down", "reminder", "reminder", "reminder"]
    assert [seq for event, seq, _ in sent(app) if event == "reminder"] == [1, 2, 3]
    with app.app_context():
        incident = db.session().scalar(select(Incident))
    post(client, f"/incidents/{incident.id}/ack")
    play(app, 60, start=NINE + timedelta(minutes=50))
    assert len(sent(app, event="reminder")) == 3  # acknowledged: no more


def test_nobody_on_it_escalates_once(app, owner):
    client, workspace_id = owner
    team, boss = channel(app, workspace_id, "Team"), channel(app, workspace_id, "Boss")
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    response = set_rules(client, monitor_id, channel=str(team), escalate_after_m="20")
    assert response.status_code == 400 and "Choose who gets the escalation" in text(response)
    set_rules(client, monitor_id, channel=str(team), escalate_to=str(boss), escalate_after_m="20")
    play(app, 60)
    assert sent(app, event="escalation") == [("escalation", 0, "Boss")]
    assert ("down", 0, "Boss") not in sent(app)  # level 2 only hears the escalation
    assert "To Boss after 20 minutes" in words(client.get(f"/monitors/{monitor_id}"))


def flaps(moment):
    """Down 3 minutes, up 3 minutes, for 36 minutes; then up for good."""
    minutes = (moment - NINE).total_seconds() // 60
    return minutes >= 36 or minutes % 6 >= 3


def test_flapping_sends_one_alert_and_then_one_when_it_settles(app, owner):
    client, workspace_id = owner
    channel(app, workspace_id, "Team")
    monitor_id = scripted_monitor(app, workspace_id, flaps)
    play(app, 33)
    with app.app_context():
        s = db.session()
        assert s.get(Monitor, monitor_id).flapping
        incidents = len(s.scalars(select(Incident)).all())
    assert incidents >= 4
    assert [event for event, *_ in sent(app)] == ["down", "up", "down", "up", "flapping"]
    assert "Flapping since" in words(client.get(f"/monitors/{monitor_id}"))
    play(app, 30, start=NINE + timedelta(minutes=33))
    assert [event for event, *_ in sent(app)][-1] == "settled"
    with app.app_context():
        assert not db.session().get(Monitor, monitor_id).flapping


def test_a_flapper_that_settles_down_sends_its_down_alert(app, owner):
    _, workspace_id = owner
    channel(app, workspace_id, "Team")
    scripted_monitor(app, workspace_id, lambda moment: flaps(moment) and moment < NINE + timedelta(minutes=20))
    play(app, 60)
    events = [event for event, *_ in sent(app)]
    assert "flapping" in events and events[-1] == "down" and "settled" not in events


def test_the_demo_has_reminders_an_escalation_and_a_flapper(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    post(client, "/demo")
    with app.app_context():
        worker.run_until_idle(db.session())
        titles = [row.title for row in db.session().scalars(select(PracticeMessage))]
        phone = [row.title for row in db.session().scalars(select(PracticeMessage).where(
            PracticeMessage.channel_name == "Rina's phone"))]
    assert "Shipping API is still down" in titles
    # The Shipping API's first alerts go to the team; Rina's phone hears only its escalation.
    assert "Shipping API is still down, and nobody is on it" in phone and "Shipping API is down" not in phone
    assert "Old Partner API is flapping" in titles
