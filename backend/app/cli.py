"""Operational CLI.

Usage::

    python -m app.cli bootstrap     # idempotent: system tenant, super admin, default tenant
"""

from __future__ import annotations

import argparse
import asyncio
import sys

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="localops")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="Create the system tenant and super administrator")
    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    if args.command == "bootstrap":
        return asyncio.run(_bootstrap())
    return 2


if __name__ == "__main__":
    sys.exit(main())
