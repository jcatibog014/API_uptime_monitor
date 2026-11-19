"""Maintenance windows: planned work is not an outage.

When you upgrade a database at 2 AM, the API will fail its checks, and nobody
should be woken up for it, nor should it count against uptime. So you plan a
window (its title, from, to, and which monitors) and, during it, those
monitors REST:

- they are still checked (so you can watch them come back), but each check is
  marked resting: no Irregular, no Flatline, no incident, no alert;
- resting checks aren't counted in uptime (metrics.py counts them apart);
- the monitor shows "Resting", and status pages show the window, also before
  it starts ("Planned maintenance").

When the window ends, the next check is judged as usual. An incident that was
already open stays open: if the service is still down after the work, it is
really down.
"""
from datetime import datetime

from sqlalchemy import select

from .models import Maintenance, MaintenanceMonitor

TITLE_MAX = 120
NOTE_MAX = 500


def active(s, monitor_id, moment):
    """The window this monitor is resting in at `moment`, or None."""
    return s.scalar(select(Maintenance).join(MaintenanceMonitor, MaintenanceMonitor.maintenance_id == Maintenance.id)
                    .where(MaintenanceMonitor.monitor_id == monitor_id, Maintenance.starts_at <= moment,
                           Maintenance.ends_at > moment).limit(1))


def of_workspace(s, workspace_id, now, upcoming=True):
    """Windows that haven't ended (upcoming=True), or that have, newest first."""
    query = select(Maintenance).where(Maintenance.workspace_id == workspace_id)
    if upcoming:
        return s.scalars(query.where(Maintenance.ends_at > now).order_by(Maintenance.starts_at)).all()
    return s.scalars(query.where(Maintenance.ends_at <= now).order_by(Maintenance.starts_at.desc()).limit(20)).all()


def read_form(form, zone_name, monitor_ids):
    """(values, errors): title, note, starts_at, ends_at (naive UTC) and the chosen monitor ids."""
    from .dates import from_zone
    errors = {}
    title = " ".join(form.get("title", "").split())
    note = form.get("note", "").strip()
    chosen = [int(value) for value in form.getlist("monitor") if value.isdigit() and int(value) in monitor_ids]
    moments = {}
    for name in ("starts_at", "ends_at"):
        try:
            moments[name] = from_zone(datetime.fromisoformat(form.get(name, "")), zone_name)
        except ValueError:
            errors[name] = "Choose a day and a time."
    if not title:
        errors["title"] = "Say what the work is, like Database upgrade."
    elif len(title) > TITLE_MAX:
        errors["title"] = f"Use at most {TITLE_MAX} characters."
    if len(note) > NOTE_MAX:
        errors["note"] = f"Use at most {NOTE_MAX} characters."
    if not chosen:
        errors["monitor"] = "Choose the monitors that rest during the work."
    if not errors and moments["ends_at"] <= moments["starts_at"]:
        errors["ends_at"] = "The end must be after the start."
    return {"title": title, "note": note, "monitors": chosen, **moments}, errors


def judge_hook(s, monitor, check, now):
    """Called by incidents.judge before anything else: True if the check is resting (stop there)."""
    if active(s, monitor.id, check.scheduled_at) is None:
        return False
    check.resting = True
    from .incidents import set_state
    set_state(s, monitor, "resting", check.scheduled_at)
    return True


def init_app(app):
    from . import incidents
    if judge_hook not in incidents.before_judge:
        incidents.before_judge.append(judge_hook)
