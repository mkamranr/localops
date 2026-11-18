"""AI foundation: model endpoints, agents, investigations, events, LangGraph checkpoints.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18 12:55:07.527648
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TENANT_TABLES = (
    "model_endpoints",
    "agents",
    "agent_executions",
    "investigations",
    "investigation_events",
)
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")

# Frozen copy of langgraph-checkpoint-postgres BasePostgresSaver.MIGRATIONS[0:10]
# (langgraph-checkpoint-postgres 3.2.x). CONCURRENTLY is dropped because Alembic runs
# inside a transaction and the tables are empty here. Library upgrades that add
# checkpoint migrations need a new Alembic revision; the worker refuses to start if
# the applied version is behind the library (app/graphs/checkpointer.py).
CHECKPOINT_MIGRATIONS = [
    "CREATE TABLE IF NOT EXISTS checkpoint_migrations (v INTEGER PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS checkpoints (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        parent_checkpoint_id TEXT,
        type TEXT,
        checkpoint JSONB NOT NULL,
        metadata JSONB NOT NULL DEFAULT '{}',
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoint_blobs (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL,
        version TEXT NOT NULL,
        type TEXT NOT NULL,
        blob BYTEA,
        PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
    )""",
    """CREATE TABLE IF NOT EXISTS checkpoint_writes (
        thread_id TEXT NOT NULL,
        checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        idx INTEGER NOT NULL,
        channel TEXT NOT NULL,
        type TEXT,
        blob BYTEA NOT NULL,
        PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
    )""",
    "ALTER TABLE checkpoint_blobs ALTER COLUMN blob DROP not null",
    "SELECT 1",
    "CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints(thread_id)",
    "CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs(thread_id)",
    "CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes(thread_id)",
    "ALTER TABLE checkpoint_writes ADD COLUMN IF NOT EXISTS task_path TEXT NOT NULL DEFAULT ''",
]


def _app_role() -> str:
    role = os.environ.get("DB_APP_USER", "localops_app")
    if not role.replace("_", "").isalnum():
        raise ValueError(f"Unsafe DB_APP_USER value: {role!r}")
    return role


def _security_and_checkpoints() -> None:
    app_role = _app_role()

    # ---- RLS on tenant-owned tables
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table} FOR ALL
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant())
            """
        )
    grants = {
        "model_endpoints": "SELECT, INSERT, UPDATE, DELETE",
        "agents": "SELECT, INSERT, UPDATE",
        "agent_executions": "SELECT, INSERT, UPDATE",
        "investigations": "SELECT, INSERT, UPDATE",
        "investigation_events": "SELECT, INSERT",  # append-only event history
    }
    for table, privs in grants.items():
        op.execute(f'GRANT {privs} ON {table} TO "{app_role}"')

    # ---- LangGraph checkpoint tables, isolated by tenant via the thread id
    # Thread ids are "<tenant_id>:<investigation_id>"; the policy only exposes threads
    # whose prefix matches the session's tenant context.
    for statement in CHECKPOINT_MIGRATIONS:
        op.execute(statement)
    op.execute(
        "INSERT INTO checkpoint_migrations (v) SELECT generate_series(0, "
        f"{len(CHECKPOINT_MIGRATIONS) - 1}) ON CONFLICT DO NOTHING"
    )
    for table in CHECKPOINT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_tenant_isolation ON {table} FOR ALL
            USING (split_part(thread_id, ':', 1) = app_current_tenant()::text)
            WITH CHECK (split_part(thread_id, ':', 1) = app_current_tenant()::text)
            """
        )
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{app_role}"')
    op.execute(f'GRANT SELECT ON checkpoint_migrations TO "{app_role}"')

    # ---- Worker crash recovery needs to find unfinished investigations across tenants
    # without any tenant context. Exposes only ids, tenant ids and status.
    op.execute(
        """
        CREATE FUNCTION worker_recoverable_investigations()
        RETURNS TABLE (id uuid, tenant_id uuid, status varchar)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT i.id, i.tenant_id, i.status FROM public.investigations i
            WHERE i.status IN ('queued', 'running')
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION worker_recoverable_investigations() FROM PUBLIC")
    op.execute(f'GRANT EXECUTE ON FUNCTION worker_recoverable_investigations() TO "{app_role}"')


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "agent_executions",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=True),
        sa.Column("agent_type", sa.String(length=50), nullable=False),
        sa.Column("node", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("model_endpoint_id", sa.UUID(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("llm_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_agent_executions_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_executions")),
    )
    op.create_index(
        "ix_agent_executions_agent",
        "agent_executions",
        ["tenant_id", "agent_type", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_executions_investigation", "agent_executions", ["investigation_id"], unique=False
    )
    op.create_index(
        "ix_agent_executions_tenant_started",
        "agent_executions",
        ["tenant_id", "started_at"],
        unique=False,
    )
    op.create_table(
        "model_endpoints",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=20), nullable=True),
        sa.Column("last_latency_ms", sa.Float(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('vllm', 'ollama', 'llamacpp', 'openai_compatible')",
            name=op.f("ck_model_endpoints_provider"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_model_endpoints_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_endpoints")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_model_endpoints_id_tenant"),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_model_endpoints_tenant_id_name")),
    )
    op.create_index(
        "uq_model_endpoints_default",
        "model_endpoints",
        ["tenant_id"],
        unique=True,
        postgresql_where="is_default",
    )
    op.create_table(
        "agents",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("model_id", sa.UUID(), nullable=True),
        sa.Column(
            "configuration",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["model_id", "tenant_id"],
            ["model_endpoints.id", "model_endpoints.tenant_id"],
            name="fk_agents_model_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_agents_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agents")),
        sa.UniqueConstraint("tenant_id", "agent_type", name=op.f("uq_agents_tenant_id_agent_type")),
    )
    op.create_table(
        "investigations",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("incident_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column(
            "execution_mode", sa.String(length=20), server_default="assisted", nullable=False
        ),
        sa.Column("investigation_type", sa.String(length=50), nullable=True),
        sa.Column("request_type", sa.String(length=50), nullable=True),
        sa.Column(
            "resource_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "result", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("pending_question", sa.Text(), nullable=True),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "execution_mode IN ('manual','assisted','autonomous')",
            name=op.f("ck_investigations_mode"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_input', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_investigations_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_investigations_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "tenant_id"],
            ["users.id", "users.tenant_id"],
            name="fk_investigations_user_tenant",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigations")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_investigations_id_tenant"),
    )
    op.create_index(
        "ix_investigations_tenant_started",
        "investigations",
        ["tenant_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_investigations_tenant_status", "investigations", ["tenant_id", "status"], unique=False
    )
    op.create_table(
        "investigation_events",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("agent_name", sa.String(length=50), nullable=True),
        sa.Column(
            "event_data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id", "tenant_id"],
            ["investigations.id", "investigations.tenant_id"],
            name="fk_investigation_events_inv_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_investigation_events_tenant_id_tenants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigation_events")),
        sa.UniqueConstraint(
            "investigation_id", "seq", name=op.f("uq_investigation_events_investigation_id_seq")
        ),
    )
    # ### end Alembic commands ###
    _security_and_checkpoints()


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS worker_recoverable_investigations()")
    for table in (*CHECKPOINT_TABLES, "checkpoint_migrations"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_table("investigation_events")
    op.drop_index("ix_investigations_tenant_status", table_name="investigations")
    op.drop_index("ix_investigations_tenant_started", table_name="investigations")
    op.drop_table("investigations")
    op.drop_table("agents")
    op.drop_index(
        "uq_model_endpoints_default", table_name="model_endpoints", postgresql_where="is_default"
    )
    op.drop_table("model_endpoints")
    op.drop_index("ix_agent_executions_tenant_started", table_name="agent_executions")
    op.drop_index("ix_agent_executions_investigation", table_name="agent_executions")
    op.drop_index("ix_agent_executions_agent", table_name="agent_executions")
    op.drop_table("agent_executions")
    # ### end Alembic commands ###
