"""The words Pulse Check uses, and what each one means in plain English.

The app reads like a bedside heart monitor: a healthy service has a Steady
pulse, a slow one a Weak Pulse, a failing one is Irregular while it is being
checked again, and a confirmed failure is a Flatline. Every word comes with a
plain note, shown when you point at it (and to screen readers), so nobody has
to guess what "Irregular" means. Change a word here and it changes everywhere.

Themed words name STATES. Buttons and actions use plain words ("Check now",
"Pause checks"), Right On Cue's lesson.

tone picks the stamp's colour in style.css (.pulse-<tone>).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    label: str
    note: str
    tone: str


# A monitor's state (incidents.py moves it; stage 3).
STATE = {
    "new": Word("Waiting", "Not checked yet. The first check is on its way.", "quiet"),
    "steady": Word("Steady", "Up: the last checks answered as expected.", "steady"),
    "weak": Word("Weak Pulse", "Up, but slower than the limit you set.", "weak"),
    "irregular": Word("Irregular", "A check just failed. Pulse Check is checking again before it calls it down.",
                      "weak"),
    "flatline": Word("Flatline", "Down: several checks in a row failed. An incident is open.", "flat"),
    "paused": Word("Paused", "Not being checked. Nothing is counted while it is paused.", "quiet"),
    "resting": Word("Resting", "In a maintenance window: checked, but no alerts and not counted in uptime.",
                    "rest"),
}

# One check's result.
CHECK = {
    "beat": Word("Beat", "The check got the answer it expected.", "steady"),
    "slow": Word("Slow Beat", "The answer was right, but slower than the limit.", "weak"),
    "missed": Word("Missed Beat", "The check failed (the reason is beside it).", "flat"),
    "our_side": Word("Our Side", "The check failed, but so did Pulse Check's own test, so the problem was "
                                 "probably our connection, not this service. Not counted.", "quiet"),
}

# An incident (incidents.py).
INCIDENT = {
    "open": Word("Ongoing", "Still down. It closes by itself after a few good checks in a row.", "flat"),
    "resolved": Word("Resolved", "It is back up.", "steady"),
}

# An alert on its way to a channel (alerts.py). Plain words: these are about delivery, not health.
ALERT = {
    "waiting": Word("Waiting", "In the queue; it goes out in a moment.", "quiet"),
    "retrying": Word("Retrying", "The service said no for now. It is tried again after a wait.", "weak"),
    "sent": Word("Sent", "The service took it.", "steady"),
    "failed": Word("Failed", "It can't be delivered: fix the channel (the reason is beside it).", "flat"),
    "skipped": Word("Skipped", "The channel was paused, so it wasn't sent.", "quiet"),
}

# A member of the crew (worker.py), on the Crew page.
WORKER = {
    "on": Word("On Duty", "Working: it wrote its heartbeat in the last 2 minutes.", "steady"),
    "quiet": Word("Gone Quiet", "No heartbeat for over 2 minutes. It may be stuck on a slow check, or stopped.",
                  "weak"),
    "off": Word("Off Duty", "Stopped on purpose.", "quiet"),
}

# Why a check failed, in plain words (probe.py sets error_kind).
ERRORS = {
    "dns": "Name not found",
    "blocked": "Private address",
    "refused": "Connection refused",
    "timeout": "No answer in time",
    "tls": "Secure connection failed",
    "connection": "Connection broke",
    "redirects": "Too many redirects",
    "http_status": "Wrong status code",
    "assertion": "Wrong content",
    "too_slow": "Too slow",
    "missing": "Heartbeat missing",
}


def state(name):
    return STATE.get(name, STATE["new"])


def monitor_state_name(monitor):
    return getattr(monitor, "state", None) or "new"


def incident_word(incident):
    return INCIDENT["open" if incident.resolved_at is None else "resolved"]


def check_word(check):
    """The word for one Check row."""
    if check.ok:
        return CHECK["slow" if getattr(check, "slow", False) else "beat"]
    return CHECK["our_side" if getattr(check, "our_side", False) else "missed"]


def error_label(kind):
    return ERRORS.get(kind or "", "Failed")


def init_app(app):
    app.jinja_env.globals.update(pulse_state=state, check_word=check_word, error_label=error_label,
                                 monitor_state_name=monitor_state_name, worker_word=WORKER.get,
                                 incident_word=incident_word)
