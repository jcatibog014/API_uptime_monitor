"""Public status pages: what the people who USE your services see.

A status page (/status/<slug>, no login) lists the monitors you chose, each
with a plain word (Up, Slow, Down, Maintenance: no heart-monitor words here,
the readers aren't your team), its last 90 days, the open incidents with the
notes you wrote (Investigating, Identified, Monitoring, Resolved), planned
maintenance, and the last week's incidents. An RSS feed carries the same news.

A failing check that is still being checked again (Irregular) shows as Up:
the public hears about trouble once it is confirmed, never about a blip.
"""
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from . import maintenance, metrics, rollups
from .models import Incident, IncidentUpdate, Maintenance, MaintenanceMonitor, StatusPage

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,58}[a-z0-9]")
RECENT = timedelta(days=7)
UPDATE_STATUSES = {"investigating": "Investigating", "identified": "Identified", "monitoring": "Monitoring",
                   "resolved": "Resolved"}
NOTE_MAX = 1000
# The public word for a monitor's state: (label, tone, order of badness).
PUBLIC = {"down": ("Down", "flat", 3), "slow": ("Slow", "weak", 2), "rest": ("Maintenance", "rest", 1),
          "up": ("Up", "steady", 0), "new": ("Not Checked Yet", "quiet", 0)}
OVERALL = {3: ("Some Services Are Down", "flat"), 2: ("Some Services Are Slow", "weak"),
           1: ("Planned Maintenance in Progress", "rest"), 0: ("Everything Is Up", "steady")}


def public_state(monitor):
    if monitor.paused:
        return "new"
    return {"flatline": "down", "weak": "slow", "resting": "rest", "steady": "up", "irregular": "up"}.get(
        monitor.state, "new")


def new_slug(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "status"
    return f"{base}-{secrets.token_hex(3)}"


def slug_problem(slug):
    if not SLUG.fullmatch(slug or ""):
        return "Use small letters, numbers and dashes (3 to 60), like acme-status."
    return None


@dataclass
class Row:
    label: str
    monitor: object
    state: str
    days: list
    uptime: metrics.Stats


def view(s, page, now):
    """Everything the public page shows."""
    entries = list(page.entries)
    ids = [entry.monitor_id for entry in entries]
    zone_name = page_zone(s, page)
    done = rollups.done_until(s, now)
    strips = metrics.strip(s, ids, now, done, zone_name) if ids else {}
    rows = []
    for entry in entries:
        days = strips.get(entry.monitor_id, [])
        total = metrics.Stats()
        for day in days:
            total.add(day.stats)
        rows.append(Row(entry.label or entry.monitor.name, entry.monitor, public_state(entry.monitor), days, total))
    worst = max((PUBLIC[row.state][2] for row in rows), default=0)
    open_incidents = s.scalars(select(Incident).where(Incident.monitor_id.in_(ids), Incident.resolved_at.is_(None))
                               .order_by(Incident.opened_at.desc())).all() if ids else []
    recent = s.scalars(select(Incident).where(Incident.monitor_id.in_(ids), Incident.resolved_at >= now - RECENT)
                       .order_by(Incident.opened_at.desc()).limit(20)).all() if ids else []
    windows = s.scalars(select(Maintenance).join(MaintenanceMonitor).where(
        MaintenanceMonitor.monitor_id.in_(ids), Maintenance.ends_at > now).order_by(Maintenance.starts_at)
        .distinct()).all() if ids else []
    labels = {entry.monitor_id: entry.label or entry.monitor.name for entry in entries}
    return {"rows": rows, "overall": OVERALL[worst], "open_incidents": open_incidents, "recent": recent,
            "windows": windows, "labels": labels, "updates": updates_for(s, open_incidents + recent),
            "zone_name": zone_name}


def page_zone(s, page):
    from .models import Workspace
    return s.get(Workspace, page.workspace_id).time_zone


def updates_for(s, incidents):
    """{incident id: [IncidentUpdate, newest first]}."""
    ids = [incident.id for incident in incidents]
    found = {incident_id: [] for incident_id in ids}
    if ids:
        for update in s.scalars(select(IncidentUpdate).where(IncidentUpdate.incident_id.in_(ids))
                                .order_by(IncidentUpdate.created_at.desc())):
            found[update.incident_id].append(update)
    return found


def add_update(s, incident, status, message, author, now):
    """A note on an incident. "resolved" doesn't close it: only good checks do."""
    s.add(IncidentUpdate(incident_id=incident.id, status=status, message=message, author=author[:80], created_at=now))


def in_window(s, monitor_id, now):
    return maintenance.active(s, monitor_id, now)


# ---------- The demo: a status page, notes, and maintenance ----------

DEMO_LABELS = {"payments-api": "Checkout", "shipping-api": "Shipping rates", "booking-api": "Bookings",
               "search-api": "Search", "website": "Website", "login": "Sign in"}


def demo_page(s, workspace, by_service, start, now):
    """seed.extras: Lakbay Travel's public status page and two maintenance windows (a finished one
    and one planned for tomorrow at 2 AM, the workspace's time)."""
    from datetime import time as clock

    from .dates import at_local, to_zone
    from .models import Maintenance, MaintenanceMonitor, StatusPageMonitor
    page = StatusPage(workspace_id=workspace.id, name="Lakbay Travel Status", slug=new_slug("lakbay travel"),
                      intro="Can you book, pay and search right now? This page says, minute by minute.", created_at=start)
    s.add(page)
    for position, (service, label) in enumerate(DEMO_LABELS.items()):
        if service in by_service:
            page.entries.append(StatusPageMonitor(monitor_id=by_service[service].id, position=position, label=label))
    today = to_zone(now, workspace.time_zone).date()
    windows = [("Search index rebuild", "Search may be slow or empty for a few minutes.", today - timedelta(days=3),
                "search-api"),
               ("Database upgrade", "Bookings may fail for up to 30 minutes. Please try again after.",
                today + timedelta(days=1), "booking-api")]
    for title, note, day, service in windows:
        starts = at_local(day, clock(2, 0), workspace.time_zone)
        window = Maintenance(workspace_id=workspace.id, title=title, note=note, starts_at=starts,
                             ends_at=starts + timedelta(minutes=30), created_at=start)
        s.add(window)
        s.flush()
        s.add(MaintenanceMonitor(maintenance_id=window.id, monitor_id=by_service[service].id))


def demo_notes(s, workspace, now):
    """seed.after_replay: notes on the incidents the replay opened, as the team would have written them."""
    from .models import Monitor
    incidents = s.scalars(select(Incident).join(Monitor, Monitor.id == Incident.monitor_id).where(
        Incident.workspace_id == workspace.id, Incident.opened_at >= now - timedelta(hours=4),
        Monitor.name.in_(("Shipping API", "Payments API")))).all()
    for incident in incidents:
        start = incident.confirmed_at
        if incident.monitor.name == "Shipping API":
            notes = [(3, "investigating", "Shipping prices aren't showing at checkout. We're looking into it."),
                     (12, "identified", "A delivery partner's rates service is down. You can still book; shipping is "
                                        "added when it's back.")]
        else:
            notes = [(2, "investigating", "Some payments are timing out."),
                     (9, "resolved", "Payments work again. Nobody was charged twice.")]
        for minutes, state, message in notes:
            if start + timedelta(minutes=minutes) <= now:
                add_update(s, incident, state, message, "Rina Bautista", start + timedelta(minutes=minutes))


def init_app(app):
    from . import seed
    if demo_page not in seed.extras:
        seed.extras.append(demo_page)
        seed.after_replay.append(demo_notes)
