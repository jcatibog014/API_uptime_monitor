"""Sending one alert through one channel, and saying how it went.

Each kind of channel talks to a different service, but every answer becomes
one of three:

    Delivered   it arrived (or the service took it)
    TryLater    worth trying again: a 429 "too many" (with its Retry-After),
                a 5xx, a timeout, a dropped connection
    Rejected    it will never work as it is: a deleted webhook (404), a wrong
                token (401/403), a refused address (400). Trying again only
                spams the service; the owner has to fix the channel.

alerts.py turns these into the queue's Retry and GiveUp. Every HTTP call goes
through OPENER, which tests swap for a pretend one: no test calls the
internet.

Webhook addresses are checked when a channel is saved (they must be Discord's
or Slack's own addresses), so Pulse Check can't be made to post into a
private network through a "webhook" either.
"""
import json
import random
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

from flask import current_app
from sqlalchemy import select

from . import crypto
from .models import PracticeMessage, PushSubscription

TIMEOUT = 10  # seconds
OPENER = urllib.request.urlopen
DISCORD = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+")
SLACK = re.compile(r"https://hooks\.slack\.com/(?:services|workflows|triggers)/[\w/-]+")
TELEGRAM_TOKEN = re.compile(r"\d{5,}:[\w-]{30,}")
PRACTICE_BUSY = 0.15  # a practice channel says "too many requests" this often, so retries show
RED, GREEN, GREY = 0xDC2626, 0x10B981, 0x6B7280


@dataclass
class Delivered:
    note: str = ""


@dataclass
class TryLater:
    reason: str
    after: float | None = None  # seconds, from the service's Retry-After


@dataclass
class Rejected:
    reason: str


# ---------- HTTP ----------

def retry_after(headers, body=None):
    for name in ("Retry-After", "retry-after", "x-ratelimit-reset-after"):
        value = headers.get(name) if headers else None
        try:
            if value is not None:
                return max(1.0, float(value))
        except ValueError:
            pass
    if isinstance(body, dict):
        value = body.get("retry_after") or (body.get("parameters") or {}).get("retry_after")
        if isinstance(value, (int, float)):
            return max(1.0, float(value))
    return None


def post(url, payload, headers=None, service="The service"):
    """POST JSON and turn the answer into Delivered / TryLater / Rejected."""
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "PulseCheck/1.0 (alerts)", **(headers or {})})
    try:
        with OPENER(request, timeout=TIMEOUT) as response:
            return Delivered(f"{service} answered {response.status}.")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read() or b"{}")
        except ValueError:
            body = None
        detail = ""
        if isinstance(body, dict):  # Telegram says "description", Discord and Brevo "message"
            detail = str(body.get("description") or body.get("message") or "")[:150]
        said = f"{service} answered {error.code}" + (f": {detail}" if detail else ".")
        if error.code == 429:
            return TryLater(said, retry_after(error.headers, body))
        if error.code in (400, 401, 403, 404, 410):
            return Rejected(said)
        return TryLater(said, retry_after(error.headers, body))
    except (TimeoutError, socket.timeout):
        return TryLater(f"{service} didn't answer within {TIMEOUT} s.")
    except (urllib.error.URLError, OSError) as error:
        return TryLater(f"Couldn't reach {service} ({type(error).__name__}).")


# ---------- Each kind ----------

def text_of(alert):
    return f"{alert.title}\n\n{alert.body}" + (f"\n\n{alert.link}" if alert.link else "")


def colour(alert):
    return {"down": RED, "up": GREEN}.get(alert.event, GREY)


def send_discord(s, channel, alert, secrets, now):
    embed = {"title": alert.title[:256], "description": alert.body[:4000], "color": colour(alert)}
    if alert.link:
        embed["url"] = alert.link
    return post(secrets["url"], {"username": "Pulse Check", "embeds": [embed]}, service="Discord")


def send_slack(s, channel, alert, secrets, now):
    text = f"*{alert.title}*\n{alert.body}" + (f"\n<{alert.link}|Open in Pulse Check>" if alert.link else "")
    return post(secrets["url"], {"text": text}, service="Slack")


def send_telegram(s, channel, alert, secrets, now):
    url = f"https://api.telegram.org/bot{secrets['token']}/sendMessage"
    return post(url, {"chat_id": channel.settings.get("chat_id"), "text": text_of(alert)[:4000],
                      "disable_web_page_preview": True}, service="Telegram")


def send_email(s, channel, alert, secrets, now):
    config = current_app.config
    if not config.get("BREVO_API_KEY") or not config.get("BREVO_SENDER"):
        return Rejected("Email needs BREVO_API_KEY and BREVO_SENDER on the server.")
    html = "".join(f"<p>{line}</p>" for line in _html_lines(alert.body))
    if alert.link:
        html += f'<p><a href="{_escape(alert.link)}">Open in Pulse Check</a></p>'
    return post("https://api.brevo.com/v3/smtp/email", {
        "sender": {"name": "Pulse Check", "email": config["BREVO_SENDER"]},
        "to": [{"email": channel.settings.get("address")}],
        "subject": alert.title, "textContent": text_of(alert), "htmlContent": html,
    }, headers={"api-key": config["BREVO_API_KEY"]}, service="Brevo")


def _escape(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _html_lines(text):
    return [_escape(line) for line in (text or "").splitlines() if line.strip()]


def send_push(s, channel, alert, secrets, now):
    """To every browser of the workspace that turned on alerts."""
    config = current_app.config
    if not config.get("VAPID_PRIVATE_KEY"):
        return Rejected("Phone alerts need VAPID keys on the server (flask --app pulsecheck make-vapid-keys).")
    subscriptions = s.scalars(select(PushSubscription).where(PushSubscription.workspace_id == channel.workspace_id)).all()
    if not subscriptions:
        return Rejected("No phone or browser has turned on alerts yet.")
    import pywebpush
    payload = json.dumps({"title": alert.title, "body": alert.body[:240], "url": alert.link})
    delivered, problems = 0, []
    for subscription in subscriptions:
        try:
            pywebpush.webpush(subscription_info={"endpoint": subscription.endpoint,
                                                 "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth}},
                              data=payload, vapid_private_key=config["VAPID_PRIVATE_KEY"],
                              vapid_claims={"sub": config.get("VAPID_SUBJECT") or "mailto:alerts@pulsecheck.invalid"},
                              timeout=TIMEOUT)
            delivered += 1
        except pywebpush.WebPushException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status in (404, 410):  # the browser turned alerts off, or was reset: forget it
                s.delete(subscription)
            else:
                problems.append(str(status or "no answer"))
    if delivered:
        return Delivered(f"Sent to {delivered} device{'s' if delivered != 1 else ''}.")
    if problems:
        return TryLater(f"The push service answered {', '.join(problems)}.")
    return Rejected("Every device has turned alerts off.")


def send_practice(s, channel, alert, secrets, now):
    """The Practice Inbox: no network. Now and then it says "too many requests", like a
    real service would, so the retries can be seen (the dice are seeded by alert and try)."""
    if random.Random(f"practice:{alert.id}:{alert.tries}").random() < PRACTICE_BUSY:
        return TryLater("The practice service answered 429: too many requests.", after=20)
    if s.scalar(select(PracticeMessage.id).where(PracticeMessage.alert_id == alert.id)) is None:
        s.add(PracticeMessage(workspace_id=channel.workspace_id, alert_id=alert.id,
                              looks_like=channel.settings.get("looks_like", "email"), channel_name=channel.name,
                              title=alert.title, body=alert.body, link=alert.link, at=now))
    return Delivered("In the Practice Inbox.")


SENDERS = {"discord": send_discord, "slack": send_slack, "telegram": send_telegram, "email": send_email,
           "push": send_push, "practice": send_practice}
NEEDS_SECRETS = ("discord", "slack", "telegram")


def send(s, channel, alert, now):
    secrets = crypto.decrypt(channel.secret) or {}
    if channel.kind in NEEDS_SECRETS and not secrets:
        return Rejected("Its secret can't be read (the server's key changed). Add the channel again.")
    return SENDERS[channel.kind](s, channel, alert, secrets, now)


def telegram_chats(token):
    """The chats that have written to this bot lately: [(chat id, name)]. Telegram's getUpdates
    only knows chats that sent the bot a message, so the owner writes to it first."""
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/getUpdates",
                                     headers={"User-Agent": "PulseCheck/1.0 (alerts)"})
    try:
        with OPENER(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return None, f"Telegram answered {error.code}. Check the bot token."
    except (urllib.error.URLError, OSError, ValueError):
        return None, "Couldn't reach Telegram. Please try again."
    chats = {}
    for update in body.get("result", []):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = chat.get("title") or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part) or chat.get("username") or str(chat["id"])
    return sorted(chats.items(), key=lambda item: item[1].lower()), None
