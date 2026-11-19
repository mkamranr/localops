import { api } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import type {
  Approval,
  ApprovalDetail,
  ApprovalStatus,
  ApprovalSummary,
  Execution,
  Policy,
  PolicyDecision,
  PolicyInput,
  RiskLevel,
  Agent,
  AgentConfig,
  AgentExecution,
  AuditLog,
  DashboardSummary,
  DependencyGraph,
  DiscoveryRun,
  HostKey,
  Integration,
  IntegrationCredentials,
  IntegrationSettings,
  IntegrationTestResult,
  IntegrationType,
  InventoryFacets,
  InventorySummary,
  MetricSample,
  Relationship,
  RelationshipType,
  Resource,
  ResourceEvent,
  ResourceInput,
  ResourceSummary,
  Target,
  ExecutionMode,
  Investigation,
  InvestigationDetail,
  InvestigationEvent,
  Me,
  ModelEndpoint,
  ModelSettings,
  ModelTestResult,
  Provider,
  Page,
  Permission,
  Role,
  Tenant,
  TokenResponse,
  User,
  Incident,
  IncidentDetail,
  IncidentEvent,
  IncidentInput,
  IncidentStats,
  IncidentStatus,
  Priority,
  Severity,
  SimilarItem,
  ToolExecution,
  ToolInfo,
  ToolRunResult,
} from "@/types/api";

export const authApi = {
  async login(tenant: string, username: string, password: string): Promise<Me> {
    const tokens = await api.post<TokenResponse>(
      "/auth/login",
      { tenant, username, password },
      { noRetry: true },
    );
    useAuthStore.getState().setToken(tokens.access_token);
    const me = await api.get<Me>("/auth/me");
    useAuthStore.getState().setUser(me);
    return me;
  },
  async logout(): Promise<void> {
    try {
      await api.post("/auth/logout", undefined, { noRetry: true });
    } finally {
      useAuthStore.getState().clear();
    }
  },
  me: () => api.get<Me>("/auth/me"),
  async changePassword(current_password: string, new_password: string): Promise<void> {
    const tokens = await api.post<TokenResponse>("/auth/change-password", {
      current_password,
      new_password,
    });
    useAuthStore.getState().setToken(tokens.access_token);
    useAuthStore.getState().setUser(await api.get<Me>("/auth/me"));
  },
};

export interface UserListQuery {
  page: number;
  page_size: number;
  q?: string;
  status?: string;
}

export const usersApi = {
  list: (query: UserListQuery) => api.get<Page<User>>("/users", { ...query }),
  create: (body: {
    username: string;
    email: string;
    password: string;
    display_name?: string;
    role_ids: string[];
    must_change_password: boolean;
  }) => api.post<User>("/users", body),
  update: (
    id: string,
    body: Partial<{ email: string; display_name: string; status: string; password: string }>,
  ) => api.put<User>(`/users/${id}`, body),
  setRoles: (id: string, role_ids: string[]) => api.put<User>(`/users/${id}/roles`, { role_ids }),
  remove: (id: string) => api.delete(`/users/${id}`),
};

export const rolesApi = {
  list: () => api.get<Role[]>("/roles"),
  permissions: () => api.get<Permission[]>("/permissions"),
};

export const tenantsApi = {
  list: (page: number, page_size: number) => api.get<Page<Tenant>>("/tenants", { page, page_size }),
  create: (body: {
    name: string;
    slug: string;
    admin: { username: string; email: string; password: string; display_name?: string };
  }) => api.post<Tenant>("/tenants", body),
  update: (id: string, body: { name?: string }) => api.put<Tenant>(`/tenants/${id}`, body),
  setEnabled: (id: string, enabled: boolean) =>
    api.post<Tenant>(`/tenants/${id}/${enabled ? "enable" : "disable"}`),
};

export interface AuditQuery {
  page: number;
  page_size: number;
  action?: string;
  entity_id?: string;
  outcome?: string;
  date_from?: string;
  date_to?: string;
}

export const auditApi = {
  list: (query: AuditQuery) => api.get<Page<AuditLog>>("/audit-logs", { ...query }),
};

export const dashboardApi = {
  summary: () => api.get<DashboardSummary>("/dashboard/summary"),
  activity: (limit = 10) => api.get<AuditLog[]>("/dashboard/activity", { limit }),
};

export interface ModelInput {
  name: string;
  provider: Provider;
  base_url: string;
  model_name: string;
  description?: string;
  configuration?: ModelSettings;
  api_key?: string;
  clear_api_key?: boolean;
  enabled?: boolean;
  is_default?: boolean;
}

export const modelsApi = {
  list: () => api.get<ModelEndpoint[]>("/models"),
  create: (body: ModelInput) => api.post<ModelEndpoint>("/models", body),
  update: (id: string, body: Partial<ModelInput>) => api.put<ModelEndpoint>(`/models/${id}`, body),
  remove: (id: string) => api.delete(`/models/${id}`),
  test: (id: string) => api.post<ModelTestResult>(`/models/${id}/test`),
};

export const agentsApi = {
  list: () => api.get<Agent[]>("/agents"),
  update: (
    id: string,
    body: Partial<{
      name: string;
      description: string;
      model_id: string | null;
      enabled: boolean;
      configuration: Partial<Omit<AgentConfig, "allowed_tools">>;
    }>,
  ) => api.put<Agent>(`/agents/${id}`, body),
  executions: (query: {
    page: number;
    page_size: number;
    agent_type?: string;
    investigation_id?: string;
  }) => api.get<Page<AgentExecution>>("/agents/executions", { ...query }),
};

export interface InvestigationQuery {
  page: number;
  page_size: number;
  status?: string;
  q?: string;
  active?: boolean;
}

export const investigationsApi = {
  list: (query: InvestigationQuery) =>
    api.get<Page<Investigation>>("/investigations", { ...query }),
  get: (id: string) => api.get<InvestigationDetail>(`/investigations/${id}`),
  events: (id: string, after = 0) =>
    api.get<InvestigationEvent[]>(`/investigations/${id}/events`, { after }),
  create: (body: {
    request: string;
    title?: string;
    execution_mode: ExecutionMode;
    resource_ids?: string[];
    incident_id?: string;
  }) => api.post<Investigation>("/investigations", body),
  toolExecutions: (id: string) => api.get<ToolExecution[]>(`/investigations/${id}/tool-executions`),
  resume: (id: string, answer: string) =>
    api.post<Investigation>(`/investigations/${id}/resume`, { answer }),
  cancel: (id: string) => api.post<Investigation>(`/investigations/${id}/cancel`),
  remediationStep: (id: string, step: number, action: "request" | "skip") =>
    api.post<Investigation>(`/investigations/${id}/remediation/${step}`, { action }),
};

// ------------------------------------------------------------------ inventory (Phase 3)

export interface IntegrationInput {
  name: string;
  integration_type: IntegrationType;
  description?: string;
  configuration?: IntegrationSettings;
  credentials?: IntegrationCredentials;
  clear_credentials?: boolean;
  targets?: Target[];
  enabled?: boolean;
  schedule_minutes?: number | null;
  clear_schedule?: boolean;
}

export const integrationsApi = {
  list: () => api.get<Integration[]>("/integrations"),
  get: (id: string) => api.get<Integration>(`/integrations/${id}`),
  create: (body: IntegrationInput) => api.post<Integration>("/integrations", body),
  update: (id: string, body: Partial<IntegrationInput>) =>
    api.put<Integration>(`/integrations/${id}`, body),
  remove: (id: string) => api.delete(`/integrations/${id}`),
  test: (id: string, target?: Target) =>
    api.post<IntegrationTestResult>(`/integrations/${id}/test`, target ? { target } : {}),
  discover: (id: string) => api.post<DiscoveryRun>(`/integrations/${id}/discover`),
  runs: (id: string, page = 1, page_size = 10) =>
    api.get<Page<DiscoveryRun>>(`/integrations/${id}/runs`, { page, page_size }),
  run: (runId: string) => api.get<DiscoveryRun>(`/integrations/runs/${runId}`),
  hostKeys: (id: string) => api.get<HostKey[]>(`/integrations/${id}/host-keys`),
  resetHostKey: (id: string, keyId: string) => api.delete(`/integrations/${id}/host-keys/${keyId}`),
};

export interface ResourceQuery {
  page: number;
  page_size: number;
  q?: string;
  type?: string;
  environment?: string;
  health?: string;
  group?: string;
  tag?: string;
  integration_id?: string;
  stale?: boolean;
}

export const resourcesApi = {
  list: (query: ResourceQuery) => api.get<Page<ResourceSummary>>("/resources", { ...query }),
  summary: () => api.get<InventorySummary>("/resources/summary"),
  facets: () => api.get<InventoryFacets>("/resources/facets"),
  get: (id: string) => api.get<Resource>(`/resources/${id}`),
  create: (body: ResourceInput) => api.post<Resource>("/resources", body),
  update: (id: string, body: Partial<ResourceInput>) => api.put<Resource>(`/resources/${id}`, body),
  remove: (id: string) => api.delete(`/resources/${id}`),
  checkHealth: (id: string) => api.post<DiscoveryRun>(`/resources/${id}/health/check`),
  history: (id: string, page = 1, page_size = 25) =>
    api.get<Page<ResourceEvent>>(`/resources/${id}/history`, { page, page_size }),
  metrics: (id: string, hours = 168) =>
    api.get<MetricSample[]>(`/resources/${id}/metrics`, { hours }),
  relationships: (id: string) => api.get<Relationship[]>(`/resources/${id}/relationships`),
  dependencies: (id: string, direction: "up" | "down" | "both" = "both", depth = 3) =>
    api.get<DependencyGraph>(`/resources/${id}/dependencies`, { direction, depth }),
  graph: (query: { environment?: string; group?: string; type?: string; include_stale?: boolean }) =>
    api.get<DependencyGraph>("/resources/graph", { ...query }),
  addRelationship: (body: {
    source_id: string;
    target_id: string;
    relationship_type: RelationshipType;
    description?: string;
  }) => api.post<Relationship>("/resource-relationships", body),
  removeRelationship: (id: string) => api.delete(`/resource-relationships/${id}`),
};

// ------------------------------------------------------------------ incidents & tools (Phase 4)

export interface IncidentQuery {
  page: number;
  page_size: number;
  status?: string;
  severity?: string;
  source?: string;
  assigned_to?: string;
  resource_id?: string;
  open?: boolean;
  q?: string;
}

export interface IncidentUpdate {
  title?: string;
  description?: string;
  severity?: Severity;
  priority?: Priority;
  status?: IncidentStatus;
  assigned_to?: string | null;
  resolution?: string;
  resource_ids?: string[];
}

export const incidentsApi = {
  list: (query: IncidentQuery) => api.get<Page<Incident>>("/incidents", { ...query }),
  summary: () => api.get<IncidentStats>("/incidents/summary"),
  get: (id: string) => api.get<IncidentDetail>(`/incidents/${id}`),
  create: (body: IncidentInput) => api.post<IncidentDetail>("/incidents", body),
  update: (id: string, body: IncidentUpdate) => api.put<IncidentDetail>(`/incidents/${id}`, body),
  close: (id: string, resolution: string, status: "resolved" | "closed" = "closed") =>
    api.post<IncidentDetail>(`/incidents/${id}/close`, { resolution, status }),
  investigate: (id: string, body: { request?: string; execution_mode?: ExecutionMode } = {}) =>
    api.post<Investigation>(`/incidents/${id}/investigate`, body),
  timeline: (id: string) => api.get<IncidentEvent[]>(`/incidents/${id}/timeline`),
  comment: (id: string, body: string) => api.post<IncidentEvent>(`/incidents/${id}/comments`, { body }),
  similar: (id: string) => api.get<SimilarItem[]>(`/incidents/${id}/similar`),
  toolExecutions: (id: string) => api.get<ToolExecution[]>(`/incidents/${id}/tool-executions`),
};

export const toolsApi = {
  list: (resource_type?: string) => api.get<ToolInfo[]>("/tools", { resource_type }),
  run: (name: string, resource_id: string, args: Record<string, unknown>) =>
    api.post<ToolRunResult>(`/tools/${name}/run`, { resource_id, arguments: args }),
  forResource: (id: string, limit = 50) =>
    api.get<ToolExecution[]>(`/resources/${id}/tool-executions`, { limit }),
};

// ------------------------------------------------------------------ remediation (Phase 5)

export interface ApprovalQuery {
  page: number;
  page_size: number;
  status?: ApprovalStatus;
  risk?: RiskLevel;
  mine?: boolean;
  investigation_id?: string;
  incident_id?: string;
}

export interface ExecutionQuery {
  page: number;
  page_size: number;
  investigation_id?: string;
  incident_id?: string;
  resource_id?: string;
  status?: Execution["status"];
}

export const approvalsApi = {
  list: (query: ApprovalQuery) => api.get<Page<Approval>>("/approvals", { ...query }),
  summary: () => api.get<ApprovalSummary>("/approvals/summary"),
  get: (id: string) => api.get<ApprovalDetail>(`/approvals/${id}`),
  approve: (id: string, comment?: string) => api.post<Approval>(`/approvals/${id}/approve`, { comment }),
  reject: (id: string, comment?: string) => api.post<Approval>(`/approvals/${id}/reject`, { comment }),
  requestChanges: (id: string, comment: string) =>
    api.post<Approval>(`/approvals/${id}/request-changes`, { comment }),
  cancel: (id: string, comment?: string) => api.post<Approval>(`/approvals/${id}/cancel`, { comment }),
};

export const executionsApi = {
  list: (query: ExecutionQuery) => api.get<Page<Execution>>("/executions", { ...query }),
  get: (id: string) => api.get<Execution>(`/executions/${id}`),
};

export type PolicyPreview = PolicyDecision & { tool: string; resource: string };

export const policiesApi = {
  list: () => api.get<Policy[]>("/policies"),
  actions: () => api.get<ToolInfo[]>("/policies/actions"),
  preview: (body: { tool: string; resource_id: string; execution_mode: ExecutionMode }) =>
    api.post<PolicyPreview>("/policies/preview", body),
  create: (body: PolicyInput) => api.post<Policy>("/policies", body),
  update: (id: string, body: Partial<PolicyInput>) => api.put<Policy>(`/policies/${id}`, body),
  remove: (id: string) => api.delete(`/policies/${id}`),
};
