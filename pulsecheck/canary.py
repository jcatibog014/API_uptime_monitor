"""The canary: Pulse Check checking itself, every minute.

Two checks, for two reasons:

- SELF: Pulse Check's own address (BASE_URL/healthz). On Render this goes out
  through Render's front door and back in, which counts as a visitor, so the
  free plan never puts the site to sleep (it sleeps after 15 minutes without
  one). A sleeping monitor checks nothing.
- OUTSIDE: an address on the internet that is always up (CANARY_URL). If even
  that one fails, the problem is almost surely OUR connection, not the
  services we watch. A monitor that fails while the outside check is failing
  gets an "Our Side" check: it isn't counted and opens no incident. Without
  this guard, one network blip on our server would page you for every
  service at once.

Like a canary in a coal mine: if the canary is in trouble, don't blame the mine.
"""
from datetime import timedelta

from flask import current_app
from sqlalchemy import delete, select

from . import probe, queue, timers
from .models import CanaryCheck

EVERY = 60  # seconds
FRESH = timedelta(minutes=3)  # a canary result older than this tells us nothing
KEEP = timedelta(days=2)
OUTSIDE_DEFAULT = "https://www.gstatic.com/generate_204"  # a tiny "204 No Content" page made for this


def targets(config):
    """[(name, url, allow_private)] that this site checks. Tests have none."""
    found = []
    if config.get("SELF_CHECK"):
        # Our own address, set by us (an environment variable), never typed by a visitor:
        # on your PC it is 127.0.0.1, so the private-network guard is off for it.
        found.append(("self", config["BASE_URL"] + "/healthz", True))
    if config.get("CANARY_URL"):
        found.append(("outside", config["CANARY_URL"], False))
    return found


def every_round(s, now, workspace_id):
    """Queue the canary once a minute (one worker wins the timer)."""
    if workspace_id is not None or not targets(current_app.config):
        return  # a demo's replay doesn't check the real network
    if timers.claim(s, "canary", now, EVERY):
        queue.enqueue(s, "canary", run_at=now, priority=1, max_attempts=1,
                      dedupe_key=f"canary:{now.isoformat(timespec='minutes')}")
    s.commit()


def wake_up(s, now, workspace_id):
    if workspace_id is not None or not targets(current_app.config):
        return None
    return timers.next_at(s, "canary")


@queue.handler("canary")
def canary_job(s, job, now):
    s.commit()  # nothing open while we wait for answers
    for name, url, allow_private in targets(current_app.config):
        result = probe.check(url, expected="200-399", timeout=10, allow_private=allow_private)
        s.add(CanaryCheck(target=name, at=now, ok=result.ok, total_ms=result.total_ms, message=result.message[:300]))


def latest(s):
    """{"self": CanaryCheck, "outside": CanaryCheck} (the newest of each, when there is one)."""
    found = {}
    for name in ("self", "outside"):
        row = s.scalar(select(CanaryCheck).where(CanaryCheck.target == name).order_by(CanaryCheck.at.desc()).limit(1))
        if row is not None:
            found[name] = row
    return found


def our_side_down(s, now):
    """True if the newest outside check is recent and failed: our own connection is in trouble."""
    row = s.scalar(select(CanaryCheck).where(CanaryCheck.target == "outside", CanaryCheck.at >= now - FRESH)
                   .order_by(CanaryCheck.at.desc()).limit(1))
    return row is not None and not row.ok


def tidy(s, moment):
    return s.execute(delete(CanaryCheck).where(CanaryCheck.at < moment - KEEP)).rowcount


def init_app(app):
    from . import tick, worker
    if every_round not in worker.every_round:
        worker.every_round.append(every_round)
        worker.wake_ups.append(wake_up)
    tick.tidy_jobs["canary_checks_deleted"] = tidy
