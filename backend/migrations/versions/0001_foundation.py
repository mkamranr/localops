"""Platform foundation: tenants, identity, RBAC, audit log, row-level security.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen snapshot of the permission catalogue at this revision. Later revisions add or
# remove codes; tests/unit/test_permissions.py checks the union matches app.core.permissions.
PERMISSIONS_0001: dict[str, str] = {
    "tenant.manage": "Create, update and disable tenants (platform administrators only)",
    "user.read": "View users",
    "user.create": "Create users",
    "user.update": "Update users and reset their passwords",
    "user.delete": "Delete users",
    "role.read": "View roles and permissions",
    "role.assign": "Assign roles to users",
    "audit.read": "View audit logs",
    "dashboard.read": "View the operational dashboard",
    "settings.read": "View system settings",
    "settings.manage": "Change system settings",
    "resource.read": "View infrastructure resources",
    "resource.create": "Register infrastructure resources",
    "resource.update": "Update infrastructure resources",
    "resource.delete": "Delete infrastructure resources",
    "incident.read": "View incidents and investigations",
    "incident.create": "Create incidents",
    "incident.update": "Update incidents",
    "incident.investigate": "Start AI investigations",
    "tool.execute.readonly": "Execute read-only diagnostic tools",
    "tool.execute.remediation": "Execute remediation tools",
    "approval.read": "View approval requests",
    "approval.approve": "Approve or reject remediation actions",
    "agent.read": "View agents",
    "agent.configure": "Configure agents",
    "integration.read": "View integrations",
    "integration.configure": "Configure integrations and credentials",
    "knowledge.read": "Search and read the knowledge base",
    "knowledge.manage": "Upload and manage knowledge documents",
}

TENANT_TABLES = ("users", "roles", "role_permissions", "user_roles", "refresh_tokens")


def _app_role() -> str:
    role = os.environ.get("DB_APP_USER", "localops_app")
    if not role.replace("_", "").isalnum():
        raise ValueError(f"Unsafe DB_APP_USER value: {role!r}")
    return role


def _uuid_pk() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    app_role = _app_role()

    # ------------------------------------------------------------------ helper functions
    op.execute(
        """
        CREATE FUNCTION app_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION app_is_platform_admin() RETURNS boolean
        LANGUAGE sql STABLE AS
        $$ SELECT COALESCE(current_setting('app.platform_admin', true), '') = 'on' $$;
        """
    )

    # ------------------------------------------------------------------ tables
    op.create_table(
        "tenants",
        _uuid_pk(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("settings", pg.JSONB(), nullable=False, server_default="{}"),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_tenants_status"),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'", name="ck_tenants_slug_format"),
    )

    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("password_changed_at", sa.DateTime(timezone=True)),
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_users_tenant_id_tenants"
        ),
        sa.UniqueConstraint("tenant_id", "username", name="uq_users_tenant_id_username"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_users_id_tenant"),
        sa.CheckConstraint("status IN ('active','disabled','deleted')", name="ck_users_status"),
    )

    op.create_table(
        "permissions",
        _uuid_pk(),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )

    op.create_table(
        "roles",
        _uuid_pk(),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_roles_tenant_id_tenants"
        ),
        sa.UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_id_name"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_roles_id_tenant"),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("permission_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id", "tenant_id"],
            ["roles.id", "roles.tenant_id"],
            ondelete="CASCADE",
            name="fk_role_permissions_role_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            ondelete="CASCADE",
            name="fk_role_permissions_permission_id_permissions",
        ),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("role_id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            ondelete="CASCADE",
            name="fk_user_roles_user_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["role_id", "tenant_id"],
            ["roles.id", "roles.tenant_id"],
            ondelete="CASCADE",
            name="fk_user_roles_role_tenant",
        ),
    )

    op.create_table(
        "refresh_tokens",
        _uuid_pk(),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("replaced_by", pg.UUID(as_uuid=True)),
        sa.Column("user_agent", sa.String(500)),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            ondelete="CASCADE",
            name="fk_refresh_tokens_user_tenant",
        ),
    )
    op.create_index("ix_refresh_tokens_family", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_user", "refresh_tokens", ["user_id"])

    op.create_table(
        "audit_logs",
        _uuid_pk(),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True)),
        sa.Column("username", sa.String(100)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False, server_default="success"),
        sa.Column("entity_type", sa.String(50)),
        sa.Column("entity_id", pg.UUID(as_uuid=True)),
        sa.Column("details", pg.JSONB(), nullable=False, server_default="{}"),
        sa.Column("ip_address", pg.INET()),
        sa.Column("user_agent", sa.String(500)),
        sa.Column("request_id", sa.String(64)),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_audit_logs_tenant_id_tenants",
        ),
    )
    op.create_index("ix_audit_logs_tenant_created", "audit_logs", ["tenant_id", "created_at"])
    op.create_index("ix_audit_logs_tenant_action", "audit_logs", ["tenant_id", "action"])

    # ------------------------------------------------------------------ row-level security
    # The owner role is exempt (no FORCE), the runtime app role is not.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenants_select ON tenants FOR SELECT
        USING (id = app_current_tenant() OR app_is_platform_admin())
        """
    )
    op.execute(
        "CREATE POLICY tenants_insert ON tenants FOR INSERT WITH CHECK (app_is_platform_admin())"
    )
    op.execute(
        """
        CREATE POLICY tenants_update ON tenants FOR UPDATE
        USING (app_is_platform_admin()) WITH CHECK (app_is_platform_admin())
        """
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table} FOR ALL
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant())
            """
        )

    op.execute("ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY audit_logs_select ON audit_logs FOR SELECT "
        "USING (tenant_id = app_current_tenant())"
    )
    op.execute(
        "CREATE POLICY audit_logs_insert ON audit_logs FOR INSERT "
        "WITH CHECK (tenant_id = app_current_tenant())"
    )

    # ------------------------------------------------------------------ login helper
    # Resolving a tenant slug happens before any tenant context exists, so it runs as the
    # owner via SECURITY DEFINER and exposes only (id, status) for an exact slug match.
    op.execute(
        """
        CREATE FUNCTION auth_resolve_tenant(p_slug text)
        RETURNS TABLE (id uuid, status varchar)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$ SELECT t.id, t.status FROM public.tenants t WHERE t.slug = p_slug $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION auth_resolve_tenant(text) FROM PUBLIC")

    # ------------------------------------------------------------------ grants (least privilege)
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{app_role}"')
    op.execute(f'GRANT EXECUTE ON FUNCTION auth_resolve_tenant(text) TO "{app_role}"')
    grants = {
        "tenants": "SELECT, INSERT, UPDATE",
        "users": "SELECT, INSERT, UPDATE",  # users are soft-deleted
        "permissions": "SELECT",
        "roles": "SELECT, INSERT, UPDATE, DELETE",
        "role_permissions": "SELECT, INSERT, DELETE",
        "user_roles": "SELECT, INSERT, DELETE",
        "refresh_tokens": "SELECT, INSERT, UPDATE, DELETE",
        "audit_logs": "SELECT, INSERT",  # append-only
    }
    for table, privs in grants.items():
        op.execute(f'GRANT {privs} ON {table} TO "{app_role}"')

    # ------------------------------------------------------------------ seed permissions
    permissions = sa.table(
        "permissions", sa.column("code", sa.String), sa.column("description", sa.Text)
    )
    op.bulk_insert(
        permissions, [{"code": c, "description": d} for c, d in PERMISSIONS_0001.items()]
    )


def downgrade() -> None:
    for table in (
        "audit_logs",
        "refresh_tokens",
        "user_roles",
        "role_permissions",
        "roles",
        "permissions",
        "users",
        "tenants",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS auth_resolve_tenant(text)")
    op.execute("DROP FUNCTION IF EXISTS app_is_platform_admin()")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant()")
