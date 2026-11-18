"""Permission catalogue and built-in role definitions.

Permissions are global codes seeded by migration. Roles are tenant-scoped and seeded
per tenant from ``SYSTEM_ROLES`` when a tenant is created.
"""

from __future__ import annotations

from typing import Final

PERMISSIONS: Final[dict[str, str]] = {
    # Platform
    "tenant.manage": "Create, update and disable tenants (platform administrators only)",
    # Identity
    "user.read": "View users",
    "user.create": "Create users",
    "user.update": "Update users and reset their passwords",
    "user.delete": "Delete users",
    "role.read": "View roles and permissions",
    "role.assign": "Assign roles to users",
    # Audit / dashboard / settings
    "audit.read": "View audit logs",
    "dashboard.read": "View the operational dashboard",
    "settings.read": "View system settings",
    "settings.manage": "Change system settings",
    # Infrastructure (used from Phase 3)
    "resource.read": "View infrastructure resources",
    "resource.create": "Register infrastructure resources",
    "resource.update": "Update infrastructure resources",
    "resource.delete": "Delete infrastructure resources",
    # Incidents & investigations (Phase 2/4)
    "incident.read": "View incidents and investigations",
    "incident.create": "Create incidents",
    "incident.update": "Update incidents",
    "incident.investigate": "Start AI investigations",
    # Tools & approvals (Phase 5)
    "tool.execute.readonly": "Execute read-only diagnostic tools",
    "tool.execute.remediation": "Execute remediation tools",
    "approval.read": "View approval requests",
    "approval.approve": "Approve or reject remediation actions",
    # Agents, integrations, knowledge (Phase 2/6)
    "agent.read": "View agents",
    "agent.configure": "Configure agents",
    "integration.read": "View integrations",
    "integration.configure": "Configure integrations and credentials",
    "knowledge.read": "Search and read the knowledge base",
    "knowledge.manage": "Upload and manage knowledge documents",
}

ALL: Final[frozenset[str]] = frozenset(PERMISSIONS)
_READ: Final[frozenset[str]] = frozenset(p for p in PERMISSIONS if p.endswith(".read"))

SUPER_ADMIN = "Super Administrator"
TENANT_ADMIN = "Tenant Administrator"

SYSTEM_ROLES: Final[dict[str, tuple[str, frozenset[str]]]] = {
    SUPER_ADMIN: (
        "Full platform access including tenant management",
        ALL,
    ),
    TENANT_ADMIN: (
        "Full access within the tenant",
        ALL - {"tenant.manage"},
    ),
    "Infrastructure Administrator": (
        "Manage infrastructure, integrations and remediation",
        frozenset(
            {
                "dashboard.read",
                "user.read",
                "role.read",
                "settings.read",
                "resource.read",
                "resource.create",
                "resource.update",
                "resource.delete",
                "incident.read",
                "incident.create",
                "incident.update",
                "incident.investigate",
                "tool.execute.readonly",
                "tool.execute.remediation",
                "approval.read",
                "approval.approve",
                "agent.read",
                "integration.read",
                "integration.configure",
                "knowledge.read",
                "knowledge.manage",
            }
        ),
    ),
    "Operator": (
        "Operate infrastructure and handle incidents",
        frozenset(
            {
                "dashboard.read",
                "resource.read",
                "resource.update",
                "incident.read",
                "incident.create",
                "incident.update",
                "incident.investigate",
                "tool.execute.readonly",
                "tool.execute.remediation",
                "approval.read",
                "agent.read",
                "integration.read",
                "knowledge.read",
            }
        ),
    ),
    "Investigator": (
        "Investigate incidents using read-only diagnostics",
        frozenset(
            {
                "dashboard.read",
                "resource.read",
                "incident.read",
                "incident.create",
                "incident.investigate",
                "tool.execute.readonly",
                "approval.read",
                "agent.read",
                "knowledge.read",
            }
        ),
    ),
    "Auditor": (
        "Read-only access including audit logs",
        _READ | {"dashboard.read"},
    ),
    "Read Only": (
        "Read-only access to operational data",
        frozenset(
            {
                "dashboard.read",
                "resource.read",
                "incident.read",
                "approval.read",
                "agent.read",
                "integration.read",
                "knowledge.read",
            }
        ),
    ),
}

# Regular tenants never receive platform-level roles.
PLATFORM_ONLY_ROLES: Final[frozenset[str]] = frozenset({SUPER_ADMIN})


def roles_for_tenant(*, is_system_tenant: bool) -> dict[str, tuple[str, frozenset[str]]]:
    if is_system_tenant:
        return dict(SYSTEM_ROLES)
    return {k: v for k, v in SYSTEM_ROLES.items() if k not in PLATFORM_ONLY_ROLES}
