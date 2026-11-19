"""Encrypting channel secrets (webhook addresses, bot tokens) before they go into the database.

Anyone holding a Discord webhook address can post into that channel, and a
Telegram bot token controls the whole bot, so they're never stored in plain
text. Fernet (from the cryptography library) encrypts and signs, so a changed
value can't be read either. Its key comes from ENCRYPTION_KEY (any long random
text; Render makes one), turned into the 32 bytes Fernet wants. Locally,
without ENCRYPTION_KEY, it is made from SECRET_KEY instead.

If the key ever changes, stored secrets can't be read any more: decrypt()
returns None and the channel says to add the webhook again.
"""
import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


def _fernet():
    secret = current_app.config.get("ENCRYPTION_KEY") or current_app.config["SECRET_KEY"]
    key = hashlib.sha256(b"pulsecheck-channel:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(values):
    """A dict of secrets -> one encrypted text."""
    return _fernet().encrypt(json.dumps(values).encode()).decode()


def decrypt(token):
    """The dict again, or None if it can't be read (no secret, or a different key)."""
    if not token:
        return None
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, ValueError):
        return None
