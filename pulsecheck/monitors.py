"""Monitors: what to check, how often, and what a good answer is.

The pages (views/monitors.py) read a form with read_form(), which says what is
wrong in plain words, and save it with save(). Nothing here makes a check;
checking.py does.
"""
from flask import abort, g
from sqlalchemy import func, select

from . import heartbeat, insights, practice, probe, rules, targets
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
    """Every minute, Every 6 hours, Every day: never "Every 86400 s"."""
    if seconds in dict(INTERVALS):
        return dict(INTERVALS)[seconds]
    for size, one, many in ((86400, "day", "days"), (3600, "hour", "hours"), (60, "minute", "minutes")):
        if seconds % size == 0:
            count = seconds // size
            return f"Every {one}" if count == 1 else f"Every {count} {many}"
    return f"Every {seconds} seconds"


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


KINDS = {"http": "Web Address", "tcp": "TCP Port", "heartbeat": "Heartbeat"}
SLOW_LIMITS = [("", "No limit"), (300, "300 ms"), (500, "500 ms"), (1000, "1 second"), (2000, "2 seconds"),
               (5000, "5 seconds")]
TEXT_MAX = 200  # a rule's words


def defaults(kind="http"):
    return {"kind": kind, "name": "", "url": "", "host": "", "port": "", "service": "", "method": "GET",
            "expected_statuses": "200-299", "timeout_s": 10, "interval_s": 3600 if kind == "heartbeat" else 60,
            "grace_s": 300, "slow_ms": "", "sleepy": False, "slo_target": 99.9, **{key: "" for key in rules.RULE_KEYS}}


def values_of(monitor):
    host, port = rules.tcp_address(monitor.url) if monitor.kind == "tcp" else ("", "")
    return {"kind": monitor.kind, "name": monitor.name, "url": monitor.url, "host": host or "", "port": port or "",
            "service": practice.parse(monitor.url)[0], "method": monitor.method,
            "expected_statuses": monitor.expected_statuses, "timeout_s": monitor.timeout_s,
            "interval_s": monitor.interval_s, "grace_s": monitor.grace_s, "slow_ms": monitor.slow_ms or "",
            "sleepy": monitor.sleepy, "slo_target": monitor.slo_target,
            **{key: (monitor.assertions or {}).get(key, "") for key in rules.RULE_KEYS}}


def _choice(form, name, choices, default):
    try:
        value = int(form.get(name, default))
    except (TypeError, ValueError):
        return None
    return value if value in dict(choices) else None


def read_form(form, allow_private=False, demo=False, kind=None):
    """(values, errors) from a monitor form. errors is empty when it can be saved.
    values holds only columns of Monitor, plus a few keys starting with "_" that only
    re-show the form. demo: a demo's monitor checks one of the practice services."""
    kind = "practice" if demo else (kind or form.get("kind", "http"))
    if kind not in KINDS and kind != "practice":
        kind = "http"
    errors = {}
    values = {"kind": kind, "name": " ".join(form.get("name", "").split())}
    try:
        values["slo_target"] = float(form.get("slo_target", "99.9"))
    except ValueError:
        values["slo_target"] = None
    if values["slo_target"] not in dict(insights.GOALS):
        errors["slo_target"] = "Choose a goal from the list."
        values["slo_target"] = 99.9
    shown = {"service": form.get("service", ""), "host": form.get("host", "").strip(), "port": form.get("port", "").strip()}
    if not values["name"]:
        errors["name"] = "Give the monitor a name, like Payments API."
    elif len(values["name"]) > NAME_MAX_LENGTH:
        errors["name"] = f"Use at most {NAME_MAX_LENGTH} characters."

    if kind == "heartbeat":
        values["interval_s"] = _choice(form, "interval_s", heartbeat.EVERY, 3600)
        values["grace_s"] = _choice(form, "grace_s", heartbeat.GRACE, 300)
        values["url"], values["confirm_tries"], values["recover_tries"] = "", 1, 1
        if values["interval_s"] is None:
            errors["interval_s"] = "Choose how often from the list."
        if values["grace_s"] is None:
            errors["grace_s"] = "Choose a time from the list."
        return {**values, **{f"_{key}": value for key, value in shown.items()}}, errors

    values["interval_s"] = _choice(form, "interval_s", INTERVALS, 60)
    values["timeout_s"] = _choice(form, "timeout_s", TIMEOUTS, 10)
    values["slow_ms"] = _choice(form, "slow_ms", SLOW_LIMITS[1:], None) if form.get("slow_ms") else None
    if form.get("slow_ms") and values["slow_ms"] is None:
        errors["slow_ms"] = "Choose a limit from the list."
    if kind == "practice":
        values["url"] = practice.address(shown["service"])
        if shown["service"] not in practice.SERVICES:
            errors["service"] = "Choose a practice service from the list."
    elif kind == "tcp":
        host, port = shown["host"], shown["port"]
        if not host:
            errors["host"] = "Enter the server's name or address."
        elif not port.isdigit() or not 1 <= int(port) <= 65535:
            errors["port"] = "Enter a port from 1 to 65535."
        else:
            values["url"] = f"tcp://{host}:{int(port)}"
            if problem := targets.url_problem(f"http://{host}:{int(port)}/", allow_private):
                errors["host"] = problem
        values["sleepy"] = False
    else:
        values["url"] = targets.clean_url(form.get("url", ""))
        values["method"] = form.get("method", "GET").upper()
        values["expected_statuses"] = " ".join(form.get("expected_statuses", "").split()) or "200-299"
        values["sleepy"] = form.get("sleepy") == "1"
        values["assertions"] = {key: form.get(key, "").strip()[:TEXT_MAX] for key in rules.RULE_KEYS
                                if form.get(key, "").strip()}
        if values["assertions"].get("json_value") and not values["assertions"].get("json_path"):
            errors["json_path"] = "Say where the value is, like data.status."
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
    wait = rules.SLEEPY_TIMEOUT if values.get("sleepy") else values["timeout_s"]
    if not errors and wait >= values["interval_s"]:
        if values.get("sleepy"):
            errors["interval_s"] = "A sleepy site may need 90 seconds to wake: check it every 2 minutes or less often."
        else:
            errors["timeout_s"] = "The wait must be shorter than the time between checks."
    return {**values, **{f"_{key}": value for key, value in shown.items()}}, errors


def save(s, workspace, values, monitor=None):
    """Make a new monitor or change one (the caller commits). Returns it."""
    if monitor is None:
        monitor = Monitor(workspace_id=workspace.id, kind=values["kind"])
        s.add(monitor)
        if values["kind"] == "heartbeat":
            monitor.heartbeat_token = heartbeat.new_token()
    same_service = (practice.is_practice(values.get("url", "")) and monitor.url
                    and practice.parse(monitor.url)[0] == practice.parse(values["url"])[0])
    for key, value in values.items():
        if key.startswith("_") or key == "kind" or (key == "url" and same_service):
            continue  # the same practice service: keep its script's settings (like the demo's outage time)
        setattr(monitor, key, value)
    s.flush()
    return monitor


def form_values(values):
    """The values as the form shows them again (after a mistake)."""
    shown = {key: value for key, value in values.items() if not key.startswith("_")}
    shown.update({key[1:]: value for key, value in values.items() if key.startswith("_")})
    for key in rules.RULE_KEYS:
        shown[key] = (values.get("assertions") or {}).get(key, "")
    shown.setdefault("slow_ms", "")
    shown["slow_ms"] = shown["slow_ms"] or ""
    return {**defaults(values["kind"]), **shown}
