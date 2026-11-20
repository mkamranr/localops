// Types mirroring backend/app/schemas.

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: unknown; request_id?: string | null };
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
}

export interface TenantBrief {
  id: string;
  name: string;
  slug: string;
}

export interface Me {
  id: string;
  username: string;
  email: string;
  display_name: string;
  must_change_password: boolean;
  tenant: TenantBrief;
  roles: string[];
  permissions: string[];
}

export interface Tenant extends TenantBrief {
  status: "active" | "disabled";
  created_at: string;
  updated_at: string | null;
}

export interface RoleBrief {
  id: string;
  name: string;
}

export interface User {
  id: string;
  username: string;
  email: string;
  display_name: string;
  status: "active" | "disabled" | "deleted";
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string;
  roles: RoleBrief[];
}

export interface Role {
  id: string;
  name: string;
  description: string;
  is_system: boolean;
  permissions: string[];
  user_count: number;
}

export interface Permission {
  code: string;
  description: string;
}

export interface AuditLog {
  id: string;
  user_id: string | null;
  username: string | null;
  action: string;
  outcome: "success" | "failure";
  entity_type: string | null;
  entity_id: string | null;
  details: Record<string, unknown>;
  ip_address: string | null;
  request_id: string | null;
  created_at: string;
}

export interface ServiceHealth {
  name: string;
  status: "ok" | "error";
  latency_ms: number | null;
  detail: string | null;
}

export interface DashboardSummary {
  tenant: string;
  users: { total: number; active: number; disabled: number };
  activity: { events_24h: number; logins_24h: number; failed_logins_24h: number };
  services: ServiceHealth[];
  resources: { total: number; online: number; offline: number };
  incidents: { critical: number; high: number; medium: number; low: number; open_total: number };
  active_investigations: number;
  investigations_24h: number;
  agent_executions_24h: number;
  agent_failures_24h: number;
  default_model: string | null;
  pending_approvals: number;
  knowledge_documents: number;
}

// ------------------------------------------------------------------ AI (Phase 2)

export type Provider = "vllm" | "ollama" | "llamacpp" | "openai_compatible";

export interface ModelSettings {
  timeout_seconds?: number;
  max_retries?: number;
  structured_output?: "json_schema" | "json_object" | "none";
  context_window?: number | null;
  thinking?: "auto" | "enabled" | "disabled";
  tool_calling?: "auto" | "native" | "json";
}

export type ModelPurpose = "chat" | "embedding";

export interface ModelEndpoint {
  id: string;
  name: string;
  provider: Provider;
  purpose: ModelPurpose;
  base_url: string;
  model_name: string;
  description: string;
  configuration: ModelSettings;
  has_api_key: boolean;
  enabled: boolean;
  is_default: boolean;
  last_checked_at: string | null;
  last_status: "healthy" | "degraded" | "unhealthy" | null;
  last_latency_ms: number | null;
  last_error: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface ModelTestResult {
  ok: boolean;
  latency_ms: number | null;
  dimensions?: number | null;
  models: string[];
  model_available: boolean | null;
  reply: string | null;
  error: string | null;
}

export interface AgentConfig {
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  max_retries: number;
  allowed_tools: string[];
  max_steps: number;
  instructions: string;
}

export interface AgentStats {
  executions_24h: number;
  failures_24h: number;
  avg_duration_ms: number | null;
  tokens_24h: number;
  last_run_at: string | null;
  executions_total: number;
  success_rate: number | null;
}

export interface Agent {
  id: string;
  agent_type: string;
  name: string;
  description: string;
  enabled: boolean;
  model_id: string | null;
  model_name: string | null;
  configuration: AgentConfig;
  capabilities: string[];
  available_in_phase: number;
  implemented: boolean;
  specialist: boolean;
  custom: boolean;
  uses_tools: boolean;
  stats: AgentStats;
  updated_at: string | null;
}

export interface AgentExecution {
  id: string;
  investigation_id: string | null;
  agent_type: string;
  node: string;
  status: "running" | "success" | "failed" | "interrupted";
  model_name: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  llm_calls: number;
  error: string | null;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
}

export type InvestigationStatus =
  | "queued"
  | "running"
  | "awaiting_input"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type ExecutionMode = "manual" | "assisted" | "autonomous";

export interface Investigation {
  id: string;
  title: string;
  request: string;
  status: InvestigationStatus;
  execution_mode: ExecutionMode;
  request_type: string | null;
  workflow_id: string;
  user_id: string;
  incident_id: string | null;
  investigation_type: string | null;
  pending_question: string | null;
  error: string | null;
  cancel_requested: boolean;
  attempts: number;
  started_at: string;
  completed_at: string | null;
  updated_at: string | null;
  report?: ReportBrief | null;
}

export interface ReportBrief {
  conclusion: string | null;
  root_cause: string | null;
  root_cause_status: RootCause["status"] | null;
  confidence: number | null;
}

export interface ReportTask {
  id: string;
  agent: string;
  objective: string;
  rationale: string;
  status: string;
  reason: string | null;
  summary?: string | null;
  notes?: string[];
}

export type FindingKind = "confirmed_fact" | "observation" | "hypothesis";
export type Likelihood = "high" | "medium" | "low";

export interface Finding {
  agent: string;
  kind: FindingKind;
  statement: string;
  evidence_ids: string[];
  likelihood?: Likelihood;
}

export interface RootCause {
  statement: string;
  status: "confirmed" | "probable" | "unverified";
  confidence: number;
  evidence_ids: string[];
}

export interface Remediation {
  action: string;
  rationale: string;
  risk: "low" | "medium" | "high";
  evidence_ids: string[];
}

export interface ChangeImpact {
  resource_id: string;
  resource: string;
  environment: string | null;
  owner: string | null;
  direct: string[];
  indirect: string[];
  production_dependents: string[];
  unhealthy_dependents: string[];
  active_incidents: string[];
  previous_incidents: number;
  risk_level: string;
  potential_disruption: string;
  precautions: string[];
  required_approvals: string[];
}

export interface SimilarItem {
  kind: "incident" | "investigation";
  id: string;
  number?: string | null;
  title: string;
  status: string;
  severity?: string | null;
  root_cause?: string | null;
  resolution?: string | null;
  conclusion?: string | null;
  created_at: string;
  score: number;
  match?: KnowledgeMatch | null;
}

export interface InvestigationReport {
  workflow: string;
  summary: string;
  request_type: string | null;
  urgency: string | null;
  classification_confidence: number | null;
  classification_verified: boolean;
  affected_entities: string[];
  symptoms: string[];
  resources: {
    name: string;
    entity?: string;
    resolved: boolean;
    resource_id: string | null;
    match?: "exact" | "partial";
    resource_type?: string;
    health?: string;
    reason?: "ambiguous" | "not_in_inventory";
    candidates?: { name: string; resource_id: string; resource_type: string }[];
  }[];
  clarifications: { question: string; answer: string }[];
  tasks: ReportTask[];
  evidence: EvidenceItem[];
  evidence_sufficient: boolean;
  root_cause: RootCause | null;
  conclusion: string;
  recommended_next_steps: string[];
  // Phase 4 RCA sections (spec §6.6)
  incident_summary: string;
  timeline: { at: string | null; agent: string; evidence_id: string; summary: string }[];
  findings: Finding[];
  recent_changes: { evidence_id: string; resource: string | null; summary: string }[];
  dependency_analysis: { resource: string; depends_on: string[]; dependents: number; evidence_id: string }[];
  change_impact: { impacts: ChangeImpact[]; risk_level: string } | null;
  contributing_factors: { statement: string; evidence_ids: string[] }[];
  alternative_hypotheses: { statement: string; likelihood: Likelihood; evidence_ids: string[] }[];
  confidence: number | null;
  confidence_label: string;
  impact: string;
  recommended_remediation: Remediation[];
  preventive_recommendations: string[];
  evidence_gaps: string[];
  triage: { severity: string; priority: string; reasons: string[]; investigation_paths: string[] } | null;
  similar_incidents: SimilarItem[];
  similar_investigations: SimilarItem[];
  remediation?: RemediationSection | null;
}

export interface InvestigationDetail extends Investigation {
  result: Partial<InvestigationReport>;
  username: string | null;
}

export interface InvestigationEvent {
  seq: number;
  event_type: string;
  agent_name: string | null;
  event_data: Record<string, unknown>;
  created_at: string;
}

// ------------------------------------------------------------------ Inventory (Phase 3)

export type IntegrationType = "linux_ssh" | "windows_winrm" | "docker" | "http" | "mcp" | "http_api";
export const TOOL_INTEGRATION_TYPES: IntegrationType[] = ["mcp", "http_api"];
export type HealthStatus = "healthy" | "degraded" | "unhealthy" | "unknown";
export type ResourceType =
  | "linux_server"
  | "windows_server"
  | "docker_host"
  | "docker_container"
  | "http_service"
  | "application"
  | "database"
  | "network_device"
  | "other";
export type RelationshipType =
  | "HOSTS"
  | "DEPENDS_ON"
  | "CONNECTS_TO"
  | "RUNS_ON"
  | "STORES_DATA_IN"
  | "EXPOSES"
  | "MONITORED_BY"
  | "MANAGED_BY";

export interface Target {
  host: string;
  port?: number | null;
  name?: string | null;
}

export interface IntegrationSettings {
  username?: string | null;
  port?: number | null;
  timeout_seconds?: number;
  docker?: "auto" | "off";
  mode?: "ssh" | "tls";
  verify_hostname?: boolean;
  use_https?: boolean;
  transport?: "ntlm" | "basic";
  verify_tls?: boolean;
  expected_status?: number[];
  slow_ms?: number;
  // mcp / http_api
  url?: string | null;
  mcp_transport?: "auto" | "streamable_http" | "sse";
  auth_type?: "none" | "bearer" | "basic" | "header";
  auth_header?: string | null;
  environment?: string | null;
}

export interface IntegrationCredentials {
  password?: string;
  private_key?: string;
  passphrase?: string;
  ca?: string;
  cert?: string;
  key?: string;
  token?: string;
}

export interface Integration {
  id: string;
  name: string;
  integration_type: IntegrationType;
  description: string;
  configuration: IntegrationSettings;
  targets: Target[];
  enabled: boolean;
  schedule_minutes: number | null;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: "completed" | "partial" | "failed" | null;
  last_error: string | null;
  created_at: string;
  has_credentials: boolean;
  credential_fields: string[];
  resource_count: number;
}

export interface IntegrationTestResult {
  ok: boolean;
  target: string;
  message: string;
  details: Record<string, unknown>;
}

export interface TargetResult {
  target: string;
  ok: boolean;
  duration_ms: number;
  resources: number;
  error?: string;
  error_code?: string;
  details?: Record<string, unknown>;
  host_key_fingerprint?: string;
  host_key_pinned_now?: boolean;
}

export interface DiscoveryRun {
  id: string;
  integration_id: string;
  trigger: "manual" | "scheduled";
  triggered_by: string | null;
  status: "queued" | "running" | "completed" | "partial" | "failed";
  started_at: string;
  completed_at: string | null;
  stats: Partial<Record<string, number>> & { only_target?: string };
  target_results: TargetResult[];
  error: string | null;
}

export interface HostKey {
  id: string;
  host: string;
  port: number;
  key_type: string;
  fingerprint: string;
  first_seen_at: string;
  last_seen_at: string | null;
}

export interface ResourceSummary {
  id: string;
  name: string;
  resource_type: ResourceType;
  hostname: string | null;
  ip_addresses: string[];
  os: string | null;
  environment: string | null;
  group_name: string | null;
  owner: string | null;
  tags: string[];
  health_status: HealthStatus;
  health_reasons: string[];
  source: "discovered" | "manual";
  stale: boolean;
  integration_id: string | null;
  last_seen_at: string | null;
  created_at: string;
  /** The host a container runs on (RUNS_ON relationship). */
  parent_id: string | null;
  parent_name: string | null;
  /** Containers running on this host, counted by health. */
  child_count: number;
  child_health: Partial<Record<HealthStatus, number>>;
}

export interface Resource extends ResourceSummary {
  description: string;
  facts: Record<string, unknown>;
  last_health_at: string | null;
  discovery_target: string | null;
  updated_at: string | null;
  integration_name: string | null;
}

export interface ResourceInput {
  name: string;
  resource_type: ResourceType;
  hostname?: string | null;
  ip_addresses?: string[];
  os?: string | null;
  environment?: string | null;
  group_name?: string | null;
  owner?: string | null;
  tags?: string[];
  description?: string;
}

export interface ResourceEvent {
  id: string;
  event_type: string;
  summary: string;
  details: Record<string, unknown>;
  actor: string | null;
  created_at: string;
}

export interface MetricSample {
  taken_at: string;
  cpu_load: number | null;
  cpu_count: number | null;
  memory_percent: number | null;
  disk_percent: number | null;
  latency_ms: number | null;
}

export interface Relationship {
  id: string;
  source_id: string;
  target_id: string;
  relationship_type: RelationshipType;
  source: "discovered" | "manual";
  confidence: number;
  properties: Record<string, unknown>;
  last_seen_at: string | null;
  created_at: string;
}

export interface GraphNode {
  id: string;
  name: string;
  resource_type: ResourceType;
  health_status: HealthStatus;
  environment: string | null;
  group_name: string | null;
  stale: boolean;
  depth: number | null;
  direction: "self" | "upstream" | "downstream" | null;
}

export interface GraphEdge {
  id: string;
  source_id: string;
  target_id: string;
  relationship_type: RelationshipType;
  source: "discovered" | "manual";
  confidence: number;
}

export interface DependencyGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  blast_radius: string[];
  truncated: boolean;
}

export interface InventorySummary {
  total: number;
  by_health: Partial<Record<HealthStatus, number>>;
  by_type: Partial<Record<ResourceType, number>>;
  stale: number;
  last_discovery_at: string | null;
  integrations: number;
}

export interface InventoryFacets {
  environments: string[];
  groups: string[];
  tags: string[];
}

export interface EvidenceItem {
  id?: string;
  source: string;
  agent: string;
  tool?: string;
  excerpt?: string;
  ok?: boolean;
  arguments?: Record<string, unknown>;
  execution_id?: string | null;
  duration_ms?: number;
  baseline?: boolean;
  ref?: { kind: string; id: string; number?: string | null };
  resource_id?: string | null;
  resource: string | null;
  resource_type?: ResourceType;
  summary: string;
  findings?: string[];
  depends_on?: string[];
  dependents?: number;
  health?: HealthStatus;
  observed_at?: string;
}

// ------------------------------------------------------------------ Incidents (Phase 4)

export type Severity = "critical" | "high" | "medium" | "low" | "informational";
export type Priority = "P1" | "P2" | "P3" | "P4";
export type IncidentStatus =
  | "new"
  | "investigating"
  | "awaiting_approval"
  | "remediating"
  | "monitoring"
  | "resolved"
  | "closed"
  | "failed";
export type IncidentSource = "manual" | "investigation" | "discovery";

export interface IncidentResource {
  id: string;
  name: string;
  resource_type: ResourceType;
  health_status: HealthStatus;
  environment: string | null;
}

export interface Incident {
  id: string;
  number: number;
  key: string;
  title: string;
  description: string;
  severity: Severity;
  priority: Priority;
  status: IncidentStatus;
  source: IncidentSource;
  assigned_to: string | null;
  assigned_username: string | null;
  created_by: string | null;
  root_cause: Partial<RootCause> & {
    investigation_id?: string;
    contributing_factors?: { statement: string; evidence_ids: string[] }[];
    recommended_remediation?: Remediation[];
  };
  investigation_summary: string | null;
  resolution: string | null;
  created_at: string;
  updated_at: string | null;
  resolved_at: string | null;
  closed_at: string | null;
  resources: IncidentResource[];
}

export interface IncidentDetail extends Incident {
  investigations: {
    id: string;
    title: string;
    status: InvestigationStatus;
    started_at: string;
    completed_at: string | null;
    conclusion: string | null;
  }[];
  allowed_transitions: IncidentStatus[];
}

export interface IncidentEvent {
  id: string;
  event_type: string;
  summary: string;
  details: Record<string, unknown>;
  actor: string | null;
  created_at: string;
}

export interface IncidentStats {
  open: number;
  by_status: Partial<Record<IncidentStatus, number>>;
  by_severity_open: Partial<Record<Severity, number>>;
  resolved_last_7d: number;
  mean_time_to_resolve_minutes: number | null;
}

export interface IncidentInput {
  title: string;
  description?: string;
  severity?: Severity;
  priority?: Priority;
  resource_ids?: string[];
  assigned_to?: string | null;
  investigation_id?: string;
}

export interface ToolExecution {
  id: string;
  investigation_id: string | null;
  resource_id: string | null;
  resource_name: string | null;
  agent_type: string | null;
  actor: string | null;
  evidence_id: string | null;
  tool: string;
  arguments: Record<string, unknown>;
  status: "success" | "failed" | "rejected";
  duration_ms: number | null;
  output: string | null;
  summary: string | null;
  error: string | null;
  created_at: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  category: string;
  kind: "read" | "change";
  source: ToolSource;
  permission: string;
  verify_with: string[];
  risk_level: string;
  requires_approval: boolean;
  remote: boolean;
  timeout_seconds: number;
  resource_types: ResourceType[];
  input_schema: {
    properties?: Record<string, { type?: string; description?: string; enum?: string[]; default?: unknown; minimum?: number; maximum?: number }>;
    required?: string[];
  };
}

export interface ToolRunResult {
  tool: string;
  status: "success" | "failed" | "rejected";
  summary: string;
  output: string;
  error: string | null;
  duration_ms: number;
  execution_id: string | null;
  resource_name: string | null;
}

// ------------------------------------------------------------------ Remediation (Phase 5)

export type RiskLevel = "low" | "medium" | "high" | "critical";
export type ApprovalStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "changes_requested"
  | "cancelled"
  | "expired"
  | "executed"
  | "failed";
export type RemediationStepStatus =
  | "proposed"
  | "advisory"
  | "blocked"
  | "approved"
  | "changes_requested"
  | "rejected"
  | "cancelled"
  | "expired"
  | "skipped"
  | "dropped"
  | "executed"
  | "failed"
  | "verified"
  | "unverified"
  | "not_run";

export interface PolicyDecision {
  risk_level: RiskLevel;
  requires_approval: boolean;
  auto_approve: boolean;
  allow_self_approval: boolean;
  approval_ttl_minutes: number;
  denied: boolean;
  denied_reason: string | null;
  matched_rules: string[];
  notes: string[];
}

export interface Verification {
  status: "passed" | "failed";
  summary: string;
  reasons: string[];
}

export interface RemediationStep {
  index: number;
  tool: string;
  arguments: Record<string, unknown>;
  resource_id: string;
  resource_name: string;
  resource_type: string;
  description: string;
  risk_level: RiskLevel;
  policy: PolicyDecision;
  status: RemediationStepStatus;
  status_reason: string | null;
  rationale?: string;
  expected_impact?: string;
  evidence_ids?: string[];
  approval_id: string | null;
  approval?: { status: ApprovalStatus; auto_approved: boolean; comment: string | null } | null;
  job_id: string | null;
  execution: { status: string; summary: string; error_code?: string | null; duration_ms?: number | null } | null;
  verification: Verification | null;
  revisions: number;
}

export interface RemediationSection {
  status: "none" | "skipped" | "advisory" | "planned" | "stopped" | "verified" | "unverified" | "not_executed";
  reason: string | null;
  mode: ExecutionMode | null;
  notes: string[];
  steps: RemediationStep[];
}

export interface Execution {
  id: string;
  investigation_id: string | null;
  approval_id: string | null;
  step_index: number | null;
  tool: string;
  resource_id: string | null;
  resource_name: string | null;
  parameters: Record<string, unknown>;
  description: string;
  risk_level: RiskLevel;
  actor: string | null;
  status: "running" | "succeeded" | "failed" | "rejected";
  execution_result: {
    summary?: string;
    output?: string;
    error?: string;
    error_code?: string;
    data?: Record<string, unknown>;
  };
  before: Record<string, { ok: boolean; summary: string }>;
  after: Record<string, { ok: boolean; summary: string }>;
  verification_status: "pending" | "passed" | "failed" | "skipped";
  verification: Partial<Verification>;
  started_at: string;
  completed_at: string | null;
  duration_ms: number | null;
}

export interface Approval {
  id: string;
  investigation_id: string;
  incident_id: string | null;
  step_index: number;
  requested_by: string;
  requested_by_username: string | null;
  approved_by: string | null;
  approved_by_username: string | null;
  action: string;
  action_parameters: Record<string, unknown>;
  description: string;
  resource_id: string | null;
  resource_name: string | null;
  risk_level: RiskLevel;
  rationale: string;
  expected_impact: string;
  evidence_ids: string[];
  policy: Partial<PolicyDecision>;
  auto_approved: boolean;
  status: ApprovalStatus;
  decision_comment: string | null;
  expires_at: string;
  decided_at: string | null;
  created_at: string;
}

export interface ApprovalEvidence {
  id: string;
  tool?: string | null;
  resource?: string | null;
  summary?: string;
  excerpt?: string;
}

export interface ApprovalDetail extends Approval {
  investigation_title: string | null;
  incident_key: string | null;
  can_decide: boolean;
  cannot_decide_reason: string | null;
  evidence: ApprovalEvidence[];
  execution: Execution | null;
}

export interface ApprovalSummary {
  pending: number;
  pending_for_me: number;
  by_risk: Partial<Record<RiskLevel, number>>;
  expiring_soon: number;
}

export interface PolicyMatch {
  tools?: string[];
  categories?: string[];
  risks?: string[];
  environments?: string[];
  resource_types?: string[];
  tags?: string[];
  groups?: string[];
}

export interface PolicyEffect {
  risk_level?: RiskLevel | null;
  auto_approve?: boolean | null;
  allow_self_approval?: boolean | null;
  deny?: boolean | null;
  deny_reason?: string | null;
  approval_ttl_minutes?: number | null;
}

export interface PolicyRules {
  match: PolicyMatch;
  effect: PolicyEffect;
}

export interface Policy {
  id: string;
  name: string;
  description: string;
  policy_type: string;
  priority: number;
  rules: PolicyRules;
  enabled: boolean;
  builtin: boolean;
  created_at: string;
  updated_at: string | null;
}

export interface PolicyInput {
  name: string;
  description: string;
  priority: number;
  rules: PolicyRules;
  enabled: boolean;
}

// ------------------------------------------------------------------ Knowledge (Phase 6)

export type KnowledgeDocumentType =
  | "runbook"
  | "troubleshooting"
  | "documentation"
  | "procedure"
  | "incident"
  | "investigation"
  | "other";
export type EditableDocumentType = Exclude<KnowledgeDocumentType, "incident" | "investigation">;
export type DocumentStatus = "active" | "draft" | "deprecated";
export type IndexStatus = "pending" | "indexed" | "keyword_only" | "failed";
export type KnowledgeMatch = "keyword" | "semantic" | "both";

export interface KnowledgeDocument {
  id: string;
  title: string;
  document_type: KnowledgeDocumentType;
  source: "upload" | "manual" | "incident" | "investigation";
  source_id: string | null;
  category: string | null;
  tags: string[];
  resource_ids: string[];
  status: DocumentStatus;
  version: number;
  validated_by: string | null;
  validated_at: string | null;
  index_status: IndexStatus;
  index_error: string | null;
  embedding_model: string | null;
  indexed_at: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
  size: number;
  filename: string | null;
  chunk_count: number;
  helpful: number;
  not_helpful: number;
}

export interface KnowledgeChunk {
  id: string;
  chunk_index: number;
  heading: string;
  content: string;
  embedded: boolean;
}

export interface KnowledgeDocumentDetail extends KnowledgeDocument {
  content: string;
  metadata: Record<string, unknown>;
  chunks: KnowledgeChunk[];
  resource_names: Record<string, string>;
}

export interface KnowledgeVersion {
  version: number;
  title: string;
  created_by: string | null;
  created_at: string;
  note: string | null;
  size: number;
}

export interface KnowledgeVersionDetail extends KnowledgeVersion {
  content: string;
}

export interface KnowledgeHit {
  chunk_id: string;
  document_id: string;
  title: string;
  document_type: KnowledgeDocumentType;
  heading: string;
  excerpt: string;
  score: number;
  match: KnowledgeMatch;
  version: number;
  validated: boolean;
  status: DocumentStatus;
  tags: string[];
  resource_ids: string[];
  source: KnowledgeDocument["source"];
  source_id: string | null;
}

export interface KnowledgeSearchResponse {
  mode: "hybrid" | "keyword";
  note: string | null;
  hits: KnowledgeHit[];
}

export interface KnowledgeStats {
  documents: number;
  chunks: number;
  by_type: Record<string, number>;
  by_status: Record<string, number>;
  by_index_status: Record<string, number>;
  validated: number;
  embedding_model: string | null;
  vector_store: "ok" | "unavailable" | "not_configured";
}

export interface KnowledgeDocumentInput {
  title: string;
  document_type: EditableDocumentType;
  content: string;
  category?: string | null;
  tags?: string[];
  resource_ids?: string[];
  status?: "active" | "draft";
}

// ------------------------------------------------------------------ Registered tools (Phase 6)

export type ToolSource = "builtin" | "mcp" | "http";
export type ToolDefinitionStatus = "new" | "active" | "schema_changed" | "missing";
export type ToolPermission = "tool.execute.readonly" | "tool.execute.remediation";

export interface JsonSchemaObject {
  type?: string;
  properties?: Record<
    string,
    {
      type?: string;
      description?: string;
      enum?: (string | number)[];
      default?: unknown;
      minimum?: number;
      maximum?: number;
      maxLength?: number;
      pattern?: string;
    }
  >;
  required?: string[];
  [key: string]: unknown;
}

export interface HttpTemplate {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  query: Record<string, string>;
  headers: Record<string, string>;
  body: unknown;
  response_max_bytes: number;
}

export interface ToolDefinition {
  id: string;
  integration_id: string;
  integration_name: string | null;
  source: "mcp" | "http";
  name: string;
  remote_name: string | null;
  title: string | null;
  description: string;
  remote_description: string | null;
  input_schema: JsonSchemaObject;
  output_schema: JsonSchemaObject | null;
  http_template: HttpTemplate | null;
  kind: "read" | "change";
  risk_level: RiskLevel;
  required_permission: ToolPermission;
  timeout_seconds: number;
  verify_with: string | null;
  enabled: boolean;
  status: ToolDefinitionStatus;
  schema_hash: string;
  approved_schema_hash: string | null;
  approved_by: string | null;
  last_synced_at: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
  usable: boolean;
}

export interface ToolDefinitionUpdate {
  description?: string;
  kind?: "read" | "change";
  risk_level?: RiskLevel;
  required_permission?: ToolPermission;
  timeout_seconds?: number;
  verify_with?: string;
  clear_verify_with?: boolean;
  enabled?: boolean;
  approve_schema?: boolean;
}

export interface ToolParam {
  name: string;
  type: "string" | "integer" | "number" | "boolean";
  description: string;
  required: boolean;
  enum?: (string | number)[] | null;
  pattern?: string | null;
  max_length?: number | null;
  minimum?: number | null;
  maximum?: number | null;
}

export interface HttpToolInput {
  integration_id: string;
  name: string;
  description: string;
  parameters: ToolParam[];
  template: Partial<HttpTemplate> & Pick<HttpTemplate, "method" | "path">;
  kind: "read" | "change";
  risk_level: RiskLevel;
  timeout_seconds: number;
  verify_with?: string | null;
  enabled: boolean;
}

export interface ToolSyncResult {
  server: { name?: string; version?: string };
  total: number;
  added: string[];
  changed: string[];
  missing: string[];
  unchanged: number;
}

export interface AgentCreateInput {
  name: string;
  description: string;
  slug?: string | null;
  model_id?: string | null;
  enabled?: boolean;
  allowed_tools: string[];
  instructions?: string;
  max_steps?: number;
  temperature?: number;
  max_tokens?: number;
  timeout_seconds?: number;
  max_retries?: number;
}
