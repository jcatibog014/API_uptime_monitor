"""Alert rules: who hears about what, nagging until someone is on it, and calm during flapping.

ROUTING. A monitor can choose its channels (monitor_channels, level 1). With
none chosen, every channel hears about it. Level-2 channels are the
ESCALATION: they hear only when nobody said "I'm on it" in time.

REMINDERS. While an incident is open and nobody has acknowledged it, the same
channels get a reminder every remind_every_m minutes (at most MAX_REMINDERS).
Acknowledging (the link in the alert, or the incident page) stops them.

ESCALATION. If escalate_after_m minutes pass without an acknowledgement, the
level-2 channels get one alert ("still down, nobody is on it").

FLAPPING. A service that keeps going down and coming back would send a down
and an up alert every few minutes, and people learn to ignore them. If a
monitor opens FLAP_INCIDENTS incidents within FLAP_WINDOW, it is flapping:
one "flapping" alert goes out, and its down/up alerts are held until it has
been in one state for SETTLE minutes. Then one alert says how it settled
(back up, or down: then the incident's down alert goes out after all).

A timer (timers.py) runs check() once a minute in one worker. Every alert is
made through alerts.make(), so the UNIQUE incident + channel + event + seq rule
means a reminder or an escalation is never sent twice, even if two runs overlap.
"""
from datetime import timedelta

from sqlalchemy import func, select

from . import alerts, queue, timers
from .formatting import duration
from .models import Channel, Incident, Monitor, MonitorChannel, Timer

EVERY = 60  # seconds between runs
MAX_REMINDERS = 10
FLAP_INCIDENTS = 3
FLAP_WINDOW = timedelta(minutes=30)
SETTLE = timedelta(minutes=15)
REMIND_CHOICES = [("", "Never"), (15, "Every 15 minutes"), (30, "Every 30 minutes"), (60, "Every hour"),
                  (120, "Every 2 hours")]
ESCALATE_CHOICES = [("", "Never"), (10, "After 10 minutes"), (20, "After 20 minutes"), (30, "After 30 minutes"),
                    (60, "After an hour")]


# ---------- Routing ----------

def channels_for(s, monitor, level=1):
    """The enabled channels of this level for this monitor. Level 1 with nothing chosen: all of them."""
    chosen = s.scalars(select(Channel).join(MonitorChannel, MonitorChannel.channel_id == Channel.id)
                       .where(MonitorChannel.monitor_id == monitor.id, MonitorChannel.level == level,
                              Channel.enabled.is_(True)).order_by(Channel.id)).all()
    if chosen or level != 1:
        return chosen
    if s.scalar(select(func.count()).select_from(MonitorChannel)
                .where(MonitorChannel.monitor_id == monitor.id, MonitorChannel.level == 1)):
        return []  # some were chosen, but they're all paused
    return alerts.enabled_channels(s, monitor.workspace_id)


def set_routing(s, monitor, level_1, level_2):
    """Save the monitor's channels (ids of this workspace's channels only; the caller checks)."""
    s.query(MonitorChannel).filter(MonitorChannel.monitor_id == monitor.id).delete()
    for channel_id in level_1:
        s.add(MonitorChannel(monitor_id=monitor.id, channel_id=channel_id, level=1))
    for channel_id in level_2:
        if channel_id not in level_1:
            s.add(MonitorChannel(monitor_id=monitor.id, channel_id=channel_id, level=2))


# ---------- Opening and closing (incidents.on_open / on_resolve) ----------

def recent_incidents(s, monitor, now):
    return s.scalar(select(func.count()).select_from(Incident).where(
        Incident.monitor_id == monitor.id, Incident.opened_at >= now - FLAP_WINDOW))


def on_open(s, monitor, incident, now):
    if monitor.flapping:
        return  # held: one "flapping" alert was enough
    if recent_incidents(s, monitor, incident.confirmed_at) >= FLAP_INCIDENTS:
        monitor.flapping, monitor.flapping_since = True, incident.confirmed_at
        alerts.raise_alert(s, monitor, incident, "flapping", now, channels=channels_for(s, monitor))
        return
    alerts.raise_alert(s, monitor, incident, "down", now, channels=channels_for(s, monitor))


def on_resolve(s, monitor, incident, now):
    if monitor.flapping:
        return
    alerts.raise_alert(s, monitor, incident, "up", now, channels=channels_for(s, monitor))


# ---------- Once a minute ----------

def check(s, now, workspace_id=None):
    """Reminders, escalations and settling flappers that are due. Returns how many alerts went out."""
    made = 0
    scope = [queue.in_scope(Incident.workspace_id, workspace_id)]  # the live run skips replaying demos
    open_ones = s.scalars(select(Incident).join(Monitor, Monitor.id == Incident.monitor_id).where(
        Incident.resolved_at.is_(None), Incident.acked_at.is_(None), Monitor.paused.is_(False),
        Monitor.flapping.is_(False), *scope)).all()
    for incident in open_ones:
        monitor = incident.monitor
        down_for = now - incident.confirmed_at
        if monitor.remind_every_m:
            due = min(MAX_REMINDERS, int(down_for.total_seconds() // (monitor.remind_every_m * 60)))
            if due > incident.reminders_sent:
                incident.reminders_sent = due
                made += len(alerts.raise_alert(s, monitor, incident, "reminder", now,
                                               channels=channels_for(s, monitor), seq=due))
        if monitor.escalate_after_m and incident.escalated_at is None \
                and down_for >= timedelta(minutes=monitor.escalate_after_m):
            incident.escalated_at = now
            made += len(alerts.raise_alert(s, monitor, incident, "escalation", now,
                                           channels=channels_for(s, monitor, level=2)))
    flap_scope = [queue.in_scope(Monitor.workspace_id, workspace_id)]
    for monitor in s.scalars(select(Monitor).where(Monitor.flapping.is_(True), *flap_scope)).all():
        if monitor.state_since is None or now - monitor.state_since < SETTLE or monitor.state == "irregular":
            continue
        monitor.flapping = False
        incident = s.scalar(select(Incident).where(Incident.monitor_id == monitor.id)
                            .order_by(Incident.opened_at.desc()).limit(1))
        if incident is None:
            continue
        if incident.is_open:  # it settled DOWN: now the down alert is worth sending
            made += len(alerts.raise_alert(s, monitor, incident, "down", now, channels=channels_for(s, monitor)))
        else:
            made += len(alerts.raise_alert(s, monitor, incident, "settled", now, channels=channels_for(s, monitor)))
    s.commit()
    return made


@queue.handler("alert_rules")
def rules_job(s, job, now):
    check(s, now, job.payload.get("workspace_id"))


def every_round(s, now, workspace_id):
    # A demo's replay runs the rules for its own workspace on its own clock (it has a timer row each).
    name = "alert_rules" if workspace_id is None else f"alert_rules:{workspace_id}"
    if timers.claim(s, name, now, EVERY):
        queue.enqueue(s, "alert_rules", {"workspace_id": workspace_id}, run_at=now, workspace_id=workspace_id,
                      priority=3, max_attempts=2, dedupe_key=f"{name}:{now.isoformat(timespec='seconds')}")
    s.commit()


def wake_up(s, now, workspace_id):
    return timers.next_at(s, "alert_rules" if workspace_id is None else f"alert_rules:{workspace_id}")


def after_replay(s, workspace, now):
    """The replay's own timer row isn't needed any more: the live run takes over."""
    s.query(Timer).filter(Timer.name == f"alert_rules:{workspace.id}").delete()


def words_for(event, monitor, incident, now):
    """compose() for the stage 7 events: (title, body)."""
    down = duration((now - incident.opened_at).total_seconds())
    if event == "reminder":
        return (f"{monitor.name} is still down",
                f"Down for {down}, and nobody has said they're on it yet.\n"
                f"On it? Acknowledge: {alerts.ack_link(incident)}")
    if event == "escalation":
        return (f"{monitor.name} is still down, and nobody is on it",
                f"Down for {down}. This alert goes to you because nobody acknowledged it in "
                f"{monitor.escalate_after_m} minutes.\nOn it? Acknowledge: {alerts.ack_link(incident)}")
    if event == "flapping":
        return (f"{monitor.name} is flapping",
                f"It went down and came back {FLAP_INCIDENTS} times in {int(FLAP_WINDOW.total_seconds() // 60)} "
                f"minutes. Its down and up alerts are held until it stays one way for "
                f"{int(SETTLE.total_seconds() // 60)} minutes.")
    return (f"{monitor.name} has settled",
            f"It has been up for {int(SETTLE.total_seconds() // 60)} minutes in a row: the flapping is over.")


def demo_rules(s, workspace, by_service, start, now):
    """seed.extras: the demo's alert rules. The Shipping API (down when the visitor comes) reminds
    every 15 minutes and escalates to Rina's phone after 20; the Payments API reminds every 30."""
    channels = {channel.name: channel for channel in s.scalars(select(Channel).where(
        Channel.workspace_id == workspace.id))}
    shipping, payments = by_service.get("shipping-api"), by_service.get("payments-api")
    if shipping is None or "Rina's phone" not in channels:
        return
    shipping.remind_every_m, shipping.escalate_after_m = 15, 20
    payments.remind_every_m = 30
    team = [channel.id for name, channel in channels.items() if name != "Rina's phone"]
    set_routing(s, shipping, team, [channels["Rina's phone"].id])


def init_app(app):
    from . import incidents, worker
    # Alert rules replace the plain down/up alerts of stage 5.
    if alerts.on_open in incidents.on_open:
        incidents.on_open.remove(alerts.on_open)
        incidents.on_resolve.remove(alerts.on_resolve)
    if on_open not in incidents.on_open:
        incidents.on_open.append(on_open)
        incidents.on_resolve.append(on_resolve)
    from . import seed
    if every_round not in worker.every_round:
        worker.every_round.append(every_round)
        worker.wake_ups.append(wake_up)
    if after_replay not in seed.after_replay:
        seed.after_replay.append(after_replay)
    if demo_rules not in seed.extras:
        seed.extras.append(demo_rules)
    alerts.extra_words["reminder"] = alerts.extra_words["escalation"] = words_for
    alerts.extra_words["flapping"] = alerts.extra_words["settled"] = words_for
