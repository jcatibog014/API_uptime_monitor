"""Monitor pages: the list, add, change, delete, one monitor's page, and Check now."""
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from sqlalchemy import func, select

from .. import (alert_rules, checking, db, heartbeat, incidents, insights, limits, monitors, practice, probe,
               scheduler, worker)
from ..models import Channel, MonitorChannel
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
    shown = monitors.form_values(values) if errors else values
    return render_template(template, values=shown, errors=errors, monitor=monitor, kinds=monitors.KINDS,
                           intervals=monitors.INTERVALS, timeouts=monitors.TIMEOUTS,
                           every=heartbeat.EVERY, graces=heartbeat.GRACE, slow_limits=monitors.SLOW_LIMITS,
                           goals=[(goal, label) for goal, label in insights.GOALS],
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
        kind = "practice" if g.workspace.is_demo else request.args.get("kind", "http")
        return form_page("monitor_form.html", monitors.defaults(kind if kind in monitors.KINDS else
                                                                "practice" if g.workspace.is_demo else "http"), {})
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
    values, errors = monitors.read_form(request.form, current_app.config["ALLOW_PRIVATE_TARGETS"], g.workspace.is_demo,
                                        kind=monitor.kind)
    if errors:
        return form_page("monitor_form.html", values, errors, monitor)
    old_interval, old_grace = monitor.interval_s, monitor.grace_s
    monitors.save(s, g.workspace, values, monitor)
    if monitor.interval_s != old_interval or monitor.grace_s != old_grace:
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
    if request.args.get("back") == "list":  # the pause button on the Monitors list
        return redirect(url_for("monitors.index"))
    return redirect(url_for("monitors.page", monitor_id=monitor.id))


@bp.route("/<int:monitor_id>/alerts", methods=["GET", "POST"])
@login_required
def alert_settings(monitor_id):
    """Who hears about this monitor, reminders and escalation (alert_rules.py)."""
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    channels = s.scalars(select(Channel).where(Channel.workspace_id == g.workspace.id).order_by(Channel.id)).all()
    routing = {row.channel_id: row.level for row in s.scalars(select(MonitorChannel).where(
        MonitorChannel.monitor_id == monitor.id))}
    errors = {}
    if request.method == "POST":
        ids = {channel.id for channel in channels}
        level_1 = [int(value) for value in request.form.getlist("channel") if value.isdigit() and int(value) in ids]
        level_2 = [int(value) for value in request.form.getlist("escalate_to") if value.isdigit() and int(value) in ids]
        remind = _number(request.form.get("remind_every_m"), alert_rules.REMIND_CHOICES)
        escalate = _number(request.form.get("escalate_after_m"), alert_rules.ESCALATE_CHOICES)
        if escalate and not [channel_id for channel_id in level_2 if channel_id not in level_1]:
            errors["escalate_to"] = "Choose who gets the escalation (someone who doesn't get the first alerts)."
        if not errors:
            alert_rules.set_routing(s, monitor, level_1, level_2)
            monitor.remind_every_m, monitor.escalate_after_m = remind, escalate
            s.commit()
            flash("The alert rules are saved.")
            return redirect(url_for("monitors.page", monitor_id=monitor.id, _anchor="alert-rules"))
        routing = {**{channel_id: 2 for channel_id in level_2}, **{channel_id: 1 for channel_id in level_1}}
    return render_template("alert_rules.html", monitor=monitor, channels=channels, routing=routing, errors=errors,
                           form=request.form, reminds=alert_rules.REMIND_CHOICES,
                           escalates=alert_rules.ESCALATE_CHOICES), 400 if errors else 200


def _number(value, choices):
    return int(value) if value and value.isdigit() and int(value) in dict(choices) else None


def rule_extras(s, monitor):
    channels = alert_rules.channels_for(s, monitor)
    second = alert_rules.channels_for(s, monitor, level=2)
    chosen = s.scalar(select(func.count()).select_from(MonitorChannel).where(MonitorChannel.monitor_id == monitor.id,
                                                                            MonitorChannel.level == 1))
    return {"alert_channels": channels, "escalation_channels": second, "channels_chosen": bool(chosen)}


@bp.post("/<int:monitor_id>/ping")
@login_required
def test_ping(monitor_id):
    """A heartbeat monitor's ping, sent from its own page (what the job's call would do)."""
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    if monitor.kind != "heartbeat":
        abort(404)
    if monitor.paused:
        flash("It is paused: resume it first.", "error")
    else:
        heartbeat.ping(s, monitor, utcnow())
        s.commit()
        flash("Ping received. The next one is expected within its time.")
    return redirect(url_for("monitors.page", monitor_id=monitor.id))


@bp.post("/<int:monitor_id>/check")
@login_required
def check_now(monitor_id):
    """Check it straight away, inside this request (the result is on the page that comes back)."""
    s = db.session()
    monitor = monitors.get(s, monitor_id)
    if monitor.kind == "heartbeat":
        abort(404)
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
    app.jinja_env.globals["monitor_address"] = address
    app.jinja_env.globals["heartbeat_every"] = lambda monitor: dict(heartbeat.EVERY).get(
        monitor.interval_s, monitors.interval_words(monitor.interval_s))
    if cert_extras not in page_extras:
        page_extras.append(cert_extras)
    if rule_extras not in page_extras:
        page_extras.append(rule_extras)


def address(monitor):
    """What the pages show as a monitor's address. A practice address keeps its script's settings
    after "?" (practice.py): not for people to read. A heartbeat has no address of ours to call."""
    if monitor.kind == "practice":
        return monitor.url.split("?")[0]
    if monitor.kind == "heartbeat":
        return "Heartbeat: your job calls Pulse Check"
    return monitor.url


def cert_extras(s, monitor):
    from .. import certificates
    return {"cert_days": certificates.days_left(monitor, utcnow())}
