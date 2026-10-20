"""Encryption at rest for the Xero OAuth tokens.

Tokens are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a key derived
from InvenTree's SECRET_KEY, so a copy of the database alone doesn't give access
to Xero. If SECRET_KEY changes, stored tokens can't be read and Xero has to be
reconnected (the plugin says so).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

PREFIX = 'enc1:'


class TokenUnreadable(Exception):
    """Stored token can't be decrypted (SECRET_KEY changed) - reconnect to Xero."""


def _fernet():
    digest = hashlib.sha256(f'inventree-xero:{settings.SECRET_KEY}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    if not value:
        return ''
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(stored):
    if not stored:
        return ''
    if not stored.startswith(PREFIX):
        return stored  # written before encryption was added; re-encrypted on next save
    try:
        return _fernet().decrypt(stored[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise TokenUnreadable(
            'The stored Xero tokens could not be decrypted (InvenTree\'s SECRET_KEY has changed). '
            'Reconnect to Xero from the plugin settings.'
        ) from exc


def is_encrypted(stored):
    return not stored or stored.startswith(PREFIX)
