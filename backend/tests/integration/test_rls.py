"""Row-level security verified directly in PostgreSQL as the runtime app role.

These tests bypass the application entirely: even buggy queries without tenant
filters must not be able to read or write another tenant's rows.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from app.database.session import get_engine

pytestmark = pytest.mark.usefixtures("bootstrapped")


async def _tenant_ids(conn: Any) -> list[uuid.UUID]:
    await conn.execute(text("SELECT set_config('app.platform_admin', 'on', true)"))
    rows = (await conn.execute(text("SELECT id FROM tenants ORDER BY created_at"))).scalars().all()
    await conn.execute(text("SELECT set_config('app.platform_admin', '', true)"))
    return list(rows)


async def test_app_role_has_no_rls_bypass() -> None:
    async with get_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert row == (False, False)


async def test_no_tenant_context_sees_nothing() -> None:
    async with get_engine().connect() as conn:
        for table in ("tenants", "users", "roles", "user_roles", "audit_logs", "refresh_tokens"):
            n = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608
            assert n == 0, table


async def test_tenant_context_sees_only_own_rows(make_tenant: Any) -> None:
    await make_tenant()
    async with get_engine().connect() as conn, conn.begin():
        tenants = await _tenant_ids(conn)
        assert len(tenants) >= 3
        for tid in tenants:
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)}
            )
            owners = (await conn.execute(text("SELECT DISTINCT tenant_id FROM users"))).scalars()
            assert set(owners) <= {tid}
            visible = (await conn.execute(text("SELECT id FROM tenants"))).scalars().all()
            assert visible == [tid]


async def test_cannot_write_into_another_tenant() -> None:
    async with get_engine().connect() as conn:
        async with conn.begin():
            a, b = (await _tenant_ids(conn))[:2]
        trans = await conn.begin()
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(a)})
        with pytest.raises(ProgrammingError, match="row-level security"):
            await conn.execute(
                text(
                    "INSERT INTO users (tenant_id, username, email, password_hash) "
                    "VALUES (:t, 'intruder', 'i@x.test', 'x')"
                ),
                {"t": str(b)},
            )
        await trans.rollback()

        # Updates to other tenants' rows silently match nothing.
        async with conn.begin():
            await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(a)})
            res = await conn.execute(
                text("UPDATE users SET display_name = 'pwned' WHERE tenant_id = :t"), {"t": str(b)}
            )
            assert res.rowcount == 0


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_logs SET action = 'tampered'",
        "DELETE FROM audit_logs",
        "DELETE FROM users",
        "DELETE FROM tenants",
        "INSERT INTO permissions (code) VALUES ('evil.permission')",
    ],
)
async def test_forbidden_statements(statement: str) -> None:
    async with get_engine().connect() as conn:
        async with conn.begin():
            tid = (await _tenant_ids(conn))[0]
        trans = await conn.begin()
        await conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)})
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text(statement))
        await trans.rollback()


async def test_tenant_context_does_not_leak_between_transactions() -> None:
    async with get_engine().connect() as conn:
        async with conn.begin():
            tid = (await _tenant_ids(conn))[0]
            await conn.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tid)}
            )
            assert (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one() >= 0
        async with conn.begin():
            current = (await conn.execute(text("SELECT app_current_tenant()"))).scalar_one()
            assert current is None
