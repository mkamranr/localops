"""Encryption of secrets at rest (model API keys; integration credentials later).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with ``LOCALOPS_ENCRYPTION_KEY``. The key is
deliberately separate from ``APP_SECRET_KEY`` so signing keys can rotate without losing
stored credentials, and so backups can treat it as separate key material (spec §18).
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.core.errors import AppError


class EncryptionUnavailableError(AppError):
    status_code = 400
    code = "encryption_unavailable"


def _fernet(settings: Settings) -> Fernet:
    if settings.encryption_key is None or not settings.encryption_key.get_secret_value():
        raise EncryptionUnavailableError(
            "LOCALOPS_ENCRYPTION_KEY is not configured; secrets cannot be stored"
        )
    try:
        return Fernet(settings.encryption_key.get_secret_value().encode())
    except ValueError as exc:
        raise EncryptionUnavailableError(
            "LOCALOPS_ENCRYPTION_KEY is not a valid Fernet key"
        ) from exc


def encrypt_secret(settings: Settings, plaintext: str) -> str:
    return _fernet(settings).encrypt(plaintext.encode()).decode()


def decrypt_secret(settings: Settings, ciphertext: str) -> str:
    try:
        return _fernet(settings).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionUnavailableError(
            "Stored secret cannot be decrypted with the configured LOCALOPS_ENCRYPTION_KEY"
        ) from exc
