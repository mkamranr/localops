"""Self-signed TLS certificate for the nginx edge (default until a real one is supplied)."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_DAYS = 825  # the maximum browsers accept for server certificates


def _san(value: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def generate_self_signed(names: list[str], *, days: int = VALID_DAYS) -> tuple[bytes, bytes]:
    """Return ``(certificate_pem, private_key_pem)`` valid for ``names`` (DNS names or IPs)."""
    names = list(dict.fromkeys(n.strip() for n in names if n.strip())) or ["localhost"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64]),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "LocalOps (self-signed)"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([_san(n) for n in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem
