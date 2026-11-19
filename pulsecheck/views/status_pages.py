"""Status pages (make them, choose monitors), maintenance windows, incident notes, and the public
pages themselves (/status/<slug> and its RSS feed: no login)."""
from email.utils import format_datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import db, maintenance, monitors, status
from ..auth import login_required, name_problem
from ..dates import utcnow
from ..models import Incident, Maintenance, MaintenanceMonitor, StatusPage, StatusPageMonitor

bp = Blueprint("status_pages", __name__)
public = Blueprint("public", __name__)
MAX_PAGES = 5


def get_page(s, page_id):
    page = s.get(StatusPage, page_id)
    if page is None or page.workspace_id != g.workspace.id:
        abort(404)
    return page


@bp.get("/status-pages")
@login_required
def index():
    s = db.session()
    pages = s.scalars(select(StatusPage).where(StatusPage.workspace_id == g.workspace.id).order_by(StatusPage.id)).all()
    return render_template("status_pages.html", pages=pages, room=MAX_PAGES - len(pages))


@bp.post("/status-pages")
@login_required
def create():
    s = db.session()
    if len(s.scalars(select(StatusPage.id).where(StatusPage.workspace_id == g.workspace.id)).all()) >= MAX_PAGES:
        flash(f"A workspace can have at most {MAX_PAGES} status pages.", "error")
        return redirect(url_for("status_pages.index"))
    name = f"{g.workspace.name} Status"[:80]
    page = StatusPage(workspace_id=g.workspace.id, name=name, slug=status.new_slug(g.workspace.name))
    s.add(page)
    for position, monitor in enumerate(monitors.all_of(s, g.workspace.id)):
        page.entries.append(StatusPageMonitor(monitor_id=monitor.id, position=position))
    s.commit()
    flash("A status page is made, with every monitor on it. Choose what it shows.")
    return redirect(url_for("status_pages.edit", page_id=page.id))


@bp.route("/status-pages/<int:page_id>", methods=["GET", "POST"])
@login_required
def edit(page_id):
    s = db.session()
    page = get_page(s, page_id)
    all_monitors = monitors.all_of(s, g.workspace.id)
    errors, form = {}, request.form
    if request.method == "POST":
        name = " ".join(form.get("name", "").split())
        slug = form.get("slug", "").strip().lower()
        intro = form.get("intro", "").strip()
        if problem := name_problem(name, "a name for the page"):
            errors["name"] = problem
        if problem := status.slug_problem(slug):
            errors["slug"] = problem
        elif s.scalar(select(StatusPage.id).where(StatusPage.slug == slug, StatusPage.id != page.id)):
            errors["slug"] = "Another status page has that address. Choose another."
        if len(intro) > 300:
            errors["intro"] = "Use at most 300 characters."
        chosen = [monitor for monitor in all_monitors if form.get(f"show_{monitor.id}") == "1"]
        if not chosen:
            errors["monitors"] = "Choose at least one monitor to show."
        if not errors:
            page.name, page.slug, page.intro = name, slug, intro
            page.entries.clear()
            s.flush()
            for monitor in chosen:
                try:
                    position = int(form.get(f"position_{monitor.id}", "0"))
                except ValueError:
                    position = 0
                label = " ".join(form.get(f"label_{monitor.id}", "").split())[:80]
                page.entries.append(StatusPageMonitor(monitor_id=monitor.id, position=position, label=label))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                errors["slug"] = "Another status page has that address. Choose another."
            else:
                flash("The status page is saved.")
                return redirect(url_for("status_pages.edit", page_id=page.id))
    shown = {entry.monitor_id: entry for entry in page.entries}
    return render_template("status_page_form.html", page=page, monitors=all_monitors, shown=shown, errors=errors,
                           form=form), 400 if errors else 200


@bp.post("/status-pages/<int:page_id>/delete")
@login_required
def delete(page_id):
    s = db.session()
    page = get_page(s, page_id)
    s.delete(page)
    s.commit()
    flash("The status page is deleted. Its address doesn't work any more.")
    return redirect(url_for("status_pages.index"))


# ---------- Maintenance ----------

@bp.route("/maintenance", methods=["GET", "POST"])
@login_required
def windows():
    s = db.session()
    all_monitors = monitors.all_of(s, g.workspace.id)
    errors, form = {}, request.form
    if request.method == "POST":
        values, errors = maintenance.read_form(form, g.workspace.time_zone, {monitor.id for monitor in all_monitors})
        if not errors:
            window = Maintenance(workspace_id=g.workspace.id, title=values["title"], note=values["note"],
                                 starts_at=values["starts_at"], ends_at=values["ends_at"])
            s.add(window)
            s.flush()
            for monitor_id in values["monitors"]:
                s.add(MaintenanceMonitor(maintenance_id=window.id, monitor_id=monitor_id))
            s.commit()
            flash(f"{window.title} is planned. Its monitors rest during it.")
            return redirect(url_for("status_pages.windows"))
    now = utcnow()
    return render_template("maintenance.html", upcoming=maintenance.of_workspace(s, g.workspace.id, now),
                           past=maintenance.of_workspace(s, g.workspace.id, now, upcoming=False),
                           monitors=all_monitors, errors=errors, form=form, now=now), 400 if errors else 200


@bp.post("/maintenance/<int:window_id>/delete")
@login_required
def delete_window(window_id):
    s = db.session()
    window = s.get(Maintenance, window_id)
    if window is None or window.workspace_id != g.workspace.id:
        abort(404)
    s.delete(window)
    s.commit()
    flash("The maintenance window is deleted.")
    return redirect(url_for("status_pages.windows"))


# ---------- Incident notes ----------

@bp.post("/incidents/<int:incident_id>/updates")
@login_required
def add_update(incident_id):
    s = db.session()
    incident = s.get(Incident, incident_id)
    if incident is None or incident.workspace_id != g.workspace.id:
        abort(404)
    state = request.form.get("status", "")
    message = request.form.get("message", "").strip()
    if state not in status.UPDATE_STATUSES or not message or len(message) > status.NOTE_MAX:
        flash(f"Choose a stage and write a note (at most {status.NOTE_MAX} characters).", "error")
    else:
        status.add_update(s, incident, state, message, g.user.name, utcnow())
        s.commit()
        flash("The note is added. Status pages that show this monitor show it too.")
    return redirect(url_for("incidents.page", incident_id=incident.id, _anchor="notes"))


def incident_notes(s, incident):
    return {"notes": status.updates_for(s, [incident])[incident.id], "note_statuses": status.UPDATE_STATUSES,
            "incident_panels": ["_incident_notes.html"]}


# ---------- The public pages ----------

def public_page(slug):
    s = db.session()
    page = s.scalar(select(StatusPage).where(StatusPage.slug == slug))
    if page is None:
        abort(404)
    return s, page


@public.get("/status/<slug>")
def show(slug):
    s, page = public_page(slug)
    return render_template("status_public.html", page=page, **status.view(s, page, utcnow()))


@public.get("/status/<slug>/feed.xml")
def feed(slug):
    """The page's incidents and notes as RSS, for feed readers and chat tools."""
    s, page = public_page(slug)
    data = status.view(s, page, utcnow())
    items = []
    for incident in data["open_incidents"] + data["recent"]:
        label = data["labels"].get(incident.monitor_id, incident.monitor.name)
        notes = data["updates"].get(incident.id, [])
        moment = notes[0].created_at if notes else (incident.resolved_at or incident.opened_at)
        state = "Resolved" if incident.resolved_at else (status.UPDATE_STATUSES[notes[0].status] if notes else "Down")
        items.append({"title": f"{label}: {state}", "text": notes[0].message if notes else incident.message,
                      "id": f"incident-{incident.id}-{len(notes)}", "date": format_datetime(moment)})
    body = render_template("status_feed.xml", page=page, items=items)
    return body, 200, {"Content-Type": "application/rss+xml; charset=utf-8"}


def init_app(app):
    status.init_app(app)
    from . import incidents as incident_views
    if incident_notes not in incident_views.page_extras:
        incident_views.page_extras.append(incident_notes)
    from .. import charts
    app.jinja_env.globals.update(public_word=lambda state: status.PUBLIC[state],
                                 note_label=lambda key: status.UPDATE_STATUSES.get(key, key.capitalize()),
                                 strip_of=charts.strip)
