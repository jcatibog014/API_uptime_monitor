"""The numbers on the monitor page (uptime, percentiles, the charts) and on the Dashboard tiles."""
from collections import defaultdict
from datetime import timedelta

from flask import g, request
from sqlalchemy import func, select

from .. import charts, metrics, rollups
from ..dates import utcnow
from ..models import Check, Incident, Monitor

SPARK_CHECKS = 30


def phase_averages(s, monitor_id, start, end):
    """The average time of each part of the good checks in [start, end)."""
    row = s.execute(select(func.avg(Check.dns_ms), func.avg(Check.connect_ms), func.avg(Check.tls_ms),
                           func.avg(Check.ttfb_ms), func.avg(Check.total_ms), func.count())
                    .where(Check.monitor_id == monitor_id, Check.ok.is_(True), Check.scheduled_at >= start,
                           Check.scheduled_at < end)).one()
    if not row[5]:
        return None
    return {"dns_ms": row[0], "connect_ms": row[1], "tls_ms": row[2], "ttfb_ms": row[3], "total_ms": row[4],
            "count": row[5]}


def downtime(s, monitor_ids, start, end, now):
    """(incidents that touch [start, end), seconds of them inside it)."""
    rows = s.scalars(select(Incident).where(Incident.monitor_id.in_(monitor_ids), Incident.opened_at < end,
                                            (Incident.resolved_at.is_(None)) | (Incident.resolved_at > start))).all()
    seconds = sum(((min(row.resolved_at or now, end) - max(row.opened_at, start)).total_seconds()) for row in rows)
    return len(rows), max(0.0, seconds)


def monitor_numbers(s, monitor):
    period = request.args.get("period", "24h")
    if period not in metrics.PERIODS:
        period = "24h"
    now = utcnow()
    done = rollups.done_until(s, now)
    zone_name = g.workspace.time_zone
    _label, length = metrics.PERIODS[period]
    stats = metrics.window(s, [monitor.id], now - length, now, done, zone_name)[monitor.id]
    points = metrics.series(s, monitor.id, period, now, done, zone_name)
    days = metrics.strip(s, [monitor.id], now, done, zone_name)[monitor.id]
    incident_count, down_seconds = downtime(s, [monitor.id], now - length, now, now)
    return {"period": period, "periods": metrics.PERIODS, "numbers": stats, "points": points,
            "chart": charts.response_chart(points, period), "strip": charts.strip(days),
            "strip_uptime": strip_uptime(days), "incident_count": incident_count, "down_seconds": down_seconds,
            "phase_average": phase_averages(s, monitor.id, now - timedelta(hours=24), now)}


def strip_uptime(days):
    total = metrics.Stats()
    for day in days:
        total.add(day.stats)
    return total


def dashboard_numbers(s, workspace):
    """Each tile's sparkline (its last 30 checks) and 24-hour uptime, and the overall uptime."""
    now = utcnow()
    ids = s.scalars(select(Monitor.id).where(Monitor.workspace_id == workspace.id)).all()
    recent = defaultdict(list)
    if ids:
        ranked = select(Check.monitor_id, Check.ok, Check.total_ms, Check.scheduled_at,
                        func.row_number().over(partition_by=Check.monitor_id,
                                               order_by=Check.scheduled_at.desc()).label("n")) \
            .where(Check.monitor_id.in_(ids), Check.scheduled_at >= now - timedelta(days=1)).subquery()
        for monitor_id, ok, total_ms, _at, _n in s.execute(select(ranked).where(ranked.c.n <= SPARK_CHECKS)
                                                           .order_by(ranked.c.scheduled_at)).all():
            recent[monitor_id].append(total_ms if ok else None)
    day = metrics.short_window(s, ids, now - timedelta(hours=24), now, keep_values=False)
    counts = {monitor_id: (stats.ok, stats.counted) for monitor_id, stats in day.items() if stats.counted}
    good = sum(value[0] for value in counts.values())
    counted = sum(value[1] for value in counts.values())
    return {"sparks": {monitor_id: charts.sparkline(values) for monitor_id, values in recent.items()},
            "uptime_24h": counts, "overall_uptime": (good, counted),
            "dashboard_stats": ["_dashboard_uptime.html"]}


def init_app(app):
    from . import dashboard, monitors
    if monitor_numbers not in monitors.page_extras:
        monitors.page_extras.append(monitor_numbers)
    if dashboard_numbers not in dashboard.panels:
        dashboard.panels.append(dashboard_numbers)
    app.jinja_env.globals["time_label"] = charts.time_label
