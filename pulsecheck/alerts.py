"""Alerts: telling people when a monitor goes down and comes back, reliably.

When an incident opens or closes (incidents.on_open / on_resolve), raise_alert()
makes one Alert row per enabled channel and queues a deliver_alert job for
each. The alerting lesson is in what happens next:

- ONE ALERT, ONCE. Alert is UNIQUE per incident + channel + event + seq, so an
  incident that is judged twice can't make two "down" alerts.
- RETRIED UNTIL IT ARRIVES. A TryLater (429, 5xx, timeout) becomes queue.Retry:
  backoff with full jitter, or the service's own Retry-After when it sent one
  (Discord and Telegram do). After 6 tries the job is a dead letter, shown on
  the Crew page with Retry now.
- NEVER RETRY WHAT CAN'T WORK. A Rejected (a deleted webhook, a wrong token)
  gives up at once: retrying would only hammer the service.
- A BREAKER PER CHANNEL. After 5 failures in a row, a channel rests for 5
  minutes: its alerts wait (queue.Defer, which uses no try) instead of each
  burning its tries against a service that is down. Then one alert tries,
  and a success closes the breaker.
- COMMIT BEFORE CALLING OUT. The try is counted and saved before the network
  call, so a crash in the middle never loses it.

Every alert carries a signed Acknowledge link (tokens.py): whoever opens it
can say "I'm on it" without logging in; reminders stop (stage 7).
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import queue, senders, tokens
from .formatting import clock, duration, nice_datetime
from .models import Alert, Channel, Workspace

BREAKER_AFTER = 5  # failures in a row
BREAKER_REST = timedelta(minutes=5)
MAX_TRIES = 6
WAITING, SENT, RETRYING, FAILED, SKIPPED = "waiting", "sent", "retrying", "failed", "skipped"


def site(path):
    return current_app.config["BASE_URL"] + path


def ack_link(incident):
    return site(f"/ack/{tokens.make('ack', incident.id)}")


def compose(event, monitor, incident, workspace, now):
    """(title, body, link) of an alert, in plain words."""
    zone_name = workspace.time_zone
    from .pulse_words import error_label
    if event == "down":
        return (f"{monitor.name} is down",
                f"Down since {clock(incident.opened_at, zone_name)} ({zone_name.replace('_', ' ')}).\n"
                f"{error_label(incident.cause)}: {incident.message}\n"
                f"It failed {incident.failed_checks} checks in a row.\n"
                f"On it? Acknowledge: {ack_link(incident)}",
                site(f"/incidents/{incident.id}"))
    if event == "up":
        return (f"{monitor.name} is back up",
                f"Back up at {clock(incident.resolved_at, zone_name)}, after being down for "
                f"{duration((incident.resolved_at - incident.opened_at).total_seconds())}.",
                site(f"/incidents/{incident.id}"))
    return ("Test alert from Pulse Check",
            f"If you can read this, alerts reach this channel. Sent {nice_datetime(now, zone_name)}.",
            site("/alerts"))


def enabled_channels(s, workspace_id):
    return s.scalars(select(Channel).where(Channel.workspace_id == workspace_id, Channel.enabled.is_(True))
                     .order_by(Channel.id)).all()


def make(s, channel, event, title, body, link, now, incident=None, seq=0):
    """One Alert, queued for delivery (the caller commits). None if it already exists."""
    alert = Alert(workspace_id=channel.workspace_id, channel_id=channel.id, incident_id=incident.id if incident else None,
                  event=event, seq=seq, title=title[:200], body=body, link=link[:500], created_at=now)
    try:
        with s.begin_nested():  # UNIQUE incident + channel + event + seq: never the same alert twice
            s.add(alert)
    except IntegrityError:
        return None
    queue.enqueue(s, "deliver_alert", {"alert_id": alert.id}, run_at=now, workspace_id=channel.workspace_id,
                  priority=2, max_attempts=MAX_TRIES, dedupe_key=f"alert:{alert.id}")
    return alert


def raise_alert(s, monitor, incident, event, now, channels=None, seq=0):
    workspace = s.get(Workspace, monitor.workspace_id)
    title, body, link = compose(event, monitor, incident, workspace, now)
    made = []
    for channel in channels if channels is not None else enabled_channels(s, monitor.workspace_id):
        alert = make(s, channel, event, title, body, link, now, incident, seq)
        if alert is not None:
            made.append(alert)
    return made


def raise_monitor_alert(s, monitor, event, seq, now, title, body):
    """An alert about a monitor that isn't an incident (a certificate running out). The caller
    makes sure it is raised once (certificates.py remembers the last warning)."""
    made = []
    for channel in enabled_channels(s, monitor.workspace_id):
        made.append(make(s, channel, event, title, body, site(f"/monitors/{monitor.id}"), now, seq=seq))
    return [alert for alert in made if alert is not None]


def send_test(s, channel, now):
    workspace = s.get(Workspace, channel.workspace_id)
    title, body, link = compose("test", None, None, workspace, now)
    # Tests have no incident, so the UNIQUE rule doesn't stop a second one: that is wanted.
    return make(s, channel, "test", title, body, link, now)


@queue.handler("deliver_alert", on_dead=lambda s, job, now: _give_up(s, job.payload["alert_id"], job.last_error))
def deliver(s, job, now):
    alert = s.get(Alert, job.payload["alert_id"])
    if alert is None or alert.status in (SENT, FAILED, SKIPPED):
        return
    channel = alert.channel
    if not channel.enabled:
        alert.status, alert.last_error = SKIPPED, "The channel was paused."
        return
    if channel.paused_until is not None and channel.paused_until > now:
        raise queue.Defer(channel.paused_until, "The channel is resting after failures in a row (the breaker).")
    alert.tries += 1
    s.commit()  # the try counts even if we crash mid-call
    result = senders.send(s, channel, alert, now)
    if isinstance(result, senders.Delivered):
        alert.status, alert.sent_at, alert.last_error = SENT, now, ""
        channel.fail_streak, channel.paused_until, channel.last_sent_at, channel.last_error = 0, None, now, ""
        return
    alert.last_error = channel.last_error = result.reason[:300]
    if isinstance(result, senders.Rejected):
        alert.status = FAILED
        channel.fail_streak += 1
        s.commit()
        raise queue.GiveUp(result.reason)
    alert.status = RETRYING
    channel.fail_streak += 1
    if channel.fail_streak >= BREAKER_AFTER:
        channel.paused_until = now + BREAKER_REST
    if result.after:  # the service said when: nobody tries before then
        resume = now + timedelta(seconds=result.after)
        channel.paused_until = max(channel.paused_until or resume, resume)
    s.commit()  # keep these: the Retry below rolls back anything not committed
    raise queue.Retry(result.reason, after=result.after)


def _give_up(s, alert_id, error):
    alert = s.get(Alert, alert_id)
    if alert is not None and alert.status != SENT:
        alert.status, alert.last_error = FAILED, (error or "")[:300]


def acknowledge(s, incident, who, now):
    if incident.acked_at is None:
        incident.acked_at, incident.acked_by = now, who[:80]
        return True
    return False


DEMO_CHANNELS = [("Team email", "email"), ("Discord #ops", "discord"), ("Telegram on-call", "telegram"),
                 ("Rina's phone", "push")]


def demo_channels(s, workspace, by_service, start, now):
    """seed.extras: the demo's channels, all practice ones, made before its past is replayed,
    so the replayed incidents send their alerts to the Practice Inbox."""
    for name, looks_like in DEMO_CHANNELS:
        s.add(Channel(workspace_id=workspace.id, kind="practice", name=name, settings={"looks_like": looks_like},
                      created_at=start))
    s.flush()


def on_open(s, monitor, incident, now):
    raise_alert(s, monitor, incident, "down", now)


def on_resolve(s, monitor, incident, now):
    raise_alert(s, monitor, incident, "up", now)


def init_app(app):
    from . import incidents
    from . import seed
    if on_open not in incidents.on_open:
        incidents.on_open.append(on_open)
        incidents.on_resolve.append(on_resolve)
    if demo_channels not in seed.extras:
        seed.extras.insert(0, demo_channels)
