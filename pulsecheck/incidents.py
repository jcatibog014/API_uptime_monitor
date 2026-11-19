"""Failure handling: when is a service really down, and when is it back?

One failed check is not an outage. Networks drop a packet, a server restarts
for a second. Paging someone for every blip teaches them to ignore pages. So
a monitor moves through states (pulse_words.STATE), one check at a time:

    Steady --fail--> Irregular --fail--> ... --fail (confirm_tries in a row)--> Flatline
       ^                 |                                                        |
       +------ok---------+            Flatline --ok (recover_tries in a row)--> Steady

- CONFIRM BEFORE ALERTING. A failure makes the monitor Irregular and asks for
  a quick re-check (20 s, then 40 s later) instead of waiting for the next
  minute. Only confirm_tries failures IN A ROW (3 at first) open an Incident,
  and the incident starts at the FIRST failure (that is when users noticed).
- RECOVER WITH HYSTERESIS. A down service that answers once may fall over
  again a second later. The incident closes only after recover_tries good
  checks in a row (2 at first); meanwhile it stays a Flatline.
- NEVER COUNT A CHECK TWICE. Jobs run at least once, not exactly once, and a
  re-check can finish after a newer check. judged_until remembers the newest
  check already judged; anything older or the same is skipped.
- TWO WORKERS CAN'T BOTH OPEN AN INCIDENT. Monitor has a version column
  (SQLAlchemy's version_id_col): saving a monitor from a stale copy fails with
  StaleDataError, and the check job reads it again and judges again.
- NOT OUR FAULT, NOT COUNTED. If Pulse Check's own outside check is failing
  (canary.py), a failure is marked "our side" and judged no further.

Later stages hang alerts on on_open / on_resolve.
"""
from datetime import timedelta

from sqlalchemy import func, select

from . import canary, queue
from .dates import utcnow
from .models import Incident, Monitor

RECHECKS = (20, 40)  # seconds after a failure (the 1st, then the 2nd and later) while Irregular
# Hooks: function(s, monitor, incident, now). Alerts (stage 5) use them.
on_open = []
on_resolve = []
# Called on every state change: function(s, monitor, old_state, now).
on_move = []
NETWORK_KINDS = ("http", "tcp")  # kinds that go over our connection (the canary can excuse them)


def set_state(s, monitor, state, moment):
    if monitor.state == state:
        return
    old, monitor.state, monitor.state_since = monitor.state, state, moment
    for hook in on_move:
        hook(s, monitor, old, moment)


def open_incident(s, monitor_id):
    return s.scalar(select(Incident).where(Incident.monitor_id == monitor_id, Incident.resolved_at.is_(None))
                    .order_by(Incident.id.desc()).limit(1))


def judge(s, monitor, check, now):
    """Move the monitor's state after one check. The caller flushes and commits."""
    if monitor.judged_until is not None and check.scheduled_at <= monitor.judged_until:
        return  # older than (or the same as) a check already judged: never count it twice
    monitor.judged_until = check.scheduled_at
    if monitor.paused:
        return
    if not check.ok and monitor.kind in NETWORK_KINDS and canary.our_side_down(s, now):
        check.our_side = True  # our connection is failing: this says nothing about the service
        return
    incident = open_incident(s, monitor.id)
    if check.ok:
        monitor.fail_streak, monitor.ok_streak = 0, monitor.ok_streak + 1
        if incident is None:
            set_state(s, monitor, "weak" if check.slow else "steady", check.scheduled_at)
        elif monitor.ok_streak >= monitor.recover_tries:
            incident.resolved_at = check.scheduled_at
            set_state(s, monitor, "weak" if check.slow else "steady", check.scheduled_at)
            for hook in on_resolve:
                hook(s, monitor, incident, now)
        else:
            recheck(s, monitor, now, RECHECKS[0])  # looks better: confirm it soon
        return
    monitor.ok_streak, monitor.fail_streak = 0, monitor.fail_streak + 1
    if incident is not None:
        incident.failed_checks += 1
        return
    if monitor.state != "irregular":
        set_state(s, monitor, "irregular", check.scheduled_at)
    if monitor.fail_streak >= monitor.confirm_tries:
        incident = Incident(workspace_id=monitor.workspace_id, monitor_id=monitor.id, opened_at=monitor.state_since,
                            confirmed_at=check.scheduled_at, cause=check.error_kind, message=check.message,
                            failed_checks=monitor.fail_streak)
        s.add(incident)
        s.flush()
        set_state(s, monitor, "flatline", monitor.state_since)
        for hook in on_open:
            hook(s, monitor, incident, now)
    else:
        recheck(s, monitor, now, RECHECKS[min(monitor.fail_streak, len(RECHECKS)) - 1])


def recheck(s, monitor, now, after_seconds):
    """Check again soon, unless the regular check comes first anyway."""
    moment = (now + timedelta(seconds=after_seconds)).replace(microsecond=0)
    if monitor.next_check_at is not None and moment >= monitor.next_check_at - timedelta(seconds=5):
        return
    queue.enqueue(s, "check", {"monitor_id": monitor.id, "scheduled_at": moment.isoformat(), "recheck": True},
                  run_at=moment, workspace_id=monitor.workspace_id, priority=2, max_attempts=2,
                  dedupe_key=f"check:{monitor.id}:{moment.isoformat()}")


def reset_streaks(monitor):
    """After a pause (or a change), the runs of failed and good checks start again from zero.
    An open incident stays open: the next checks decide when it ends."""
    monitor.fail_streak = monitor.ok_streak = 0


def open_count(s, workspace_id):
    return s.scalar(select(func.count()).select_from(Incident)
                    .where(Incident.workspace_id == workspace_id, Incident.resolved_at.is_(None)))


def any_flatline(s, workspace_id):
    return s.scalar(select(func.count()).select_from(Monitor).where(
        Monitor.workspace_id == workspace_id, Monitor.state == "flatline", Monitor.paused.is_(False))) > 0


def duration(incident, now):
    return ((incident.resolved_at or now) - incident.opened_at).total_seconds()


def after_check(s, monitor, check, result, now):
    judge(s, monitor, check, now)


def init_app(app):
    from flask import g

    from . import db, scheduler
    if after_check not in scheduler.after_check:
        scheduler.after_check.append(after_check)

    def workspace_open_count(workspace):
        if workspace is None:
            return 0
        if "open_incidents" not in g:
            g.open_incidents = open_count(db.session(), workspace.id)
        return g.open_incidents

    def workspace_flatline(workspace):
        return workspace is not None and any_flatline(db.session(), workspace.id)

    app.jinja_env.globals.update(open_incidents=workspace_open_count, any_flatline=workspace_flatline,
                                 incident_duration=lambda incident: duration(incident, utcnow()))
