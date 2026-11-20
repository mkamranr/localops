"""Knowledge base, tenant tool definitions (MCP / HTTP), embedding model endpoints.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-20 07:22:36.812895
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen: permission codes first seeded by this revision.
PERMISSIONS_0006: dict[str, str] = {
    "tool.manage": "Register, enable and classify MCP and custom tools",
}
# Built-in roles that receive the new codes in existing tenants.
ROLE_GRANTS_0006 = {
    "tool.manage": (
        "Super Administrator",
        "Tenant Administrator",
        "Infrastructure Administrator",
    ),
}
TENANT_TABLES = (
    "knowledge_documents",
    "knowledge_document_versions",
    "knowledge_chunks",
    "knowledge_feedback",
    "tool_definitions",
)
GRANTS = {
    "knowledge_documents": "SELECT, INSERT, UPDATE, DELETE",
    "knowledge_document_versions": "SELECT, INSERT, DELETE",
    "knowledge_chunks": "SELECT, INSERT, UPDATE, DELETE",
    "knowledge_feedback": "SELECT, INSERT, DELETE",
    "tool_definitions": "SELECT, INSERT, UPDATE, DELETE",
}
OLD_TYPES = "('linux_ssh', 'windows_winrm', 'docker', 'http')"
NEW_TYPES = "('linux_ssh', 'windows_winrm', 'docker', 'http', 'mcp', 'http_api')"


def _app_role() -> str:
    role = os.environ.get("DB_APP_USER", "localops_app")
    if not role.replace("_", "").isalnum():
        raise ValueError(f"Unsafe DB_APP_USER value: {role!r}")
    return role


def _security() -> None:
    app_role = _app_role()
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table} FOR ALL
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant())
            """
        )
        op.execute(f'GRANT {GRANTS[table]} ON {table} TO "{app_role}"')
    # Custom agents can be deleted (built-in ones are protected by the service).
    op.execute(f'GRANT DELETE ON agents TO "{app_role}"')
    # The knowledge sync finds work across tenants without tenant context. Exposes only
    # ids; the worker then indexes inside each tenant's session.
    op.execute(
        """
        CREATE FUNCTION worker_knowledge_backlog()
        RETURNS TABLE (tenant_id uuid, kind varchar, id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT i.tenant_id, 'incident'::varchar, i.id FROM public.incidents i
            LEFT JOIN public.knowledge_documents d
              ON d.tenant_id = i.tenant_id AND d.source = 'incident' AND d.source_id = i.id
            WHERE i.status IN ('resolved', 'closed')
              AND (d.id IS NULL
                   OR coalesce(i.updated_at, i.created_at) > coalesce(d.source_synced_at, d.created_at))
            UNION ALL
            SELECT v.tenant_id, 'investigation'::varchar, v.id FROM public.investigations v
            LEFT JOIN public.knowledge_documents d
              ON d.tenant_id = v.tenant_id AND d.source = 'investigation' AND d.source_id = v.id
            WHERE v.status = 'completed' AND v.result ? 'root_cause' AND d.id IS NULL
            UNION ALL
            SELECT d.tenant_id, 'document'::varchar, d.id FROM public.knowledge_documents d
            WHERE d.index_status = 'pending'
              AND coalesce(d.updated_at, d.created_at) < now() - interval '2 minutes'
            LIMIT 200
        $$;
        """
    )
    op.execute(
        """
        CREATE FUNCTION worker_tool_integrations()
        RETURNS TABLE (id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT g.id, g.tenant_id FROM public.integrations g
            WHERE g.enabled AND g.integration_type = 'mcp'
        $$;
        """
    )
    for fn in ("worker_knowledge_backlog()", "worker_tool_integrations()"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f'GRANT EXECUTE ON FUNCTION {fn} TO "{app_role}"')


def _seed_permissions() -> None:
    permissions = sa.table(
        "permissions", sa.column("code", sa.String), sa.column("description", sa.Text)
    )
    op.bulk_insert(
        permissions, [{"code": c, "description": d} for c, d in PERMISSIONS_0006.items()]
    )
    for code, roles in ROLE_GRANTS_0006.items():
        names = ", ".join(f"'{r}'" for r in roles)
        op.execute(
            f"""
            INSERT INTO role_permissions (role_id, permission_id, tenant_id)
            SELECT r.id, p.id, r.tenant_id FROM roles r, permissions p
            WHERE r.is_system AND r.name IN ({names}) AND p.code = '{code}'
            ON CONFLICT DO NOTHING
            """
        )


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "knowledge_documents",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), server_default="upload", nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column(
            "tags", postgresql.ARRAY(sa.String(length=60)), server_default="{}", nullable=False
        ),
        sa.Column("resource_ids", postgresql.ARRAY(sa.UUID()), server_default="{}", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("validated_by", sa.String(length=100), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("index_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("index_error", sa.String(length=1000), nullable=True),
        sa.Column("indexed_hash", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=200), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', coalesce(title, '')), 'A') || setweight(to_tsvector('english', coalesce(content, '')), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "document_type IN ('runbook', 'troubleshooting', 'documentation', 'procedure', 'incident', 'investigation', 'other')",
            name=op.f("ck_knowledge_documents_type"),
        ),
        sa.CheckConstraint(
            "index_status IN ('pending', 'indexed', 'keyword_only', 'failed')",
            name=op.f("ck_knowledge_documents_index_status"),
        ),
        sa.CheckConstraint(
            "source IN ('upload', 'manual', 'incident', 'investigation')",
            name=op.f("ck_knowledge_documents_source"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'draft', 'deprecated')",
            name=op.f("ck_knowledge_documents_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_knowledge_documents_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_knowledge_documents_id_tenant"),
    )
    op.create_index(
        "ix_knowledge_documents_resources",
        "knowledge_documents",
        ["resource_ids"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_documents_search",
        "knowledge_documents",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_knowledge_documents_tenant_type",
        "knowledge_documents",
        ["tenant_id", "document_type"],
        unique=False,
    )
    op.create_index(
        "uq_knowledge_documents_source",
        "knowledge_documents",
        ["tenant_id", "source", "source_id"],
        unique=True,
        postgresql_where="source_id IS NOT NULL",
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding_id", sa.String(length=100), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', coalesce(metadata ->> 'heading', '')), 'A') || setweight(to_tsvector('english', coalesce(content, '')), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_documents.id", "knowledge_documents.tenant_id"],
            name="fk_knowledge_chunks_document_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_knowledge_chunks_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunks")),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name=op.f("uq_knowledge_chunks_document_id_chunk_index")
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_search",
        "knowledge_chunks",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index("ix_knowledge_chunks_tenant", "knowledge_chunks", ["tenant_id"], unique=False)
    op.create_table(
        "knowledge_document_versions",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_documents.id", "knowledge_documents.tenant_id"],
            name="fk_knowledge_versions_document_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_knowledge_document_versions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_document_versions")),
        sa.UniqueConstraint(
            "document_id",
            "version",
            name=op.f("uq_knowledge_document_versions_document_id_version"),
        ),
    )
    op.create_table(
        "knowledge_feedback",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=True),
        sa.Column("investigation_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("username", sa.String(length=100), nullable=True),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "tenant_id"],
            ["knowledge_documents.id", "knowledge_documents.tenant_id"],
            name="fk_knowledge_feedback_document_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_knowledge_feedback_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_feedback")),
    )
    op.create_index(
        "ix_knowledge_feedback_document", "knowledge_feedback", ["document_id"], unique=False
    )
    op.create_table(
        "tool_definitions",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("integration_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("remote_name", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("remote_description", sa.Text(), nullable=True),
        sa.Column(
            "input_schema",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("http_template", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("kind", sa.String(length=10), server_default="read", nullable=False),
        sa.Column("risk_level", sa.String(length=10), server_default="low", nullable=False),
        sa.Column(
            "required_permission",
            sa.String(length=40),
            server_default="tool.execute.readonly",
            nullable=False,
        ),
        sa.Column("timeout_seconds", sa.Float(), server_default="30", nullable=False),
        sa.Column("verify_with", sa.String(length=60), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="new", nullable=False),
        sa.Column("schema_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_schema_hash", sa.String(length=64), nullable=True),
        sa.Column("approved_by", sa.String(length=100), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('read', 'change')", name=op.f("ck_tool_definitions_kind")),
        sa.CheckConstraint(
            "name ~ '^[a-z][a-z0-9_]{0,59}$'", name=op.f("ck_tool_definitions_name")
        ),
        sa.CheckConstraint(
            "required_permission IN ('tool.execute.readonly', 'tool.execute.remediation')",
            name=op.f("ck_tool_definitions_permission"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name=op.f("ck_tool_definitions_risk"),
        ),
        sa.CheckConstraint("source IN ('mcp', 'http')", name=op.f("ck_tool_definitions_source")),
        sa.CheckConstraint(
            "status IN ('new', 'active', 'schema_changed', 'missing')",
            name=op.f("ck_tool_definitions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["integration_id", "tenant_id"],
            ["integrations.id", "integrations.tenant_id"],
            name="fk_tool_definitions_integration_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tool_definitions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_definitions")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_tool_definitions_id_tenant"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_tool_definitions_tenant_name"),
    )
    op.create_index(
        "ix_tool_definitions_integration", "tool_definitions", ["integration_id"], unique=False
    )
    op.add_column(
        "model_endpoints",
        sa.Column("purpose", sa.String(length=20), server_default="chat", nullable=False),
    )
    op.drop_index(
        op.f("uq_model_endpoints_default"),
        table_name="model_endpoints",
        postgresql_where="is_default",
    )
    op.create_index(
        "uq_model_endpoints_default",
        "model_endpoints",
        ["tenant_id", "purpose"],
        unique=True,
        postgresql_where="is_default",
    )
    # ### end Alembic commands ###
    op.create_check_constraint("purpose", "model_endpoints", "purpose IN ('chat', 'embedding')")
    op.drop_constraint(op.f("ck_integrations_type"), "integrations", type_="check")
    op.create_check_constraint("type", "integrations", f"integration_type IN {NEW_TYPES}")
    _security()
    _seed_permissions()


def downgrade() -> None:
    op.execute("DELETE FROM agents WHERE agent_type LIKE 'custom!_%' ESCAPE '!'")
    op.execute(f'REVOKE DELETE ON agents FROM "{_app_role()}"')
    op.execute("DROP FUNCTION IF EXISTS worker_knowledge_backlog()")
    op.execute("DROP FUNCTION IF EXISTS worker_tool_integrations()")
    codes = ", ".join(f"'{c}'" for c in PERMISSIONS_0006)
    op.execute(
        f"DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({codes}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({codes})")
    op.execute("DELETE FROM integrations WHERE integration_type IN ('mcp', 'http_api')")
    op.drop_constraint(op.f("ck_integrations_type"), "integrations", type_="check")
    op.create_check_constraint("type", "integrations", f"integration_type IN {OLD_TYPES}")
    op.execute("DELETE FROM model_endpoints WHERE purpose <> 'chat'")
    op.drop_constraint(op.f("ck_model_endpoints_purpose"), "model_endpoints", type_="check")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(
        "uq_model_endpoints_default", table_name="model_endpoints", postgresql_where="is_default"
    )
    op.create_index(
        op.f("uq_model_endpoints_default"),
        "model_endpoints",
        ["tenant_id"],
        unique=True,
        postgresql_where="is_default",
    )
    op.drop_column("model_endpoints", "purpose")
    op.drop_index("ix_tool_definitions_integration", table_name="tool_definitions")
    op.drop_table("tool_definitions")
    op.drop_index("ix_knowledge_feedback_document", table_name="knowledge_feedback")
    op.drop_table("knowledge_feedback")
    op.drop_table("knowledge_document_versions")
    op.drop_index("ix_knowledge_chunks_tenant", table_name="knowledge_chunks")
    op.drop_index(
        "ix_knowledge_chunks_search", table_name="knowledge_chunks", postgresql_using="gin"
    )
    op.drop_table("knowledge_chunks")
    op.drop_index(
        "uq_knowledge_documents_source",
        table_name="knowledge_documents",
        postgresql_where="source_id IS NOT NULL",
    )
    op.drop_index("ix_knowledge_documents_tenant_type", table_name="knowledge_documents")
    op.drop_index(
        "ix_knowledge_documents_search", table_name="knowledge_documents", postgresql_using="gin"
    )
    op.drop_index(
        "ix_knowledge_documents_resources", table_name="knowledge_documents", postgresql_using="gin"
    )
    op.drop_table("knowledge_documents")
    # ### end Alembic commands ###
