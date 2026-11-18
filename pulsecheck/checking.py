"""Run one monitor's check and record it.

probe.py knows how to ask an address. This module knows about monitors:
which prober a kind of monitor uses, and how a result becomes a Check row.
The Check now button (stage 1) and the scheduled check jobs (stage 2) both
come through run_check(), so they behave exactly the same.
"""
from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import probe
from .models import Check

# kind -> function(monitor, app_config) -> probe.Result. Later stages add kinds.
PROBERS = {}


def http_prober(monitor, config):
    return probe.check(monitor.url, method=monitor.method, expected=monitor.expected_statuses,
                       timeout=monitor.timeout_s, allow_private=config["ALLOW_PRIVATE_TARGETS"])


PROBERS["http"] = http_prober


def probe_monitor(monitor, config=None):
    config = config if config is not None else current_app.config
    return PROBERS[monitor.kind](monitor, config)


def record(s, monitor, result, scheduled_at, started_at, manual=False):
    """Save a result as a Check (the caller commits). Returns the Check, or the one
    already saved for this moment: a job run twice records once."""
    existing = s.scalar(select(Check).where(Check.monitor_id == monitor.id, Check.scheduled_at == scheduled_at))
    if existing is not None:
        return existing
    check = Check(monitor_id=monitor.id, scheduled_at=scheduled_at, started_at=started_at, ok=result.ok,
                  manual=manual, error_kind=result.error_kind, message=(result.message or "")[:300],
                  status_code=result.status_code, remote_ip=result.remote_ip, dns_ms=result.dns_ms,
                  connect_ms=result.connect_ms, tls_ms=result.tls_ms, ttfb_ms=result.ttfb_ms,
                  total_ms=result.total_ms)
    try:
        # A savepoint: if another worker saved this moment a split second ago, the UNIQUE
        # rule refuses ours, and only this insert is undone (not the caller's other work).
        with s.begin_nested():
            s.add(check)
    except IntegrityError:
        return s.scalar(select(Check).where(Check.monitor_id == monitor.id, Check.scheduled_at == scheduled_at))
    return check


def run_check(s, monitor, scheduled_at, started_at, manual=False, config=None):
    """Check a monitor now and record it. Returns (check, result)."""
    result = probe_monitor(monitor, config)
    return record(s, monitor, result, scheduled_at, started_at, manual), result


def recent(s, monitor_id, limit=20):
    return s.scalars(select(Check).where(Check.monitor_id == monitor_id)
                     .order_by(Check.scheduled_at.desc()).limit(limit)).all()


def last(s, monitor_id):
    return s.scalar(select(Check).where(Check.monitor_id == monitor_id).order_by(Check.scheduled_at.desc()).limit(1))
