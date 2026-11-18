import { api } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import type {
  AuditLog,
  DashboardSummary,
  Me,
  Page,
  Permission,
  Role,
  Tenant,
  TokenResponse,
  User,
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
