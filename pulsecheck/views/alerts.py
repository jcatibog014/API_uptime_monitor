"""Alert pages: the channels (add, test, pause, delete), the alerts sent, the Practice Inbox,
phones turning alerts on, and the Acknowledge link from an alert."""
import click
from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for
from flask.cli import with_appcontext
from sqlalchemy import delete, select

from .. import alerts, auth, crypto, db, pulse_words, senders, tokens, worker
from ..auth import login_required
from ..dates import utcnow
from ..models import Alert, Channel, Incident, PracticeMessage, PushSubscription

bp = Blueprint("alerts", __name__)
KINDS = {
    "email": ("Email", "An email to one address, sent through Brevo."),
    "discord": ("Discord", "A message in a Discord channel, through a webhook."),
    "slack": ("Slack", "A message in a Slack channel, through a webhook."),
    "telegram": ("Telegram", "A message from your own Telegram bot."),
    "push": ("Phone Alerts", "A notification on the phones and browsers that turn alerts on."),
    "practice": ("Practice Inbox", "Alerts stay inside Pulse Check, shown the way each service would show them."),
}
LOOKS = [("email", "Email"), ("discord", "Discord"), ("telegram", "Telegram"), ("push", "Phone")]
MAX_CHANNELS = 10
SHOWN = 60


def get_channel(s, channel_id):
    channel = s.get(Channel, channel_id)
    if channel is None or channel.workspace_id != g.workspace.id:
        abort(404)
    return channel


def kind_ready(kind):
    """None if the server can send this kind, else why not."""
    config = current_app.config
    if kind == "email" and not (config.get("BREVO_API_KEY") and config.get("BREVO_SENDER")):
        return "Email needs BREVO_API_KEY and BREVO_SENDER on the server (docs/DEPLOY.md)."
    if kind == "push" and not (config.get("VAPID_PUBLIC_KEY") and config.get("VAPID_PRIVATE_KEY")):
        return "Phone alerts need VAPID keys on the server: flask --app pulsecheck make-vapid-keys."
    return None


@bp.get("/alerts")
@login_required
def index():
    s = db.session()
    channels = s.scalars(select(Channel).where(Channel.workspace_id == g.workspace.id).order_by(Channel.id)).all()
    kinds = ["practice"] if g.workspace.is_demo else list(KINDS)
    devices = s.scalars(select(PushSubscription).where(PushSubscription.workspace_id == g.workspace.id)).all()
    return render_template("alerts.html", channels=channels, kinds=kinds, kind_words=KINDS, ready=kind_ready,
                           devices=devices, room=MAX_CHANNELS - len(channels), breaker_after=alerts.BREAKER_AFTER,
                           looks={"email": "an email", "discord": "a Discord message", "telegram": "a Telegram message",
                                  "push": "a phone notification"})


def read_channel_form(kind, form):
    """(name, settings, secrets, errors)."""
    errors, settings, secrets = {}, {}, {}
    name = " ".join(form.get("name", "").split())
    if problem := auth.name_problem(name, "a name for the channel"):
        errors["name"] = problem
    if kind == "email":
        address = auth.clean_email(form.get("address", ""))
        if problem := auth.email_problem(address):
            errors["address"] = problem
        settings["address"] = address
    elif kind in ("discord", "slack"):
        url = form.get("url", "").strip()
        pattern = senders.DISCORD if kind == "discord" else senders.SLACK
        if not pattern.fullmatch(url):
            errors["url"] = ("Paste the webhook address from the channel's settings (Integrations, Webhooks)."
                             if kind == "discord" else "Paste the webhook address Slack made, starting https://hooks.slack.com/.")
        secrets["url"] = url
    elif kind == "telegram":
        token = form.get("token", "").strip()
        if not senders.TELEGRAM_TOKEN.fullmatch(token):
            errors["token"] = "Paste the token BotFather gave you, like 123456789:AAH..."
        secrets["token"] = token
        chat_id, _bar, chat_name = form.get("chat_id", "").partition("|")  # the radio carries "id|name"
        settings["chat_id"], settings["chat_name"] = chat_id.strip(), chat_name.strip()[:80]
    elif kind == "practice":
        looks = form.get("looks_like", "email")
        settings["looks_like"] = looks if looks in dict(LOOKS) else "email"
    return name, settings, secrets, errors


@bp.route("/alerts/new/<kind>", methods=["GET", "POST"])
@login_required
def new(kind):
    if kind not in KINDS:
        abort(404)
    if g.workspace.is_demo and kind != "practice":
        abort(403, "The demo's alerts go to the Practice Inbox, never a real service.")
    s = db.session()
    if len(s.scalars(select(Channel.id).where(Channel.workspace_id == g.workspace.id)).all()) >= MAX_CHANNELS:
        flash(f"A workspace can have at most {MAX_CHANNELS} channels.", "error")
        return redirect(url_for("alerts.index"))
    if problem := kind_ready(kind):
        flash(problem, "error")
        return redirect(url_for("alerts.index"))
    form, errors, chats = request.form, {}, None
    if request.method == "POST":
        name, settings, secrets, errors = read_channel_form(kind, form)
        if kind == "telegram" and not errors and not settings["chat_id"]:
            chats, problem = senders.telegram_chats(secrets["token"])  # step 2: pick the chat
            if problem:
                errors["token"] = problem
            elif not chats:
                errors["token"] = "Nobody has written to this bot yet. Send it any message in Telegram, then try again."
            if not errors:
                return render_template("channel_form.html", kind=kind, kind_words=KINDS, looks=LOOKS, form=form,
                                       errors={}, chats=chats)
        if not errors:
            channel = Channel(workspace_id=g.workspace.id, kind=kind, name=name, settings=settings,
                              secret=crypto.encrypt(secrets) if secrets else "")
            s.add(channel)
            s.commit()
            flash(f"{name} is added. Send a test to see it work.")
            return redirect(url_for("alerts.index", _anchor=f"channel-{channel.id}"))
    return render_template("channel_form.html", kind=kind, kind_words=KINDS, looks=LOOKS, form=form, errors=errors,
                           chats=chats), 400 if errors else 200


@bp.post("/alerts/<int:channel_id>/test")
@login_required
def test(channel_id):
    s = db.session()
    channel = get_channel(s, channel_id)
    alerts.send_test(s, channel, utcnow())
    s.commit()
    worker.nudge()
    flash("A test alert is on its way. Its result shows under Sent in a moment.")
    return redirect(url_for("alerts.index", _anchor=f"channel-{channel.id}"))


@bp.post("/alerts/<int:channel_id>/toggle")
@login_required
def toggle(channel_id):
    s = db.session()
    channel = get_channel(s, channel_id)
    channel.enabled = not channel.enabled
    channel.fail_streak, channel.paused_until = 0, None
    s.commit()
    flash(f"{channel.name} gets alerts again." if channel.enabled else f"{channel.name} is paused: no alerts go there.")
    return redirect(url_for("alerts.index", _anchor=f"channel-{channel.id}"))


@bp.post("/alerts/<int:channel_id>/delete")
@login_required
def remove(channel_id):
    s = db.session()
    channel = get_channel(s, channel_id)
    name = channel.name
    s.delete(channel)
    s.commit()
    flash(f"{name} is deleted.")
    return redirect(url_for("alerts.index"))


@bp.get("/alerts/sent")
@login_required
def sent():
    s = db.session()
    rows = s.scalars(select(Alert).where(Alert.workspace_id == g.workspace.id)
                     .order_by(Alert.created_at.desc(), Alert.id.desc()).limit(SHOWN)).all()
    return render_template("alerts_sent.html", rows=rows)


@bp.get("/alerts/inbox")
@login_required
def inbox():
    s = db.session()
    rows = s.scalars(select(PracticeMessage).where(PracticeMessage.workspace_id == g.workspace.id)
                     .order_by(PracticeMessage.at.desc(), PracticeMessage.id.desc()).limit(SHOWN)).all()
    return render_template("practice_inbox.html", rows=rows)


# ---------- Phones turning alerts on (web push) ----------

@bp.post("/alerts/devices")
@login_required
def subscribe():
    """app.js posts the browser's push subscription here (JSON, with the X-CSRF-Token header)."""
    if g.workspace.is_demo:
        abort(403, "The demo's alerts go to the Practice Inbox.")
    data = request.get_json(silent=True) or {}
    keys = data.get("keys") or {}
    endpoint, p256dh, auth_key = data.get("endpoint", ""), keys.get("p256dh", ""), keys.get("auth", "")
    if not endpoint.startswith("https://") or len(endpoint) > 1000 or not p256dh or not auth_key:
        abort(400, "That isn't a push subscription.")
    s = db.session()
    s.execute(delete(PushSubscription).where(PushSubscription.endpoint == endpoint))
    s.add(PushSubscription(workspace_id=g.workspace.id, endpoint=endpoint, p256dh=p256dh[:200], auth=auth_key[:100]))
    s.commit()
    return jsonify(ok=True)


@bp.post("/alerts/devices/<int:device_id>/delete")
@login_required
def forget_device(device_id):
    s = db.session()
    device = s.get(PushSubscription, device_id)
    if device is None or device.workspace_id != g.workspace.id:
        abort(404)
    s.delete(device)
    s.commit()
    flash("That device won't get alerts any more.")
    return redirect(url_for("alerts.index", _anchor="devices"))


# ---------- Acknowledge (a link in every "down" alert) ----------

@bp.route("/ack/<token>", methods=["GET", "POST"])
def acknowledge(token):
    """No login needed: the signed token is the proof. GET only shows the page (mail apps and chat
    previews open links by themselves, so opening must never change anything); the button POSTs."""
    parts = tokens.read_ints("ack", token, 1)
    s = db.session()
    incident = s.get(Incident, parts[0]) if parts else None
    if incident is None:
        abort(404)
    if request.method == "POST":
        who = g.user.name if g.user is not None and g.user.workspace_id == incident.workspace_id else "Someone with the link"
        if alerts.acknowledge(s, incident, who, utcnow()):
            s.commit()
            flash("Thanks. Everyone can see you're on it, and reminders stop.")
        return redirect(url_for("alerts.acknowledge", token=token))
    return render_template("acknowledge.html", incident=incident, token=token)


@bp.post("/incidents/<int:incident_id>/ack")
@login_required
def acknowledge_here(incident_id):
    s = db.session()
    incident = s.get(Incident, incident_id)
    if incident is None or incident.workspace_id != g.workspace.id:
        abort(404)
    if alerts.acknowledge(s, incident, g.user.name, utcnow()):
        s.commit()
        flash("You're on it. Reminders stop.")
    return redirect(url_for("incidents.page", incident_id=incident.id))


def incident_extras(s, incident):
    rows = s.scalars(select(Alert).where(Alert.incident_id == incident.id).order_by(Alert.created_at, Alert.id)).all()
    return {"incident_alerts": rows, "incident_actions": ["_incident_ack.html"],
            "incident_facts": ["_incident_ack_fact.html"]}


@click.command("make-vapid-keys")
@with_appcontext
def make_vapid_keys_command():
    """Make the two keys for phone alerts. Put them in Render's environment."""
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid, b64urlencode
    keys = Vapid()
    keys.generate_keys()
    private = b64urlencode(keys.private_key.private_bytes(serialization.Encoding.DER,
                                                          serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))
    public = b64urlencode(keys.public_key.public_bytes(serialization.Encoding.X962,
                                                       serialization.PublicFormat.UncompressedPoint))
    click.echo(f"VAPID_PUBLIC_KEY={public}\nVAPID_PRIVATE_KEY={private}")


def init_app(app):
    from . import incidents as incident_views
    if incident_extras not in incident_views.page_extras:
        incident_views.page_extras.append(incident_extras)
    app.cli.add_command(make_vapid_keys_command)
    app.jinja_env.globals.update(alert_word=lambda status: pulse_words.ALERT[status],
                                 channel_kind=lambda kind: KINDS[kind][0], ack_link=alerts.ack_link)
