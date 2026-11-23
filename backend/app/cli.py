"""Operational CLI.

Usage::

    python -m app.cli bootstrap        # idempotent: system tenant, super admin, default tenant
    python -m app.cli gen-cert --out-dir /tls --name localops.example.com --name 10.0.0.5
    python -m app.cli seal-keys --passphrase-file F < keys.env > keys.enc
    python -m app.cli unseal-keys --passphrase-file F < keys.enc > keys.env
    python -m app.cli qdrant-snapshot --out /backup/qdrant
    python -m app.cli qdrant-restore --in /backup/qdrant
    python -m app.cli check-secrets    # stored credentials decrypt with the configured key
    python -m app.cli export-tenant <slug> [--out file.json]
    python -m app.cli reset-password [--tenant system] [--username admin]

gen-cert, seal-keys and unseal-keys need no configuration (they run with --network none
during installation and backup); the others read the normal LocalOps settings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.database.session import dispose_engine, init_engine, tenant_session
from app.schemas.identity import PASSWORD_MIN_LENGTH
from app.services.tenant_service import ensure_platform_bootstrap


async def _bootstrap() -> int:
    settings = get_settings()
    log = get_logger("localops.cli")
    password = (
        settings.bootstrap_admin_password.get_secret_value()
        if settings.bootstrap_admin_password
        else ""
    )
    if not password or "CHANGE_ME" in password or len(password) < PASSWORD_MIN_LENGTH:
        log.error(
            "bootstrap_failed",
            reason=f"BOOTSTRAP_ADMIN_PASSWORD must be set (min {PASSWORD_MIN_LENGTH} chars)",
        )
        return 1
    init_engine(settings)
    try:
        async with tenant_session(None) as session:
            notes = await ensure_platform_bootstrap(
                session,
                admin_username=settings.bootstrap_admin_username.strip().lower(),
                admin_email=settings.bootstrap_admin_email.strip().lower(),
                admin_password=password,
                create_default_tenant=settings.bootstrap_create_default_tenant,
            )
    finally:
        await dispose_engine()
    for note in notes or ["nothing to do; platform already bootstrapped"]:
        log.info("bootstrap", note=note)
    return 0


def _err(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _passphrase(path: str | None) -> str:
    """From --passphrase-file, else LOCALOPS_KEYS_PASSPHRASE (stdin carries the data)."""
    if path:
        return Path(path).read_text(encoding="utf-8").rstrip("\r\n")
    value = os.environ.get("LOCALOPS_KEYS_PASSPHRASE", "")
    if not value:
        raise ValueError("give --passphrase-file or set LOCALOPS_KEYS_PASSPHRASE")
    return value


def _gen_cert(out_dir: str, names: list[str], days: int, force: bool) -> int:
    from app.ops.certs import generate_self_signed

    target = Path(out_dir)
    crt, key = target / "server.crt", target / "server.key"
    if (crt.exists() or key.exists()) and not force:
        return _err(f"{crt} or {key} already exists (use --force to replace)")
    cert_pem, key_pem = generate_self_signed(names, days=days)
    target.mkdir(parents=True, exist_ok=True)
    crt.write_bytes(cert_pem)
    # Must be readable by the unprivileged nginx user through the bind mount; access is
    # restricted on the host by the installation directory's permissions (mode 700).
    key.write_bytes(key_pem)
    os.chmod(key, 0o644)
    print(f"wrote {crt} and {key} for: {', '.join(names) or 'localhost'}", file=sys.stderr)
    return 0


def _seal(command: str, passphrase_file: str | None) -> int:
    from app.ops.keyseal import KeySealError, seal, unseal

    try:
        passphrase = _passphrase(passphrase_file)
        data = sys.stdin.buffer.read()
        out = seal(data, passphrase) if command == "seal-keys" else unseal(data, passphrase)
    except (KeySealError, ValueError, OSError) as exc:
        return _err(str(exc))
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return 0


async def _with_db[T](factory: Callable[[], Awaitable[T]]) -> T:
    init_engine(get_settings())
    try:
        return await factory()
    finally:
        await dispose_engine()


def _check_secrets() -> int:
    from app.ops.data import check_secrets

    settings = get_settings()
    report = asyncio.run(_with_db(lambda: check_secrets(settings)))
    if report.ok:
        print(f"ok: {report.checked} stored secret(s) decrypt with the configured key")
        return 0
    print(
        f"FAILED: {len(report.failed)} of {report.checked} stored secret(s) cannot be "
        "decrypted with LOCALOPS_ENCRYPTION_KEY (restore the matching keys file):",
        file=sys.stderr,
    )
    for item in report.failed:
        print(f"  - {item}", file=sys.stderr)
    return 1


def _export_tenant(slug: str, out: str | None) -> int:
    from app.ops.data import export_tenant

    try:
        data = asyncio.run(_with_db(lambda: export_tenant(slug)))
    except LookupError as exc:
        return _err(str(exc))
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def _reset_password(tenant: str, username: str) -> int:
    """New password from the terminal (asked twice) or, without a TTY, generated and printed."""
    import getpass

    from app.ops.data import reset_password

    if sys.stdin.isatty():
        password = getpass.getpass(f"New password for {username} in tenant {tenant}: ")
        if password != getpass.getpass("Repeat it: "):
            return _err("passwords do not match")
        generated = False
    else:
        password = secrets.token_urlsafe(18)
        generated = True
    if len(password) < PASSWORD_MIN_LENGTH:
        return _err(f"the password must be at least {PASSWORD_MIN_LENGTH} characters")
    try:
        status = asyncio.run(_with_db(lambda: reset_password(tenant, username, password)))
    except LookupError as exc:
        return _err(str(exc))
    print(f"Password reset for {username} in tenant {tenant}; it must be changed at next sign-in.")
    if generated:
        print(f"Temporary password: {password}")
    if status != "active":
        print(
            f"warning: the account is {status}; an administrator must re-enable it", file=sys.stderr
        )
    return 0


def _qdrant(command: str, directory: str) -> int:
    from app.ops import qdrant_backup

    url = get_settings().qdrant_url
    if command == "qdrant-snapshot":
        files = qdrant_backup.snapshot(url, Path(directory))
        print(f"snapshotted {len(files)} collection(s) into {directory}", file=sys.stderr)
    else:
        names = qdrant_backup.restore(url, Path(directory))
        print(f"restored {len(names)} collection(s): {', '.join(names)}", file=sys.stderr)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="localops")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="Create the system tenant and super administrator")
    cert = sub.add_parser("gen-cert", help="Write a self-signed TLS certificate and key")
    cert.add_argument("--out-dir", required=True)
    cert.add_argument("--name", action="append", default=[], help="DNS name or IP (repeat)")
    cert.add_argument("--days", type=int, default=825)
    cert.add_argument("--force", action="store_true")
    for name, help_text in (
        ("seal-keys", "Encrypt key material (stdin) with a passphrase (stdout)"),
        ("unseal-keys", "Decrypt a sealed key file (stdin) to stdout"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--passphrase-file")
    snap = sub.add_parser("qdrant-snapshot", help="Snapshot every Qdrant collection")
    snap.add_argument("--out", required=True)
    rest = sub.add_parser("qdrant-restore", help="Restore Qdrant collection snapshots")
    rest.add_argument("--in", dest="in_dir", required=True)
    sub.add_parser("check-secrets", help="Verify stored credentials decrypt")
    exp = sub.add_parser("export-tenant", help="Export one tenant's data as JSON")
    exp.add_argument("slug")
    exp.add_argument("--out")
    reset = sub.add_parser("reset-password", help="Reset a user's password (account recovery)")
    reset.add_argument("--tenant", default="system")
    reset.add_argument("--username", default="admin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Configuration-free commands first: they run before any .env exists.
    if args.command == "gen-cert":
        return _gen_cert(args.out_dir, args.name, args.days, args.force)
    if args.command in ("seal-keys", "unseal-keys"):
        return _seal(args.command, args.passphrase_file)
    configure_logging(get_settings().log_level)
    if args.command == "bootstrap":
        return asyncio.run(_bootstrap())
    if args.command == "check-secrets":
        return _check_secrets()
    if args.command == "export-tenant":
        return _export_tenant(args.slug, args.out)
    if args.command == "reset-password":
        return _reset_password(args.tenant, args.username)
    if args.command == "qdrant-snapshot":
        return _qdrant(args.command, args.out)
    if args.command == "qdrant-restore":
        return _qdrant(args.command, args.in_dir)
    return 2


if __name__ == "__main__":
    sys.exit(main())
