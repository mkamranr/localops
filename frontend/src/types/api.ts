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
  pending_approvals: number;
  knowledge_documents: number;
}
