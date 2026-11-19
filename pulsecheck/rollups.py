"""Rollups: each monitor's numbers per hour and per day, worked out from its checks.

A monitor checked every minute makes 1,440 checks a day. Reading 129,600 of
them for a 90-day uptime would be slow, and keeping them all would fill the
free database. So every 5 minutes one worker (a timer, timers.py) queues a
"rollups" job that:

1. RECOMPUTES each closed hour since the last run (and the one before it, for a
   re-check that arrived late) from the raw checks: counts, good checks, the
   sum / min / max response time and a histogram (models.Rollup). It deletes
   that hour's rows and writes them again in one transaction. Recomputing
   instead of adding on is what makes it SAFE TO RUN TWICE: a job that runs
   again (at-least-once) writes the same numbers, never double ones.
2. RECOMPUTES each workspace's today and yesterday (its own time zone) from
   the hour rows. A day row is that day's hours added up.
3. Remembers how far the hours are done (the "rolled_until" timer): metrics.py
   reads rollups before it and raw checks after it.

Then old raw checks can go (after 14 days), hour rows after 90 days, and day
rows after 400 days (tick.py's tidying).
"""
from datetime import timedelta

from sqlalchemy import case, delete, func, select

from . import queue, timers
from .metrics import bucket_of, day_starts, floor_hour, local_midnight, rollup_stats
from .models import BUCKETS, Check, Gap, Monitor, Rollup, Timer, Workspace

EVERY = 300  # seconds
CATCH_UP = timedelta(hours=48)  # the most hours one run goes back (after a long sleep)
KEEP_CHECKS = timedelta(days=14)
KEEP_HOURS = timedelta(days=90)
KEEP_DAYS = timedelta(days=400)
KEEP_GAPS = timedelta(days=90)
DONE = "rolled_until"


def rolled_until(s):
    """Hours before this moment have rollups (None before the first run)."""
    return timers.next_at(s, DONE)


def done_until(s, now):
    """What metrics.py may trust as rolled up. Before the first run: nothing in the last two
    days (they are read from the raw checks instead)."""
    return rolled_until(s) or floor_hour(now) - timedelta(days=2)


def _monitor_ids(s, workspace_id):
    query = select(Monitor.id)
    if workspace_id is not None:
        query = query.where(Monitor.workspace_id == workspace_id)
    return s.scalars(query).all()


def recompute_hour(s, hour, workspace_id=None):
    """Write the hour rows of [hour, hour + 1 h) again from the raw checks. The caller commits."""
    end = hour + timedelta(hours=1)
    ids = _monitor_ids(s, workspace_id)
    if not ids:
        return 0
    good = Check.ok.is_(True)
    in_hour = (Check.monitor_id.in_(ids), Check.scheduled_at >= hour, Check.scheduled_at < end,
               Check.resting.is_(False))  # maintenance checks are only counted as resting (below)
    totals = s.execute(select(
        Check.monitor_id, func.count(),
        func.sum(case((good, 1), else_=0)),
        func.sum(case((good & Check.slow.is_(True), 1), else_=0)),
        func.sum(case((Check.our_side.is_(True), 1), else_=0)),
        func.sum(case((good, Check.total_ms), else_=0)),
        func.min(case((good, Check.total_ms))), func.max(case((good, Check.total_ms))),
    ).where(*in_hour).group_by(Check.monitor_id)).all()
    rows = {}
    for monitor_id, checks, ok, slow, our_side, sum_ms, min_ms, max_ms in totals:
        rows[monitor_id] = Rollup(monitor_id=monitor_id, period="hour", start=hour, checks=checks, ok=ok or 0,
                                  slow=slow or 0, our_side=our_side or 0, sum_ms=float(sum_ms or 0),
                                  min_ms=min_ms, max_ms=max_ms)
    bucket = bucket_of(Check.total_ms)
    for monitor_id, n, count in s.execute(select(Check.monitor_id, bucket, func.count()).where(
            *in_hour, good, Check.total_ms.is_not(None)).group_by(Check.monitor_id, bucket)).all():
        setattr(rows[monitor_id], f"b{n}", count)
    for monitor_id, resting in s.execute(select(Check.monitor_id, func.count()).where(
            Check.monitor_id.in_(ids), Check.scheduled_at >= hour, Check.scheduled_at < end,
            Check.resting.is_(True)).group_by(Check.monitor_id)).all():
        if monitor_id not in rows:
            rows[monitor_id] = Rollup(monitor_id=monitor_id, period="hour", start=hour, checks=0, ok=0, slow=0,
                                      our_side=0, sum_ms=0.0)
        rows[monitor_id].resting = resting
    for monitor_id, slots in s.execute(select(Gap.monitor_id, func.sum(Gap.slots)).where(
            Gap.monitor_id.in_(ids), Gap.start >= hour, Gap.start < end).group_by(Gap.monitor_id)).all():
        if monitor_id not in rows:
            rows[monitor_id] = Rollup(monitor_id=monitor_id, period="hour", start=hour)
        rows[monitor_id].missed = int(slots or 0)
    # Replace only the rows of monitors that have checks (or gaps) in this hour. A row with
    # nothing under it stays: it may be a demo's older past (history.py), which has no checks.
    if rows:
        s.execute(delete(Rollup).where(Rollup.period == "hour", Rollup.start == hour,
                                       Rollup.monitor_id.in_(list(rows))))
    s.add_all(rows.values())
    return len(rows)


def recompute_day(s, workspace_id, day, following):
    """Write one workspace's day rows for [day, following) again from its hour rows."""
    ids = _monitor_ids(s, workspace_id)
    if not ids:
        return 0
    written = 0
    for monitor_id, stats in rollup_stats(s, ids, "hour", day, following).items():
        if not stats.checks and not stats.missed and not stats.resting:
            continue  # nothing under it: keep any row it has (see recompute_hour)
        s.execute(delete(Rollup).where(Rollup.period == "day", Rollup.start == day, Rollup.monitor_id == monitor_id))
        row = Rollup(monitor_id=monitor_id, period="day", start=day, checks=stats.checks, ok=stats.ok,
                     slow=stats.slow, our_side=stats.our_side, resting=stats.resting, missed=stats.missed,
                     sum_ms=stats.sum_ms,
                     min_ms=stats.min_ms, max_ms=stats.max_ms)
        for n in range(BUCKETS):
            setattr(row, f"b{n}", stats.buckets[n])
        s.add(row)
        written += 1
    return written


def recent_days(s, workspace, now):
    """Recompute a workspace's yesterday and today."""
    for day in day_starts(workspace.time_zone, now - timedelta(days=1), now):
        recompute_day(s, workspace.id, day, local_midnight(day + timedelta(hours=26), workspace.time_zone))


def run(s, now, workspace_id=None):
    """Bring the rollups up to date (all workspaces, or one: a demo after its replay)."""
    done = rolled_until(s)
    newest = floor_hour(now)
    hour = max(newest - CATCH_UP, (done - timedelta(hours=1)) if done else newest - CATCH_UP)
    while hour < newest:
        recompute_hour(s, hour, workspace_id)
        hour += timedelta(hours=1)
    if workspace_id is None:
        s.merge(Timer(name=DONE, next_at=newest))
        workspaces = s.scalars(select(Workspace).where(Workspace.replaying.is_(False))).all()
    else:
        workspaces = [s.get(Workspace, workspace_id)]
    for workspace in workspaces:
        recent_days(s, workspace, now)
    s.commit()


@queue.handler("rollups")
def rollup_job(s, job, now):
    run(s, now)


def every_round(s, now, workspace_id):
    if workspace_id is not None:
        return  # a demo replay rolls up its own hours when it ends (seed.after_replay)
    if timers.claim(s, "rollups", now, EVERY):
        queue.enqueue(s, "rollups", run_at=now, priority=6, max_attempts=3,
                      dedupe_key=f"rollups:{now.isoformat(timespec='minutes')}")
    s.commit()


def wake_up(s, now, workspace_id):
    return None if workspace_id is not None else timers.next_at(s, "rollups")


def after_replay(s, workspace, now):
    """A demo's replayed hours, rolled up straight away: the live crew's rollups skipped the
    workspace while it was replaying, and may already be past those hours."""
    hour = floor_hour(now) - timedelta(hours=4)  # the replay is 3 hours, plus the hour it started in
    while hour < floor_hour(now):
        recompute_hour(s, hour, workspace.id)
        hour += timedelta(hours=1)
    recent_days(s, workspace, now)
    s.commit()


def tidy(s, moment):
    deleted = s.execute(delete(Check).where(Check.scheduled_at < moment - KEEP_CHECKS)).rowcount
    deleted += s.execute(delete(Rollup).where(Rollup.period == "hour", Rollup.start < moment - KEEP_HOURS)).rowcount
    deleted += s.execute(delete(Rollup).where(Rollup.period == "day", Rollup.start < moment - KEEP_DAYS)).rowcount
    deleted += s.execute(delete(Gap).where(Gap.start < moment - KEEP_GAPS)).rowcount
    return deleted


def init_app(app):
    from . import seed, tick, worker
    if every_round not in worker.every_round:
        worker.every_round.append(every_round)
        worker.wake_ups.append(wake_up)
    if after_replay not in seed.after_replay:
        seed.after_replay.append(after_replay)
    tick.tidy_jobs["old_checks_and_rollups_deleted"] = tidy
