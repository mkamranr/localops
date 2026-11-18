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
}

export interface ModelEndpoint {
  id: string;
  name: string;
  provider: Provider;
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
}

export interface AgentStats {
  executions_24h: number;
  failures_24h: number;
  avg_duration_ms: number | null;
  tokens_24h: number;
  last_run_at: string | null;
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
  pending_question: string | null;
  error: string | null;
  cancel_requested: boolean;
  attempts: number;
  started_at: string;
  completed_at: string | null;
  updated_at: string | null;
}

export interface ReportTask {
  id: string;
  agent: string;
  objective: string;
  rationale: string;
  status: string;
  reason: string | null;
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
  resources: { name: string; resolved: boolean; resource_id: string | null }[];
  clarifications: { question: string; answer: string }[];
  tasks: ReportTask[];
  evidence: unknown[];
  evidence_sufficient: boolean;
  root_cause: string | null;
  conclusion: string;
  recommended_next_steps: string[];
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
