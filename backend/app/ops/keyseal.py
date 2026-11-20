"""Passphrase-sealed key files for backups (spec §18: keys are backed up separately).

Format: a small JSON document. The key is derived from the passphrase with scrypt and
the payload is encrypted with AES-256-GCM; the KDF parameters, salt and format header
are bound as associated data, so any tampering fails authentication.
"""

from __future__ import annotations

import base64
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

FORMAT = "localops-keys"
VERSION = 1
MIN_PASSPHRASE = 12
DEFAULT_N = 2**17  # OWASP scrypt recommendation (N=2^17, r=8, p=1): ~128 MiB, well under 1 s


class KeySealError(Exception):
    """The key file is malformed, tampered with, or the passphrase is wrong."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: object) -> bytes:
    if not isinstance(text, str):
        raise KeySealError("malformed key file")
    try:
        return base64.b64decode(text, validate=True)
    except ValueError as exc:
        raise KeySealError("malformed key file") from exc


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def _aad(header: dict[str, object]) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode()


def seal(plaintext: bytes, passphrase: str, *, n: int | None = None) -> bytes:
    n = n or DEFAULT_N
    if len(passphrase) < MIN_PASSPHRASE:
        raise KeySealError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    salt, nonce = os.urandom(16), os.urandom(12)
    header: dict[str, object] = {
        "format": FORMAT,
        "version": VERSION,
        "kdf": {"name": "scrypt", "n": n, "r": 8, "p": 1, "salt": _b64(salt)},
        "cipher": "AES-256-GCM",
    }
    key = _derive(passphrase, salt, n, 8, 1)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(header))
    doc = {**header, "nonce": _b64(nonce), "ciphertext": _b64(ciphertext)}
    return (json.dumps(doc, indent=2) + "\n").encode()


def unseal(blob: bytes, passphrase: str) -> bytes:
    try:
        doc = json.loads(blob)
    except ValueError as exc:
        raise KeySealError("not a LocalOps key file") from exc
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise KeySealError("not a LocalOps key file")
    if doc.get("version") != VERSION or doc.get("cipher") != "AES-256-GCM":
        raise KeySealError("unsupported key file version")
    kdf = doc.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != "scrypt":
        raise KeySealError("unsupported key derivation")
    try:
        n, r, p = int(kdf["n"]), int(kdf["r"]), int(kdf["p"])
    except (KeyError, TypeError, ValueError) as exc:
        raise KeySealError("malformed key file") from exc
    if not (2**14 <= n <= 2**20 and r == 8 and p == 1):
        raise KeySealError("unsupported key derivation parameters")
    header = {k: doc[k] for k in ("format", "version", "kdf", "cipher")}
    key = _derive(passphrase, _unb64(kdf.get("salt")), n, r, p)
    try:
        return AESGCM(key).decrypt(
            _unb64(doc.get("nonce")), _unb64(doc.get("ciphertext")), _aad(header)
        )
    except InvalidTag as exc:
        raise KeySealError("wrong passphrase or the key file was modified") from exc
