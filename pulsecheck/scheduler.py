"""The scheduler: which monitors are due, and a check job for each.

Each monitor has next_check_at. run_due() finds the due ones and, for each:

1. CLAIMS it with a compare-and-swap:
       UPDATE monitors SET next_check_at = <next> WHERE id = 7 AND next_check_at = <what we read>
   Two schedulers (two workers, or a worker and the timer) may read the same
   due monitor at the same moment; only one UPDATE can match, and the loser
   sees rowcount 0 and moves on. So a slot is never checked twice.
2. Queues a "check" job for that slot. Its dedupe_key is the monitor and the
   slot, a second guard against doing the same slot twice.

Three decisions worth knowing:

- NO DRIFT. The next check is the next slot on the monitor's own timetable,
  not "now + 1 minute". A check that started 4 seconds late doesn't push every
  later check 4 seconds later (after a day of that, "every minute" would
  quietly have become every 61 seconds).
- SPREAD. Every monitor's timetable is shifted by its own fixed offset
  (offset_of), so 25 monitors that check every minute don't all fire at :00
  and queue up behind each other. The offset comes from the id, so it never
  changes between restarts.
- GAPS ARE NOT DOWNTIME. If the site slept (Render's free plan) or the crew
  was stopped, the slots it missed are skipped, not checked in a burst, and
  recorded as a Gap. Nobody looked during a gap, so uptime leaves it out
  instead of guessing "up" or "down".
"""
import math
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import select, update
from sqlalchemy.orm.exc import StaleDataError

from . import checking, queue
from .models import Gap, Monitor

EPOCH = datetime(2026, 1, 1)  # every timetable is counted from here
PER_ROUND = 200  # the most monitors one round starts (the rest are picked up a moment later)
DIRECT_AHEAD = timedelta(seconds=59)  # how far one replay round looks ahead
# Hooks run after a scheduled check is recorded: function(s, monitor, check, result, now).
# incidents.py (stage 3) decides Steady / Irregular / Flatline here.
after_check = []


def offset_of(monitor_id, interval_s):
    """A fixed shift, in whole seconds, for this monitor's timetable (spreads monitors out)."""
    return (monitor_id * 7919) % interval_s  # 7919 is a prime: neighbouring ids land far apart


def slot_after(monitor_id, interval_s, moment):
    """The first slot of the monitor's timetable strictly after `moment`."""
    offset = offset_of(monitor_id, interval_s)
    seconds = (moment - EPOCH).total_seconds() - offset
    k = math.floor(seconds / interval_s) + 1
    return EPOCH + timedelta(seconds=offset + k * interval_s)


def plan(monitor_id, interval_s, planned, now):
    """(slot to check now, the next slot, how many slots were skipped).

    planned is the slot that fell due. If it is more than one whole gap behind
    (the site slept), the slots in between are skipped: we check the latest due
    slot only."""
    behind = (now - planned).total_seconds()
    skipped = int(behind // interval_s) if behind >= interval_s else 0
    this_slot = planned + timedelta(seconds=skipped * interval_s)
    return this_slot, slot_after(monitor_id, interval_s, this_slot), skipped


def schedule(monitor, now, check_now=False):
    """Plan a monitor's next check after it is added, changed, paused or resumed.
    check_now: a new or resumed monitor is checked straight away."""
    if monitor.paused:
        monitor.next_check_at = None
    elif monitor.kind == "heartbeat":  # nothing to call: due when its ping is late (heartbeat.py)
        last = monitor.last_ping_at if monitor.last_ping_at and not check_now else now
        monitor.next_check_at = last + timedelta(seconds=monitor.interval_s + monitor.grace_s)
    elif check_now:
        monitor.next_check_at = now.replace(microsecond=0)
    else:
        monitor.next_check_at = slot_after(monitor.id, monitor.interval_s, now)


def run_due(s, now, workspace_id=None, direct=False):
    """Claim every due monitor and queue its check. Returns how many were started.

    direct: run each check straight away instead of queuing a job (the demo's replay:
    no network, and hours of checks must take seconds). Everything else is the same."""
    horizon = now
    if direct:
        # A replay takes every monitor due in the next minute in one round (each checked at its
        # own slot's time), but never past the next queued job: a re-check due at 9:00:20 must
        # still be judged before the 9:01 check. Fewer rounds, the same story.
        queued = queue.next_due(s, workspace_id)
        horizon = min(now + DIRECT_AHEAD, max(now, queued)) if queued else now + DIRECT_AHEAD
    rows = s.execute(
        select(Monitor.id, Monitor.next_check_at, Monitor.interval_s, Monitor.workspace_id)
        .where(Monitor.paused.is_(False), Monitor.next_check_at <= horizon,
               queue.in_scope(Monitor.workspace_id, workspace_id))
        .order_by(Monitor.next_check_at).limit(PER_ROUND)).all()
    started = 0
    for monitor_id, planned, interval_s, owner_id in rows:
        moment = max(now, planned) if direct else now
        this_slot, following, skipped = plan(monitor_id, interval_s, planned, moment)
        claimed = s.execute(update(Monitor).where(Monitor.id == monitor_id, Monitor.next_check_at == planned)
                            .values(next_check_at=following).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            continue  # another scheduler took this slot a moment ago
        if skipped:
            s.add(Gap(monitor_id=monitor_id, start=planned, end=this_slot, slots=skipped))
        if direct:
            check_slot(s, monitor_id, this_slot, moment)  # it commits the claim with its first step
        else:
            queue.enqueue(s, "check", {"monitor_id": monitor_id, "scheduled_at": this_slot.isoformat()},
                          run_at=now, workspace_id=owner_id, priority=3, max_attempts=2,
                          dedupe_key=f"check:{monitor_id}:{this_slot.isoformat()}")
        started += 1
    s.commit()
    return started


def next_due(s, workspace_id=None):
    """When the next monitor falls due (None if none is scheduled)."""
    return s.scalar(select(Monitor.next_check_at).where(
        Monitor.paused.is_(False), Monitor.next_check_at.is_not(None),
        queue.in_scope(Monitor.workspace_id, workspace_id)).order_by(Monitor.next_check_at).limit(1))


@queue.handler("check")
def check_job(s, job, now):
    """Check one monitor for one slot, and record it."""
    check_slot(s, job.payload["monitor_id"], datetime.fromisoformat(job.payload["scheduled_at"]), now)


def check_slot(s, monitor_id, scheduled_at, now):
    """Check one monitor for one slot, record it, and judge it (incidents.py)."""
    monitor = s.get(Monitor, monitor_id)
    if monitor is None or monitor.paused:
        return  # deleted or paused since it was queued: nothing to do
    # End the read before calling the address: no database transaction stays open (holding
    # a connection, and on SQLite maybe a lock) for the up to 30 seconds a check may wait.
    s.commit()
    result = checking.probe_monitor(monitor, scheduled_at, current_app.config)
    for _try in range(3):
        try:
            check = checking.record(s, monitor, result, scheduled_at, now)
            for hook in after_check:
                hook(s, monitor, check, result, now)
            s.flush()  # here, not at the job's commit, so a stale monitor is caught below
            return
        except StaleDataError:
            # Another worker (or a page) changed this monitor while we checked it. Read it
            # again and judge again: judged_until makes sure nothing is counted twice.
            s.rollback()
            monitor = s.get(Monitor, monitor_id)
            if monitor is None or monitor.paused:
                return
    raise queue.Retry("The monitor kept changing while this check was being saved.")
