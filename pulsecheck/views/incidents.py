"""Incident pages: every confirmed failure, open ones first, and one incident's story."""
from datetime import timedelta

from flask import Blueprint, abort, g, render_template, request
from sqlalchemy import select

from .. import db, incidents
from ..auth import login_required
from ..models import Check, Incident

bp = Blueprint("incidents", __name__)
SHOWN = 300
CHECKS_AROUND = timedelta(minutes=3)  # the incident page also shows the checks just before and after
MAX_CHECKS = 200
# Extra parts of one incident's page that later stages add: function(s, incident) -> dict.
page_extras = []


def get(s, incident_id):
    incident = s.get(Incident, incident_id)
    if incident is None or incident.workspace_id != g.workspace.id:
        abort(404)
    return incident


@bp.get("/incidents")
@login_required
def index():
    s = db.session()
    tab = request.args.get("tab", "all")
    base = select(Incident).where(Incident.workspace_id == g.workspace.id)
    if tab == "open":
        base = base.where(Incident.resolved_at.is_(None))
    elif tab == "resolved":
        base = base.where(Incident.resolved_at.is_not(None))
    else:
        tab = "all"
    rows = s.scalars(base.order_by(Incident.resolved_at.is_not(None), Incident.opened_at.desc()).limit(SHOWN)).all()
    return render_template("incidents.html", rows=rows, tab=tab, open_count=incidents.open_count(s, g.workspace.id),
                           open_rows=[row for row in rows if row.is_open], groups=group(row for row in rows
                                                                                        if not row.is_open))


def group(rows):
    """Resolved incidents, one group per monitor, the monitor with the latest incident first:
    [(monitor, [incidents, newest first], seconds down in all)]."""
    groups = {}
    for incident in rows:
        groups.setdefault(incident.monitor_id, (incident.monitor, []))[1].append(incident)
    return [(monitor, found, sum((row.resolved_at - row.opened_at).total_seconds() for row in found))
            for monitor, found in groups.values()]


@bp.get("/incidents/<int:incident_id>")
@login_required
def page(incident_id):
    s = db.session()
    incident = get(s, incident_id)
    end = incident.resolved_at or incident.confirmed_at + timedelta(hours=1)
    checks = s.scalars(select(Check).where(Check.monitor_id == incident.monitor_id,
                                           Check.scheduled_at >= incident.opened_at - CHECKS_AROUND,
                                           Check.scheduled_at <= end + CHECKS_AROUND)
                       .order_by(Check.scheduled_at.desc()).limit(MAX_CHECKS)).all()
    data = {}
    for extra in page_extras:
        data.update(extra(s, incident))
    return render_template("incident.html", incident=incident, checks=checks, **data)


def monitor_extras(s, monitor):
    """One monitor's page: its open incident (a banner) and its last incidents."""
    recent = s.scalars(select(Incident).where(Incident.monitor_id == monitor.id)
                       .order_by(Incident.opened_at.desc()).limit(5)).all()
    current = recent[0] if recent and recent[0].is_open else None
    return {"open_incident": current, "recent_incidents": recent}


def init_app(app):
    from . import monitors as monitor_views
    if monitor_extras not in monitor_views.page_extras:
        monitor_views.page_extras.append(monitor_extras)
