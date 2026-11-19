"""Heartbeat monitors: instead of Pulse Check calling a service, a job calls Pulse Check.

Some things can't be checked from outside: a nightly backup, a cron job, a
queue worker. So the monitor gets its own secret address, /hb/<token>, and
the job calls it whenever it finishes (curl, a browser, anything):

    curl -fsS https://<your-site>/hb/9fK2...

Each call is a ping: a good check, and the next one is expected within the
monitor's interval plus its grace time (a job that runs at 2:00 may finish at
2:07). If no ping comes by then, the scheduler's check falls due and that is
the failure: "No ping since 2:05 AM" (rules.heartbeat_prober). Heartbeats
are confirmed and recovered by one check, not three: a missing ping has
already waited out its grace time, and a ping is unmistakable.
"""
import secrets
from datetime import timedelta

from flask import Blueprint, abort

from . import checking, db, limits, probe, scheduler
from .dates import utcnow
from .models import Monitor

bp = Blueprint("heartbeat", __name__)
PINGS_PER_MINUTE = 30
# How often a ping is expected, and how late it may be.
EVERY = [(300, "Every 5 minutes"), (900, "Every 15 minutes"), (1800, "Every 30 minutes"), (3600, "Every hour"),
         (6 * 3600, "Every 6 hours"), (12 * 3600, "Every 12 hours"), (86400, "Every day")]
GRACE = [(60, "1 minute"), (300, "5 minutes"), (900, "15 minutes"), (1800, "30 minutes"), (3600, "1 hour")]


def new_token():
    return secrets.token_urlsafe(18)


def expected_by(monitor, moment):
    return moment + timedelta(seconds=monitor.interval_s + monitor.grace_s)


def ping(s, monitor, now):
    """Record a ping: a good check, judged like any other; the next one is due later."""
    result = probe.Result(ok=True, message="Ping received.")
    check = checking.record(s, monitor, result, now, now)
    for hook in scheduler.after_check:
        hook(s, monitor, check, result, now)
    monitor.last_ping_at = now
    if not monitor.paused:
        monitor.next_check_at = expected_by(monitor, now)
    return check


@bp.route("/hb/<token>", methods=["GET", "POST", "HEAD"])
def receive(token):
    """No login and no form token: the secret address is the proof. Says only "ok"."""
    s = db.session()
    monitor = s.query(Monitor).filter(Monitor.heartbeat_token == token, Monitor.kind == "heartbeat").one_or_none()
    if monitor is None or len(token) > 40:
        abort(404)
    now = utcnow()
    if not limits.hit(s, f"ping:{monitor.id}", PINGS_PER_MINUTE, 60, now):
        s.commit()
        abort(429, "Too many pings. One a minute is plenty.")
    if monitor.paused:
        s.commit()
        return "paused\n", 200, {"Content-Type": "text/plain"}
    ping(s, monitor, now)
    s.commit()
    return "ok\n", 200, {"Content-Type": "text/plain"}


# ---------- The demo's heartbeat: a nightly backup ----------

DEMO_NAME = "Nightly backup"
DEMO_PING = (2, 4)  # the backup finishes at about 2:04 AM, the workspace's time
DEMO_MISSED = 4  # it didn't run 4 nights ago
DEMO_DAYS = 90


def demo_heartbeat(s, workspace, by_service, start, now):
    """seed.extras: a heartbeat monitor with pings every night for two weeks (raw checks) and
    90 days of day rows, and one missed night: a "No ping" incident that lasted until the next."""
    from datetime import time as clock

    from .dates import at_local, to_zone
    from .metrics import local_midnight
    from .models import BUCKETS, Check, Incident, Monitor, Rollup
    zone_name = workspace.time_zone
    monitor = Monitor(workspace_id=workspace.id, name=DEMO_NAME, kind="heartbeat", url="", interval_s=86400,
                      grace_s=1800, confirm_tries=1, recover_tries=1, heartbeat_token=new_token(), created_at=start,
                      state="steady")
    s.add(monitor)
    s.flush()
    today = to_zone(now, zone_name).date()
    pings = []
    for back in range(DEMO_DAYS, -1, -1):
        day = today - timedelta(days=back)
        moment = at_local(day, clock(*DEMO_PING, 10 + back % 40), zone_name)
        if moment > now:
            continue
        pings.append((back, moment))
    missed_at = None
    for back, moment in pings:
        midnight = local_midnight(moment, zone_name)
        ok = back != DEMO_MISSED
        row = Rollup(monitor_id=monitor.id, period="day", start=midnight, checks=1, ok=int(ok), slow=0, our_side=0,
                     missed=0, sum_ms=0.0, **{f"b{n}": 0 for n in range(BUCKETS)})
        s.add(row)
        if back > 14:
            continue  # raw checks are kept for 14 days
        if ok:
            s.add(Check(monitor_id=monitor.id, scheduled_at=moment, started_at=moment, ok=True, message="Ping received."))
            if missed_at is not None:
                s.add(Incident(workspace_id=workspace.id, monitor_id=monitor.id, opened_at=missed_at,
                               confirmed_at=missed_at, resolved_at=moment, cause="missing",
                               message="No ping since the night before.", failed_checks=1,
                               acked_at=missed_at + timedelta(minutes=12), acked_by="Rina Bautista"))
                missed_at = None
            monitor.last_ping_at = moment
        else:
            missed_at = moment + timedelta(seconds=monitor.grace_s)
            s.add(Check(monitor_id=monitor.id, scheduled_at=missed_at, started_at=missed_at, ok=False,
                        error_kind="missing", message="No ping since the night before."))
    monitor.state_since = monitor.last_ping_at
    monitor.judged_until = monitor.last_ping_at
    monitor.next_check_at = monitor.last_ping_at + timedelta(seconds=monitor.interval_s + monitor.grace_s)
    by_service["nightly-backup"] = monitor


def init_app(app):
    from . import seed
    if demo_heartbeat not in seed.extras:
        seed.extras.append(demo_heartbeat)
    exempt = set(app.config.get("CSRF_EXEMPT", ()))
    exempt.add("heartbeat.receive")  # a job calls it, with the secret address instead of a form token
    app.config["CSRF_EXEMPT"] = exempt
