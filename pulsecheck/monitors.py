"""Monitors: what to check, how often, and what a good answer is.

The pages (views/monitors.py) read a form with read_form(), which says what is
wrong in plain words, and save it with save(). Nothing here makes a check;
checking.py does.
"""
from flask import abort, g
from sqlalchemy import func, select

from . import probe, targets
from .models import Monitor

NAME_MAX_LENGTH = 80
# How often a monitor is checked, in seconds, with the words for each.
INTERVALS = [(60, "Every minute"), (120, "Every 2 minutes"), (300, "Every 5 minutes"),
             (600, "Every 10 minutes"), (900, "Every 15 minutes"), (1800, "Every 30 minutes"),
             (3600, "Every hour")]
TIMEOUTS = [(5, "5 seconds"), (10, "10 seconds"), (20, "20 seconds"), (30, "30 seconds")]
# A workspace's monitors are checked every minute all day; the free database has room for this many.
MAX_MONITORS = 25


def interval_words(seconds):
    return dict(INTERVALS).get(seconds, f"Every {seconds} s")


def get(s, monitor_id):
    """A monitor of this request's workspace, or 404 (never someone else's)."""
    monitor = s.get(Monitor, monitor_id)
    if monitor is None or monitor.workspace_id != g.workspace.id:
        abort(404)
    return monitor


def all_of(s, workspace_id):
    return s.scalars(select(Monitor).where(Monitor.workspace_id == workspace_id)
                     .order_by(func.lower(Monitor.name), Monitor.id)).all()


def count(s, workspace_id):
    return s.scalar(select(func.count()).select_from(Monitor).where(Monitor.workspace_id == workspace_id))


def defaults():
    return {"name": "", "url": "", "method": "GET", "expected_statuses": "200-299", "timeout_s": 10,
            "interval_s": 60}


def values_of(monitor):
    return {"name": monitor.name, "url": monitor.url, "method": monitor.method,
            "expected_statuses": monitor.expected_statuses, "timeout_s": monitor.timeout_s,
            "interval_s": monitor.interval_s}


def _choice(form, name, choices, default):
    try:
        value = int(form.get(name, default))
    except (TypeError, ValueError):
        return None
    return value if value in dict(choices) else None


def read_form(form, allow_private=False):
    """(values, errors) from a monitor form. errors is empty when it can be saved."""
    values = {
        "name": " ".join(form.get("name", "").split()),
        "url": targets.clean_url(form.get("url", "")),
        "method": form.get("method", "GET").upper(),
        "expected_statuses": " ".join(form.get("expected_statuses", "").split()) or "200-299",
        "timeout_s": _choice(form, "timeout_s", TIMEOUTS, 10),
        "interval_s": _choice(form, "interval_s", INTERVALS, 60),
    }
    errors = {}
    if not values["name"]:
        errors["name"] = "Give the monitor a name, like Payments API."
    elif len(values["name"]) > NAME_MAX_LENGTH:
        errors["name"] = f"Use at most {NAME_MAX_LENGTH} characters."
    if problem := targets.url_problem(values["url"], allow_private):
        errors["url"] = problem
    if values["method"] not in probe.METHODS:
        errors["method"] = "Choose GET, HEAD or POST."
    if problem := probe.statuses_problem(values["expected_statuses"]):
        errors["expected_statuses"] = problem
    if values["timeout_s"] is None:
        errors["timeout_s"] = "Choose a time from the list."
    if values["interval_s"] is None:
        errors["interval_s"] = "Choose how often from the list."
    if not errors and values["timeout_s"] >= values["interval_s"]:
        errors["timeout_s"] = "The wait must be shorter than the time between checks."
    return values, errors


def save(s, workspace, values, monitor=None):
    """Make a new monitor or change one (the caller commits). Returns it."""
    if monitor is None:
        monitor = Monitor(workspace_id=workspace.id, kind="http")
        s.add(monitor)
    for key, value in values.items():
        setattr(monitor, key, value)
    s.flush()
    return monitor
