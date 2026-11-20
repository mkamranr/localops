"""ORM models. Import everything here so Alembic sees the full metadata."""

from app.models.ai import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    Agent,
    AgentExecution,
    Investigation,
    InvestigationEvent,
    ModelEndpoint,
)
from app.models.audit import AuditLog
from app.models.identity import (
    SYSTEM_TENANT_SLUG,
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)
from app.models.incidents import (
    OPEN_STATUSES,
    Incident,
    IncidentEvent,
    IncidentResource,
    ToolExecution,
    incident_key,
)
from app.models.inventory import (
    DiscoveryRun,
    Integration,
    Resource,
    ResourceEvent,
    ResourceMetricSample,
    ResourceRelationship,
    SshHostKey,
)
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeFeedback,
)
from app.models.remediation import (
    APPROVAL_STATUSES,
    RISK_LEVELS,
    Approval,
    ExecutionJob,
    Policy,
)
from app.models.tooling import ToolDefinition

__all__ = [
    "ACTIVE_STATUSES",
    "APPROVAL_STATUSES",
    "OPEN_STATUSES",
    "RISK_LEVELS",
    "SYSTEM_TENANT_SLUG",
    "TERMINAL_STATUSES",
    "Agent",
    "AgentExecution",
    "Approval",
    "AuditLog",
    "DiscoveryRun",
    "ExecutionJob",
    "Incident",
    "IncidentEvent",
    "IncidentResource",
    "Integration",
    "Investigation",
    "InvestigationEvent",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "KnowledgeDocumentVersion",
    "KnowledgeFeedback",
    "ModelEndpoint",
    "Permission",
    "Policy",
    "RefreshToken",
    "Resource",
    "ResourceEvent",
    "ResourceMetricSample",
    "ResourceRelationship",
    "Role",
    "RolePermission",
    "SshHostKey",
    "Tenant",
    "ToolDefinition",
    "ToolExecution",
    "User",
    "UserRole",
    "incident_key",
]
