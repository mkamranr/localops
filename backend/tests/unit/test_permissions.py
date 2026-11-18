import importlib.util
from pathlib import Path
from typing import Any

from app.core.permissions import (
    ALL,
    PERMISSIONS,
    SUPER_ADMIN,
    SYSTEM_ROLES,
    TENANT_ADMIN,
    roles_for_tenant,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _load_migration(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_seed_matches_catalogue() -> None:
    """New permission codes must ship with a migration that seeds them."""
    seeded = set(_load_migration("0001_foundation").PERMISSIONS_0001)
    assert seeded == set(PERMISSIONS)


def test_role_permissions_are_known() -> None:
    for name, (_, perms) in SYSTEM_ROLES.items():
        assert perms <= ALL, name


def test_seven_spec_roles_exist() -> None:
    assert set(SYSTEM_ROLES) == {
        "Super Administrator",
        "Tenant Administrator",
        "Infrastructure Administrator",
        "Operator",
        "Investigator",
        "Auditor",
        "Read Only",
    }


def test_only_super_admin_manages_tenants() -> None:
    holders = {n for n, (_, p) in SYSTEM_ROLES.items() if "tenant.manage" in p}
    assert holders == {SUPER_ADMIN}


def test_regular_tenants_have_no_platform_roles() -> None:
    roles = roles_for_tenant(is_system_tenant=False)
    assert SUPER_ADMIN not in roles
    assert TENANT_ADMIN in roles
    assert SUPER_ADMIN in roles_for_tenant(is_system_tenant=True)


def test_read_only_roles_cannot_mutate() -> None:
    for name in ("Read Only", "Auditor"):
        perms = SYSTEM_ROLES[name][1]
        assert all(p.endswith(".read") for p in perms), name
