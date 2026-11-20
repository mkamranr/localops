"""Phase 7: Docker-secrets settings, production checks, sealed key files, TLS certificates."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import ExtendedKeyUsageOID
from pydantic import ValidationError

from app import cli
from app.core.config import Settings
from app.ops.certs import generate_self_signed
from app.ops.keyseal import KeySealError, seal, unseal

FAST_N = 2**14  # the minimum accepted work factor keeps the tests quick
KEY = "kJ0m3Qx3l5n9oA0m3cR2nS4pZ7Yw1vB6tU8xE5gH2dQ="


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


# ------------------------------------------------------------------ *_FILE secrets
def test_file_secrets_override_plain_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "app_secret_key"
    secret.write_text("file-secret-" + "x" * 40 + "\n")
    enc = tmp_path / "enc"
    enc.write_text(KEY)
    monkeypatch.setenv("APP_SECRET_KEY", "plain-" + "y" * 40)
    monkeypatch.setenv("APP_SECRET_KEY_FILE", str(secret))
    monkeypatch.setenv("LOCALOPS_ENCRYPTION_KEY_FILE", str(enc))
    monkeypatch.delenv("LOCALOPS_ENCRYPTION_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_secret_key.get_secret_value() == "file-secret-" + "x" * 40
    assert settings.encryption_key is not None
    assert settings.encryption_key.get_secret_value() == KEY


def test_unreadable_secret_file_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_APP_PASSWORD_FILE", "/nonexistent/secret")
    with pytest.raises(ValueError, match="DB_APP_PASSWORD_FILE"):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ------------------------------------------------------------------ production checks
PROD = {"app_env": "production", "app_secret_key": "s" * 48, "db_app_password": "apppw-real"}


def test_production_requires_a_valid_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALOPS_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError, match="LOCALOPS_ENCRYPTION_KEY must be set"):
        _settings(**PROD)
    monkeypatch.setenv("LOCALOPS_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(ValidationError, match="not a valid key"):
        _settings(**PROD)
    monkeypatch.setenv("LOCALOPS_ENCRYPTION_KEY", KEY)
    assert _settings(**PROD).is_production


def test_production_rejects_placeholder_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALOPS_ENCRYPTION_KEY", KEY)
    with pytest.raises(ValidationError, match="DB_APP_PASSWORD"):
        _settings(**{**PROD, "db_app_password": "CHANGE_ME"})


# ------------------------------------------------------------------ sealed key files
def test_seal_round_trip_and_failures() -> None:
    payload = b"APP_SECRET_KEY=abc\nLOCALOPS_ENCRYPTION_KEY=" + KEY.encode() + b"\n"
    blob = seal(payload, "correct horse battery", n=FAST_N)
    assert payload not in blob and KEY.encode() not in blob
    assert unseal(blob, "correct horse battery") == payload

    with pytest.raises(KeySealError, match="wrong passphrase"):
        unseal(blob, "wrong horse battery!")

    doc = json.loads(blob)
    ct = bytearray(doc["ciphertext"].encode())
    ct[10] = ord("A") if ct[10] != ord("A") else ord("B")
    with pytest.raises(KeySealError):
        unseal(json.dumps({**doc, "ciphertext": ct.decode()}).encode(), "correct horse battery")

    # The header is authenticated: weakening the KDF parameters is detected.
    weakened = {**doc, "kdf": {**doc["kdf"], "n": 2**15}}
    with pytest.raises(KeySealError):
        unseal(json.dumps(weakened).encode(), "correct horse battery")

    with pytest.raises(KeySealError, match="at least"):
        seal(payload, "short")
    with pytest.raises(KeySealError, match="not a LocalOps key file"):
        unseal(b"garbage", "correct horse battery")


def test_cli_seal_and_unseal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    passfile = tmp_path / "pass"
    passfile.write_text("a long backup passphrase\n")
    monkeypatch.setattr("app.ops.keyseal.DEFAULT_N", FAST_N)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"SECRET=1\n")))
    assert cli.main(["seal-keys", "--passphrase-file", str(passfile)]) == 0
    sealed = capsysbinary.readouterr().out
    assert b"SECRET" not in sealed

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(sealed)))
    assert cli.main(["unseal-keys", "--passphrase-file", str(passfile)]) == 0
    assert capsysbinary.readouterr().out == b"SECRET=1\n"

    passfile.write_text("another passphrase entirely")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(sealed)))
    assert cli.main(["unseal-keys", "--passphrase-file", str(passfile)]) == 1


# ------------------------------------------------------------------ TLS certificate
def test_self_signed_certificate() -> None:
    cert_pem, key_pem = generate_self_signed(["localops.example.com", "10.0.0.5", "localhost"])
    cert = x509.load_pem_x509_certificate(cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["localops.example.com", "localhost"]
    assert [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] == ["10.0.0.5"]
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in eku
    assert (cert.not_valid_after_utc - cert.not_valid_before_utc).days >= 825
    key = load_pem_private_key(key_pem, password=None)
    assert key.key_size == 3072  # type: ignore[union-attr]


def test_cli_gen_cert_refuses_to_overwrite(tmp_path: Path) -> None:
    assert cli.main(["gen-cert", "--out-dir", str(tmp_path), "--name", "host-a"]) == 0
    first = (tmp_path / "server.crt").read_bytes()
    assert (tmp_path / "server.key").exists()
    assert cli.main(["gen-cert", "--out-dir", str(tmp_path), "--name", "host-b"]) == 1
    assert (tmp_path / "server.crt").read_bytes() == first
    assert cli.main(["gen-cert", "--out-dir", str(tmp_path), "--name", "b", "--force"]) == 0
    assert (tmp_path / "server.crt").read_bytes() != first
