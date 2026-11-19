"""Monitor pages: the list, add, change, delete, one monitor's page, and Check now."""
from flask import Blueprint, current_app, flash, g, redirect, render_template, request, url_for

from .. import checking, db, incidents, limits, monitors, practice, probe, scheduler, worker
from ..auth import login_required
from ..dates import utcnow

bp = Blueprint("monitors", __name__, url_prefix="/monitors")
CHECKS_SHOWN = 20
# Check now calls the address straight away, so it has its own limit.
CHECK_NOW_PER_MINUTE = 6
# Extra parts of one monitor's page that later stages add: function(s, monitor) -> dict.
page_extras = []
# Called after a monitor is added, changed, paused or resumed: function(s, monitor, moment).
on_change = []


def changed(s, monitor, moment):
    for hook in on_change:
        hook(s, monitor, moment)
    s.commit()
    worker.nudge()  # its next check may be sooner than the crew's next wake-up


def form_page(template, values, errors, monitor=None):
    return render_template(template, values=values, errors=errors, monitor=monitor,
                           intervals=monitors.INTERVALS, timeouts=monitors.TIMEOUTS,
                           services=practice.choices(),
                           methods=[(method, method) for method in probe.METHODS]), 400 if errors else 200


@bp.get("")
@login_required
def index():
    s = db.session()
    rows = [(monitor, checking.last(s, monitor.id)) for monitor in monitors.all_of(s, g.workspace.id)]
    return render_template("monitors.html", rows=rows, room=monitors.MAX_MONITORS - len(rows))


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    s = db.session()
    if monitors.count(s, g.workspace.id) >= monitors.MAX_MONITORS:
        flash(f"You have {monitors.MAX_MONITORS} monitors, the most a workspace can have. "
              "Delete one to add another.", "error")
        return redirect(url_for("monitors.index"))
    if request.method == "GET":
        return form_page("monitor_form.html", monitors.defaults(), {})
    values, errors = monitors.read_form(request.form, current_app.config["ALLOW_PRIVATE_TARGETS"], g.workspace.is_demo)
    if errors:
        return form_page("monitor_form.html", values, errors)
    monitor = monitors.save(s, g.workspace, values)
    scheduler.schedule(monitor, utcnow(), check_now=True)
    changed(s, monitor, utcnow())
    flash(f"{monitor.name} is added. Its first check is on its way.")
    return redirect(url_for("monitors.page", monitor_id=monitor.id))


@bp.get("/<int:monitor_id>")
@login_required
def page(monitor_id):
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    data = {}
    for extra in page_extras:
        data.update(extra(s, monitor))
    return render_template("monitor.html", monitor=monitor, checks=checking.recent(s, monitor.id, CHECKS_SHOWN),
                           interval_words=monitors.interval_words(monitor.interval_s), **data)


@bp.route("/<int:monitor_id>/edit", methods=["GET", "POST"])
@login_required
def edit(monitor_id):
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    if request.method == "GET":
        return form_page("monitor_form.html", monitors.values_of(monitor), {}, monitor)
    values, errors = monitors.read_form(request.form, current_app.config["ALLOW_PRIVATE_TARGETS"], g.workspace.is_demo)
    if errors:
        return form_page("monitor_form.html", values, errors, monitor)
    old_interval = monitor.interval_s
    monitors.save(s, g.workspace, values, monitor)
    if monitor.interval_s != old_interval:
        scheduler.schedule(monitor, utcnow())  # onto the new timetable
    changed(s, monitor, utcnow())
    flash("The monitor is saved.")
    return redirect(url_for("monitors.page", monitor_id=monitor.id))


@bp.post("/<int:monitor_id>/delete")
@login_required
def delete(monitor_id):
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    name = monitor.name
    s.delete(monitor)
    s.commit()
    flash(f"{name} is deleted, with all its checks.")
    return redirect(url_for("monitors.index"))


@bp.post("/<int:monitor_id>/pause")
@login_required
def pause(monitor_id):
    """Pause checks (nothing is counted while paused), or resume them with a check straight away."""
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    monitor.paused = not monitor.paused
    incidents.reset_streaks(monitor)
    scheduler.schedule(monitor, utcnow(), check_now=True)
    changed(s, monitor, utcnow())
    flash(f"{monitor.name} is paused. It isn't checked until you resume it." if monitor.paused
          else f"{monitor.name} is checked again, starting now.")
    return redirect(url_for("monitors.page", monitor_id=monitor.id))


@bp.post("/<int:monitor_id>/check")
@login_required
def check_now(monitor_id):
    """Check it straight away, inside this request (the result is on the page that comes back)."""
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    moment = utcnow()
    if not limits.hit(s, f"check-now:{g.workspace.id}", CHECK_NOW_PER_MINUTE, 60, moment):
        s.commit()
        flash("That's a lot of checks in one minute. Please wait a moment.", "error")
        return redirect(url_for("monitors.page", monitor_id=monitor.id))
    check, result = checking.run_check(s, monitor, moment, moment, manual=True)
    for hook in scheduler.after_check:  # a real observation: it counts like any other check
        hook(s, monitor, check, result, moment)
    s.commit()
    if result.ok:
        flash(f"Up: {result.message}, in {round(result.total_ms):,} ms.")
    else:
        flash(f"Down: {result.message}", "error")
    return redirect(url_for("monitors.page", monitor_id=monitor.id))


def init_app(app):
    app.jinja_env.globals["monitor_interval"] = lambda monitor: monitors.interval_words(monitor.interval_s)
    # A practice address keeps its script's settings after "?" (practice.py): not for people to read.
    app.jinja_env.globals["monitor_address"] = lambda monitor: (
        monitor.url.split("?")[0] if monitor.kind == "practice" else monitor.url)
