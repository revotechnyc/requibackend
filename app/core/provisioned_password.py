"""Encrypt/decrypt admin-provisioned member passwords for workspace seats."""

from __future__ import annotations

import base64
import hashlib
import secrets
import string

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def generate_temp_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_provisioned_password(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_provisioned_password(encrypted: str | None) -> str | None:
    if not encrypted:
        return None
    try:
        return _fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None
