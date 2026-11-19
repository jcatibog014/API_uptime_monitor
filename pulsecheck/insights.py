"""Insights: is each service as reliable as it should be, and is it getting better or worse?

UPTIME GOAL (an SLO, "service level objective"). Each monitor has a goal,
99.9% at first. A goal is also a promise about failure: 99.9% allows 0.1% of
the time to go wrong, which is 43 minutes in 30 days. That allowance is the
ERROR BUDGET. Failed checks spend it, measured the same way as uptime: if
the last 30 days were 99.95% up, 0.05% of 30 days (22 minutes) is spent. So
uptime and budget always agree. A budget that is almost spent says "stop
shipping risky changes and fix reliability"; a full one says "you can take
some risks".

BURN RATE. How fast the budget is being spent right now: the share of the last
24 hours' checks that failed, divided by the share the goal allows. 1 means
"exactly on budget"; 10 means "this pace spends 30 days' budget in 3".

COMPARE. Every number is shown beside the same number for the period before
(the 7 days before the last 7, and so on), so a slow slide is visible.

THE WEEKLY REPORT. If the workspace asks for it (Settings), a timer sends a
short report to every channel on Mondays at 8 AM in the workspace's time zone:
last week's uptime and incidents per monitor. last_report_at makes sure each
week's report goes out once.
"""
import csv
import io
from dataclasses import dataclass
from datetime import time, timedelta

from sqlalchemy import select

from . import alerts, metrics, queue, rollups, timers
from .dates import at_local, to_zone
from .formatting import duration, ms, nice_date, uptime
from .models import Incident, Monitor, Workspace

PERIODS = {"7d": ("7 Days", 7), "30d": ("30 Days", 30), "90d": ("90 Days", 90)}
GOALS = [(99.0, "99%"), (99.5, "99.5%"), (99.9, "99.9%"), (99.95, "99.95%"), (99.99, "99.99%")]
BUDGET_DAYS = 30
REPORT_DAY, REPORT_TIME = 0, time(8, 0)  # Monday, 8 AM


def goal_words(goal):
    return dict(GOALS).get(goal, f"{goal:g}%")


@dataclass
class Budget:
    allowed_s: float
    used_s: float

    @property
    def left_s(self):
        return max(0.0, self.allowed_s - self.used_s)

    @property
    def used_share(self):
        return min(1.0, self.used_s / self.allowed_s) if self.allowed_s else 1.0

    @property
    def level(self):
        """good / warning / critical, for the meter's colour (always with its words beside it): green
        with more than half left, amber with half or less, red when all of it is spent."""
        if self.used_s >= self.allowed_s:
            return "critical"
        return "warning" if self.used_share >= 0.5 else "good"


@dataclass
class Row:
    monitor: Monitor
    now: metrics.Stats
    before: metrics.Stats
    incidents: int
    incidents_before: int
    down_s: float
    down_before_s: float
    budget: Budget
    burn: float | None


def downtime(s, monitor_ids, start, end):
    """{monitor_id: (incidents that touch [start, end), seconds of them inside it)}."""
    found = {monitor_id: (0, 0.0) for monitor_id in monitor_ids}
    if not monitor_ids:
        return found
    for incident in s.scalars(select(Incident).where(
            Incident.monitor_id.in_(monitor_ids), Incident.opened_at < end,
            (Incident.resolved_at.is_(None)) | (Incident.resolved_at > start))):
        count, seconds = found[incident.monitor_id]
        inside = (min(incident.resolved_at or end, end) - max(incident.opened_at, start)).total_seconds()
        found[incident.monitor_id] = (count + 1, seconds + max(0.0, inside))
    return found


def spent(stats):
    """The failed share of the last 30 days' counted checks, as seconds of 30 days."""
    return 0.0 if stats.uptime is None else (1 - stats.uptime) * BUDGET_DAYS * 86400


def burn_rate(stats, goal):
    """How many times faster than the goal allows the last 24 hours failed (None: nothing counted)."""
    if stats.uptime is None:
        return None
    allowed = 1 - goal / 100
    return (1 - stats.uptime) / allowed if allowed else None


def rows(s, workspace, period, now):
    """One Row per monitor (not paused, not new), for `period`, with the period before."""
    days = PERIODS[period][1]
    length = timedelta(days=days)
    monitors = s.scalars(select(Monitor).where(Monitor.workspace_id == workspace.id)
                         .order_by(Monitor.name)).all()
    ids = [monitor.id for monitor in monitors]
    done = rollups.done_until(s, now)
    zone_name = workspace.time_zone
    current = metrics.window(s, ids, now - length, now, done, zone_name)
    before = metrics.window(s, ids, now - 2 * length, now - length, done, zone_name)
    down_now = downtime(s, ids, now - length, now)
    down_before = downtime(s, ids, now - 2 * length, now - length)
    month = current if days == BUDGET_DAYS else metrics.window(s, ids, now - timedelta(days=BUDGET_DAYS), now, done,
                                                               zone_name)
    last_day = metrics.short_window(s, ids, now - timedelta(hours=24), now, keep_values=False)
    found = []
    for monitor in monitors:
        allowed = (1 - monitor.slo_target / 100) * BUDGET_DAYS * 86400
        found.append(Row(monitor, current[monitor.id], before[monitor.id], down_now[monitor.id][0],
                         down_before[monitor.id][0], down_now[monitor.id][1], down_before[monitor.id][1],
                         Budget(allowed, spent(month[monitor.id])),
                         burn_rate(last_day[monitor.id], monitor.slo_target)))
    return found


def totals(found):
    """(now, before): the whole workspace's numbers, every monitor added up."""
    now, before = metrics.Stats(), metrics.Stats()
    for row in found:
        now.add(row.now)
        before.add(row.before)
    return now, before


def change(now, before):
    """Percentage points of uptime, better (+) or worse (-), or None."""
    if now.uptime is None or before.uptime is None:
        return None
    return round(100 * (now.uptime - before.uptime), 2)


def as_csv(found, period):
    """The table as CSV (UTF-8, with a BOM so Excel reads it right)."""
    out = io.StringIO()
    writer = csv.writer(out)
    label = PERIODS[period][0].lower()
    writer.writerow(["Monitor", f"Uptime, last {label}", "Uptime, before", "Typical (p50) ms", "Slowest 5% (p95) ms",
                     "Incidents", "Downtime minutes", "Uptime goal", "Error budget left minutes", "Burn rate"])
    for row in found:
        p50, p95 = row.now.percentile(50), row.now.percentile(95)
        writer.writerow([row.monitor.name, uptime(row.now.ok, row.now.counted), uptime(row.before.ok, row.before.counted),
                         round(p50) if p50 else "", round(p95) if p95 else "", row.incidents,
                         round(row.down_s / 60, 1), goal_words(row.monitor.slo_target),
                         round(row.budget.left_s / 60, 1), "" if row.burn is None else round(row.burn, 2)])
    return "﻿" + out.getvalue()


# ---------- The weekly report ----------

def report_due(workspace, now):
    """The latest Monday 8 AM (the workspace's time) at or before `now`, if its report hasn't
    been sent yet; else None."""
    local = to_zone(now, workspace.time_zone)
    monday = local.date() - timedelta(days=(local.weekday() - REPORT_DAY) % 7)
    due = at_local(monday, REPORT_TIME, workspace.time_zone)
    if due > now:  # Monday before 8 AM: the latest report due is last Monday's
        due = at_local(monday - timedelta(days=7), REPORT_TIME, workspace.time_zone)
    if workspace.last_report_at is not None and workspace.last_report_at >= due:
        return None
    return due


def report(s, workspace, due):
    """(title, body) of the report for the 7 days before `due`."""
    start = due - timedelta(days=7)
    found_ids = s.scalars(select(Monitor.id).where(Monitor.workspace_id == workspace.id).order_by(Monitor.name)).all()
    names = dict(s.execute(select(Monitor.id, Monitor.name).where(Monitor.id.in_(found_ids))).all()) if found_ids else {}
    stats = metrics.window(s, found_ids, start, due, rollups.done_until(s, due), workspace.time_zone)
    down = downtime(s, found_ids, start, due)
    total = metrics.Stats()
    lines = []
    for monitor_id in found_ids:
        total.add(stats[monitor_id])
        count, seconds = down[monitor_id]
        p95 = stats[monitor_id].percentile(95)
        lines.append(f"{names[monitor_id]}: {uptime(stats[monitor_id].ok, stats[monitor_id].counted)} up"
                     + (f", slowest 5% {ms(p95)}" if p95 else "")
                     + (f", {count} incident{'s' if count != 1 else ''} ({duration(seconds)})" if count else ""))
    title = f"Pulse Check weekly report: {uptime(total.ok, total.counted)} up"
    body = (f"The week of {nice_date(start, workspace.time_zone)} to {nice_date(due - timedelta(seconds=1), workspace.time_zone)}.\n"
            + "\n".join(lines))
    return title, body


def send_report(s, workspace, due, now):
    title, body = report(s, workspace, due)
    workspace.last_report_at = due
    return alerts.raise_workspace_alert(s, workspace, "report", now, title, body)


@queue.handler("weekly_report")
def report_job(s, job, now):
    for workspace in s.scalars(select(Workspace).where(Workspace.weekly_report.is_(True),
                                                       Workspace.replaying.is_(False))):
        due = report_due(workspace, now)
        if due is not None:
            send_report(s, workspace, due, now)
    s.commit()


def every_round(s, now, workspace_id):
    if workspace_id is None and timers.claim(s, "weekly_report", now, 3600):
        queue.enqueue(s, "weekly_report", run_at=now, priority=6, max_attempts=3,
                      dedupe_key=f"weekly_report:{now.isoformat(timespec='hours')}")
    s.commit()


def demo_report(s, workspace, now):
    """seed.after_replay: the demo has the weekly report on, and last Monday's report in its inbox."""
    workspace.weekly_report = True
    local = to_zone(now, workspace.time_zone)
    monday = local.date() - timedelta(days=local.weekday())
    due = at_local(monday, REPORT_TIME, workspace.time_zone)
    if due > now:
        due -= timedelta(days=7)
    send_report(s, workspace, due, now)
    s.commit()


def init_app(app):
    from . import seed, worker
    if every_round not in worker.every_round:
        worker.every_round.append(every_round)
    if demo_report not in seed.after_replay:
        seed.after_replay.append(demo_report)
