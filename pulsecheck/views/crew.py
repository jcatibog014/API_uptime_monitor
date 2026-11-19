"""The Crew page: who is working, how far behind the checks run, what is waiting,
and the jobs that gave up (dead letters), with Retry now and Discard."""
from datetime import timedelta
from statistics import median

from flask import Blueprint, current_app, flash, g, redirect, render_template, url_for
from sqlalchemy import func, select

from .. import canary, db, queue, worker
from ..auth import login_required, not_for_demo
from ..dates import utcnow
from ..models import Check, Gap, Job, Monitor, Worker

bp = Blueprint("crew", __name__)
DEAD_SHOWN = 50
GAPS_SHOWN = 20
KINDS = {"check": "Check", "deliver_alert": "Alert", "hourly_rollup": "Hourly numbers",
         "daily_rollup": "Daily numbers", "demo_history": "Demo history", "weekly_report": "Weekly report"}


def worker_name(row, number):
    """Plain names instead of "web-1234-ab12": "Web app worker 2", "Own window", "Timer"."""
    if row.kind == "in the web app":
        return f"Web app worker {number}"
    return {"own process": "Worker in its own window", "timer": "Timer", "rehearsal": "Demo replay"}.get(
        row.kind, row.kind.capitalize())


def worker_state(row, now):
    if row.stopped_at is not None:
        return "off"
    return "quiet" if now - row.last_seen > worker.QUIET_AFTER else "on"


def lag_numbers(s, workspace_id, now):
    """How late scheduled checks started in the last hour (seconds): (typical, worst, count)."""
    rows = s.execute(select(Check.scheduled_at, Check.started_at).join(Monitor, Monitor.id == Check.monitor_id)
                     .where(Monitor.workspace_id == workspace_id, Check.manual.is_(False),
                            Check.scheduled_at >= now - timedelta(hours=1))).all()
    lags = [max(0.0, (started - scheduled).total_seconds()) for scheduled, started in rows]
    if not lags:
        return None, None, 0
    return median(lags), max(lags), len(lags)


def get_dead_job(s, job_id):
    job = s.get(Job, job_id)
    if job is None or job.workspace_id != g.workspace.id or job.status != queue.DEAD:
        return None
    return job


@bp.get("/crew")
@login_required
def index():
    s = db.session()
    now = utcnow()
    rows = s.scalars(select(Worker).where(Worker.last_seen >= now - timedelta(hours=1), Worker.kind != "rehearsal")
                     .order_by(Worker.started_at, Worker.id)).all()
    numbered, web_number = [], 0
    for row in rows:
        if row.kind == "in the web app":
            web_number += 1
        numbered.append((row, worker_name(row, web_number), worker_state(row, now)))
    counts = dict(s.execute(select(Job.status, func.count()).where(Job.workspace_id == g.workspace.id)
                            .group_by(Job.status)).all())
    dead = s.scalars(select(Job).where(Job.workspace_id == g.workspace.id, Job.status == queue.DEAD)
                     .order_by(Job.finished_at.desc()).limit(DEAD_SHOWN)).all()
    gaps = s.execute(select(Gap, Monitor.name).join(Monitor, Monitor.id == Gap.monitor_id)
                     .where(Monitor.workspace_id == g.workspace.id).order_by(Gap.start.desc()).limit(GAPS_SHOWN)).all()
    typical, worst, checked = lag_numbers(s, g.workspace.id, now)
    on_duty = any(state == "on" for _row, _name, state in numbered)
    return render_template("crew.html", workers=numbered, counts=counts, dead=dead, gaps=gaps, kinds=KINDS,
                           canary=canary.latest(s), canary_on=bool(canary.targets(current_app.config)),
                           lag_typical=typical, lag_worst=worst, lag_count=checked, on_duty=on_duty,
                           can_start=current_app.config.get("RUN_WORKER") and not worker.in_app_running())


@bp.post("/crew/start")
@login_required
def start():
    """Start the web app's workers again (if they stopped, or the app started with RUN_WORKER off)."""
    not_for_demo("The demo can't start or stop the crew.")
    worker.start_in_app(current_app._get_current_object())
    flash("The crew is on duty.")
    return redirect(url_for("crew.index"))


@bp.post("/crew/jobs/<int:job_id>/retry")
@login_required
def retry(job_id):
    s = db.session()
    job = get_dead_job(s, job_id)
    if job is None:
        flash("That job isn't waiting for you any more.", "error")
        return redirect(url_for("crew.index"))
    queue.retry_now(s, job, utcnow())
    s.commit()
    worker.nudge()
    flash("The job is back in the queue.")
    return redirect(url_for("crew.index", _anchor="dead"))


@bp.post("/crew/jobs/<int:job_id>/discard")
@login_required
def discard(job_id):
    s = db.session()
    job = get_dead_job(s, job_id)
    if job is not None:
        queue.discard(s, job, utcnow())
        s.commit()
        flash("The job is discarded.")
    return redirect(url_for("crew.index", _anchor="dead"))


def init_app(app):
    app.jinja_env.globals["job_kind"] = lambda kind: KINDS.get(kind, kind.replace("_", " ").capitalize())
