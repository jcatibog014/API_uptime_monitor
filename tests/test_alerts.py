"""Alerts: one per incident and channel, retried until they arrive (Retry-After wins), never
retried when they can't work, a breaker per channel, encrypted secrets, the Acknowledge link,
the Practice Inbox, and the channel pages. A pretend service stands in for the real ones."""
import io
import json
import urllib.error
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from pulsecheck import alerts, crypto, db, queue, senders, worker
from pulsecheck.dates import utcnow
from pulsecheck.models import Alert, Channel, Incident, Job, PracticeMessage, PushSubscription

from .conftest import CSRF, make_monitor, post, text, words
from .test_incidents import SCRIPT, scripted_monitor  # noqa: F401  (the scripted kind is set up per test)

NINE = datetime(2026, 11, 18, 9, 0)
DISCORD_URL = "https://discord.com/api/webhooks/123456/abc-DEF_ghi"


class FakeService:
    """Stands in for urllib's urlopen: records each request, answers from a list."""

    def __init__(self):
        self.calls, self.answers = [], []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data) if request.data else None
        self.calls.append({"url": request.full_url, "json": body, "headers": dict(request.header_items())})
        status, answer, headers = (self.answers.pop(0) if self.answers else (204, {}, {}))
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "no", headers, io.BytesIO(json.dumps(answer).encode()))
        return FakeResponse(status, answer)


class FakeResponse:
    def __init__(self, status, answer):
        self.status, self._answer = status, answer

    def read(self):
        return json.dumps(self._answer).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(senders, "OPENER", fake)
    return fake


@pytest.fixture(autouse=True)
def scripted(monkeypatch):
    from pulsecheck import checking

    from .test_incidents import scripted as prober
    monkeypatch.setitem(checking.PROBERS, "script", prober)
    SCRIPT.clear()


def add_channel(app, workspace_id, kind="discord", name="Discord #ops", settings=None, secrets=None):
    with app.app_context():
        s = db.session()
        channel = Channel(workspace_id=workspace_id, kind=kind, name=name, settings=settings or {},
                          secret=crypto.encrypt(secrets if secrets is not None else {"url": DISCORD_URL})
                          if kind in senders.NEEDS_SECRETS else "")
        s.add(channel)
        s.commit()
        return channel.id


def all_rows(app, model, *where):
    with app.app_context():
        return db.session().scalars(select(model).where(*where)).all()


def down_for(minutes):
    return lambda moment: not (NINE <= moment < NINE + timedelta(minutes=minutes))


def play(app, minutes):
    with app.app_context():
        worker.simulate(db.session(), NINE, NINE + timedelta(minutes=minutes))


# ---------- Down and up ----------

def test_an_incident_sends_a_down_and_an_up_alert_to_every_channel(app, owner, service):
    _, workspace_id = owner
    add_channel(app, workspace_id)
    add_channel(app, workspace_id, kind="practice", name="Practice", settings={"looks_like": "email"})
    scripted_monitor(app, workspace_id, down_for(6))
    play(app, 15)
    rows = all_rows(app, Alert)
    assert sorted((alert.event, alert.status) for alert in rows) == [("down", "sent")] * 2 + [("up", "sent")] * 2
    discord = [call for call in service.calls if call["url"] == DISCORD_URL]
    assert [call["json"]["embeds"][0]["title"] for call in discord] == ["Payments API is down", "Payments API is back up"]
    assert discord[0]["json"]["embeds"][0]["color"] == senders.RED and "/ack/" in discord[0]["json"]["embeds"][0]["description"]
    assert len(all_rows(app, PracticeMessage)) == 2


def test_the_same_alert_is_never_made_twice(app, owner, service):
    _, workspace_id = owner
    add_channel(app, workspace_id)
    monitor_id = scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 5)
    with app.app_context():
        s = db.session()
        incident = s.scalar(select(Incident))
        monitor = incident.monitor
        assert alerts.raise_alert(s, monitor, incident, "down", NINE) == []  # already made
        s.commit()
    assert len(all_rows(app, Alert)) == 1 and monitor_id


# ---------- Retries, giving up, the breaker ----------

def test_retry_after_is_obeyed(app, owner, service):
    _, workspace_id = owner
    add_channel(app, workspace_id)
    service.answers = [(429, {"message": "You are being rate limited.", "retry_after": 30}, {"Retry-After": "30"})]
    scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 5)
    [alert] = all_rows(app, Alert)
    assert alert.status == "sent" and alert.tries == 2 and len(service.calls) == 2


def test_a_429_waits_exactly_as_long_as_the_service_asked(app, owner, service, monkeypatch):
    _, workspace_id = owner
    channel_id = add_channel(app, workspace_id)
    service.answers = [(429, {}, {"Retry-After": "45"})]
    with app.app_context():
        s = db.session()
        alert = alerts.make(s, s.get(Channel, channel_id), "test", "Test", "", "", NINE)
        s.commit()
        worker.run_until_idle(s, now_fn=lambda: NINE)
        job = s.scalar(select(Job).where(Job.kind == "deliver_alert"))
        assert job.status == queue.QUEUED and job.run_at == NINE + timedelta(seconds=45)
        assert s.get(Alert, alert.id).status == "retrying"
        assert "429" in s.get(Channel, channel_id).last_error


def test_a_deleted_webhook_gives_up_at_once(app, owner, service):
    _, workspace_id = owner
    channel_id = add_channel(app, workspace_id)
    service.answers = [(404, {"message": "Unknown Webhook"}, {})]
    with app.app_context():
        s = db.session()
        alert = alerts.make(s, s.get(Channel, channel_id), "test", "Test", "", "", NINE)
        s.commit()
        worker.simulate(s, NINE, NINE + timedelta(hours=2))
        assert s.get(Alert, alert.id).status == "failed"
        assert "Unknown Webhook" in s.get(Alert, alert.id).last_error
        job = s.scalar(select(Job).where(Job.kind == "deliver_alert"))
        assert job.status == queue.DEAD and job.attempts == 1  # no point trying again
    assert len(service.calls) == 1


def test_the_breaker_rests_a_failing_channel(app, owner, service):
    _, workspace_id = owner
    channel_id = add_channel(app, workspace_id)
    service.answers = [(503, {}, {})] * 5
    with app.app_context():
        s = db.session()
        channel = s.get(Channel, channel_id)
        for n in range(5):
            alerts.make(s, channel, "test", f"Test {n}", "", "", NINE)
        s.commit()
        worker.run_until_idle(s, now_fn=lambda: NINE)
        channel = s.get(Channel, channel_id)
        assert channel.fail_streak == 5 and channel.paused_until == NINE + alerts.BREAKER_REST
        # While it rests, its alerts wait without using up tries.
        worker.run_until_idle(s, now_fn=lambda: NINE + timedelta(minutes=1))
        assert len(service.calls) == 5
        # After the rest, they go out (the service works again).
        worker.simulate(s, NINE + timedelta(minutes=1), NINE + timedelta(hours=3))
        assert {alert.status for alert in s.scalars(select(Alert))} == {"sent"}
        assert s.get(Channel, channel_id).fail_streak == 0


# ---------- Secrets and the channel pages ----------

def test_webhook_addresses_are_kept_encrypted(app, owner):
    client, workspace_id = owner
    response = post(client, "/alerts/new/discord", {"name": "Discord #ops", "url": DISCORD_URL})
    assert response.status_code == 302
    [channel] = all_rows(app, Channel)
    assert DISCORD_URL not in channel.secret and "discord.com" not in channel.secret
    with app.app_context():
        assert crypto.decrypt(channel.secret) == {"url": DISCORD_URL}
    assert DISCORD_URL not in text(client.get("/alerts"))  # never shown again


@pytest.mark.parametrize("url", ["http://10.0.0.1/hook", "https://evil.example/api/webhooks/1/x",
                                 "https://discord.com.evil.example/api/webhooks/1/x", ""])
def test_only_real_discord_webhooks_are_taken(app, owner, url):
    client, _ = owner
    response = post(client, "/alerts/new/discord", {"name": "Discord", "url": url})
    assert response.status_code == 400 and "Paste the webhook address" in text(response)


def test_telegram_finds_the_chat_that_wrote_to_the_bot(app, owner, service):
    client, _ = owner
    token = "123456789:" + "A" * 35
    service.answers = [(200, {"ok": True, "result": [{"message": {"chat": {"id": 4242, "first_name": "Maria",
                                                                           "last_name": "Santos"}}}]}, {})]
    page = text(post(client, "/alerts/new/telegram", {"name": "On call", "token": token}))
    assert "Send alerts to which chat?" in page and "Maria Santos" in page
    response = post(client, "/alerts/new/telegram", {"name": "On call", "token": token, "chat_id": "4242|Maria Santos"})
    assert response.status_code == 302
    [channel] = all_rows(app, Channel)
    assert channel.settings == {"chat_id": "4242", "chat_name": "Maria Santos"}
    with app.app_context():
        assert crypto.decrypt(channel.secret) == {"token": token}


def test_email_needs_brevo_on_the_server(app, owner, service):
    client, workspace_id = owner
    assert "Email needs BREVO_API_KEY" in words(client.get("/alerts"))
    app.config.update(BREVO_API_KEY="key", BREVO_SENDER="alerts@example.com")
    assert post(client, "/alerts/new/email", {"name": "Me", "address": "maria@example.com"}).status_code == 302
    post(client, f"/alerts/{all_rows(app, Channel)[0].id}/test")
    with app.app_context():
        worker.run_until_idle(db.session())
    [call] = service.calls
    assert call["url"] == "https://api.brevo.com/v3/smtp/email"
    assert call["json"]["to"] == [{"email": "maria@example.com"}] and call["headers"].get("Api-key") == "key"


def test_a_test_alert_lands_in_the_practice_inbox(app, owner):
    client, workspace_id = owner
    post(client, "/alerts/new/practice", {"name": "Practice", "looks_like": "telegram"})
    [channel] = all_rows(app, Channel)
    post(client, f"/alerts/{channel.id}/test")
    with app.app_context():
        worker.simulate(db.session(), utcnow(), utcnow() + timedelta(minutes=5))
    page = words(client.get("/alerts/inbox"))
    assert "Test alert from Pulse Check" in page and "Telegram · Practice" in page
    assert "Sent" in words(client.get("/alerts/sent"))


def test_a_paused_channel_gets_nothing(app, owner, service):
    client, workspace_id = owner
    channel_id = add_channel(app, workspace_id)
    post(client, f"/alerts/{channel_id}/toggle")
    scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 5)
    assert all_rows(app, Alert) == [] and service.calls == []


def test_phones_can_turn_alerts_on(app, owner):
    client, workspace_id = owner
    subscription = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "key", "auth": "secret"}}
    response = client.post("/alerts/devices", json=subscription, headers={"X-CSRF-Token": CSRF})
    assert response.status_code == 200
    [device] = all_rows(app, PushSubscription)
    assert device.workspace_id == workspace_id and device.endpoint == subscription["endpoint"]
    assert client.post("/alerts/devices", json={"endpoint": "http://x"}, headers={"X-CSRF-Token": CSRF}).status_code == 400


def test_push_without_devices_says_so(app, owner):
    app.config.update(VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv")
    _, workspace_id = owner
    channel_id = add_channel(app, workspace_id, kind="push", name="Phones")
    with app.app_context():
        s = db.session()
        channel = s.get(Channel, channel_id)
        result = senders.send(s, channel, Alert(id=1, event="test", title="Test", body="", link=""), NINE)
        assert isinstance(result, senders.Rejected) and "No phone or browser" in result.reason


# ---------- Acknowledge ----------

def test_the_acknowledge_link_needs_a_press_not_just_a_visit(app, owner, service):
    _, workspace_id = owner
    add_channel(app, workspace_id)
    scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 5)
    link = service.calls[0]["json"]["embeds"][0]["description"].split("Acknowledge: ")[1]
    path = link.replace("http://localhost", "")
    stranger = app.test_client()
    with stranger.session_transaction() as session:
        session["csrf"] = CSRF
    page = words(stranger.get(path))
    assert "Payments API" in page and "I'm on it" in page
    [incident] = all_rows(app, Incident)
    assert incident.acked_at is None  # opening the link (a chat preview does that) changes nothing
    post(stranger, path)
    [incident] = all_rows(app, Incident)
    assert incident.acked_at is not None and incident.acked_by == "Someone with the link"
    assert stranger.get(path.replace("/ack/", "/ack/1.") + "x").status_code == 404


def test_acknowledging_from_the_incident_page(app, owner):
    client, workspace_id = owner
    scripted_monitor(app, workspace_id, lambda moment: False)
    play(app, 5)
    [incident] = all_rows(app, Incident)
    assert "I'm on it" in words(client.get(f"/incidents/{incident.id}"))
    post(client, f"/incidents/{incident.id}/ack")
    page = words(client.get(f"/incidents/{incident.id}"))
    assert "On it Maria Santos" in page


# ---------- The demo ----------

def test_the_demo_has_practice_channels_and_an_inbox(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    post(client, "/demo")
    with app.app_context():
        worker.run_until_idle(db.session())
        assert db.session().scalar(select(func.count()).select_from(Channel).where(Channel.kind == "practice")) == 4
    page = words(client.get("/alerts/inbox"))
    assert "Payments API is down" in page and "Payments API is back up" in page and "Shipping API is down" in page
    assert "Discord · Discord #ops" in page and "Phone notification" in page
    assert "Retrying" in words(client.get("/alerts/sent")) or "Sent" in words(client.get("/alerts/sent"))
    assert post(client, "/alerts/new/discord", {"name": "x", "url": DISCORD_URL}).status_code == 403
