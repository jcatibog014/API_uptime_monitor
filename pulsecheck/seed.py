"""Try Demo: a sandbox workspace of practice monitors, with a past.

Every visitor who presses Try the demo gets their own workspace, "Lakbay
Travel" (made up), run by Rina Bautista (made up). Its monitors check the
practice services (practice.py), never a real site, and it is deleted 24
hours later (purge(), also run by the timer).

THE PAST IS REPLAYED, NOT WRITTEN BY HAND. build() (quick, inside the Try
Demo click) makes the monitors with their first check 3 hours ago, marks the
workspace as replaying, and queues a demo_history job. That job plays those
3 hours through the REAL scheduler, queue, checks and incident code with
worker.simulate(), jumping from one due moment to the next without waiting.
So every check, re-check, Irregular, Flatline and recovery in the demo
happened exactly the way it would have live. (The replay runs each due check
straight away instead of as a queued job, so 3 hours take seconds; re-checks
still go through the queue.) While replaying, the live crew
leaves the workspace alone (queue.in_scope); afterwards it simply carries on.

The Shipping API goes down 25 minutes before the visitor arrives and comes
back 30 minutes after, so a visitor sees an open incident close by itself.
"""
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update

from . import auth, practice, queue, worker
from .dates import utcnow
from .models import Job, Monitor, Worker, Workspace

LIFETIME = timedelta(hours=24)
REPLAY = timedelta(hours=3)
MAX_DEMOS = 20  # every demo checks 8 practice services every minute; the free database has room for this many
PERSON = ("Rina Bautista", "rina")
WORKSPACE_NAME = "Lakbay Travel"
SHIPPING_DOWN_BEFORE = timedelta(minutes=25)
SHIPPING_DOWN_FOR = 55  # minutes
# The Payments API's outages come every few hours (practice.py); one is set inside the replayed
# hours too, so every demo has an incident that is already over.
PAYMENTS_DOWN_BEFORE = timedelta(minutes=105)
# (name, practice service, how often in seconds)
MONITORS = [
    ("Website", "website", 60),
    ("Login", "login", 60),
    ("Booking API", "booking-api", 60),
    ("Payments API", "payments-api", 60),
    ("Search API", "search-api", 60),
    ("Old Partner API", "partner-api", 120),
    ("Shipping API", "shipping-api", 60),
    ("Images CDN", "images-cdn", 300),
]
# More sample data from later stages: function(s, workspace, monitors_by_service, start, now).
# Runs in build(), before the replay.
extras = []
# Runs after the replay: function(s, workspace, now).
after_replay = []


def count(s):
    return s.scalar(select(func.count()).select_from(Workspace).where(Workspace.is_demo.is_(True)))


def build(s, now):
    """A new demo workspace and its user (the caller commits). The past is replayed by a job."""
    workspace = Workspace(name=WORKSPACE_NAME, is_demo=True, demo_expires_at=now + LIFETIME, replaying=True,
                          created_at=now)
    s.add(workspace)
    s.flush()
    name, handle = PERSON
    user = auth.create_user(s, workspace, name, f"{handle}-{secrets.token_hex(6)}@lakbay-travel.invalid",
                            secrets.token_urlsafe(24), is_demo=True,
                            # Random, so a cookie from a deleted demo can never fit a newer one.
                            session_version=secrets.randbelow(1_000_000_000) + 2)
    start = (now - REPLAY).replace(second=0, microsecond=0)
    by_service = {}
    for monitor_name, service, interval in MONITORS:
        params = {}
        if service == "shipping-api":
            down = (now - SHIPPING_DOWN_BEFORE).replace(second=0, microsecond=0)
            params = {"down": down.isoformat(timespec="minutes"), "for": SHIPPING_DOWN_FOR}
        elif service == "payments-api":
            down = (now - PAYMENTS_DOWN_BEFORE).replace(second=0, microsecond=0)
            params = {"down": down.isoformat(timespec="minutes"), "for": 7}
        monitor = Monitor(workspace_id=workspace.id, name=monitor_name, kind="practice",
                          url=practice.address(service, **params), interval_s=interval, next_check_at=start,
                          created_at=start, state="new")
        s.add(monitor)
        by_service[service] = monitor
    s.flush()
    for extra in extras:
        extra(s, workspace, by_service, start, now)
    queue.enqueue(s, "demo_history", {"workspace_id": workspace.id, "start": start.isoformat()}, run_at=now,
                  priority=1, max_attempts=2, dedupe_key=f"demo-history:{workspace.id}")
    return user


@queue.handler("demo_history")
def replay(s, job, now):
    """Play the demo's past through the real crew, then hand it to the live crew."""
    workspace = s.get(Workspace, job.payload["workspace_id"])
    if workspace is None:
        return  # deleted already
    start = datetime.fromisoformat(job.payload["start"])
    name = f"replay-{workspace.id}-{secrets.token_hex(2)}"

    kept = {"at": utcnow()}

    def keep_the_lease():
        if (utcnow() - kept["at"]).total_seconds() > 10:  # real time: leases are real time
            queue.heartbeat(s, job, job.locked_by, utcnow())
            kept["at"] = utcnow()

    worker.simulate(s, start, now, worker_id=name, workspace_id=workspace.id, on_round=keep_the_lease, direct=True)
    s.execute(delete(Worker).where(Worker.id == name))  # a replay isn't a member of the live crew
    for extra in after_replay:
        extra(s, workspace, now)
    s.execute(update(Workspace).where(Workspace.id == workspace.id).values(replaying=False)
              .execution_options(synchronize_session=False))
    worker.nudge()


def purge(s, moment):
    """Delete demos whose 24 hours are up (everything of theirs goes with them). Returns how many."""
    old = s.scalars(select(Workspace.id).where(Workspace.is_demo.is_(True), Workspace.demo_expires_at <= moment)).all()
    if old:
        s.execute(delete(Job).where(Job.workspace_id.in_(old)))
        s.execute(delete(Workspace).where(Workspace.id.in_(old)))
    s.commit()
    return len(old)


def init_app(app):
    from . import tick
    tick.tidy_jobs["demos_deleted"] = purge
