"""Warnings before an https certificate runs out.

An expired certificate takes a site down just as surely as a crash: browsers
and API clients refuse to talk to it. Certificates are renewed by a job that
can fail silently, so Pulse Check reads each one's end date during the TLS
step of every check (probe.read_certificate) and warns 14, 7, 3 and 1 day
before. Each warning goes out once (cert_warned_days remembers the last one);
a renewed certificate (more than 14 days left) starts the count again.
"""
from . import alerts
from .formatting import nice_date

WARN_DAYS = (14, 7, 3, 1)


def days_left(monitor, now):
    if monitor.cert_expires_at is None:
        return None
    return (monitor.cert_expires_at - now).total_seconds() / 86400


def note(s, monitor, result, now):
    """Keep the certificate's date, and warn when it crosses a threshold."""
    if result.cert_expires_at is None:
        return
    monitor.cert_expires_at, monitor.cert_issuer = result.cert_expires_at, result.cert_issuer or monitor.cert_issuer
    left = days_left(monitor, now)
    if left > WARN_DAYS[0]:
        monitor.cert_warned_days = None  # renewed: warn again next time
        return
    due = min(days for days in WARN_DAYS if left <= days)
    if monitor.cert_warned_days is not None and due >= monitor.cert_warned_days:
        return  # already warned at this point
    monitor.cert_warned_days = due
    whole = max(0, int(left))
    alerts.raise_monitor_alert(
        s, monitor, "cert", due, now,
        f"{monitor.name}'s certificate runs out in {whole} day{'s' if whole != 1 else ''}",
        f"Its https certificate ends on {nice_date(monitor.cert_expires_at, monitor.workspace.time_zone)}"
        f"{' (issued by ' + monitor.cert_issuer + ')' if monitor.cert_issuer else ''}. "
        "Renew it before then, or browsers and apps will refuse to connect.")


def after_check(s, monitor, check, result, now):
    note(s, monitor, result, now)


def init_app(app):
    from . import scheduler
    if after_check not in scheduler.after_check:
        scheduler.after_check.append(after_check)
