import { useAuthStore } from "@/stores/authStore";
import type { ApiErrorBody, TokenResponse } from "@/types/api";

export const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details?: unknown,
    public readonly requestId?: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  query?: Query;
  /** Skip the automatic refresh-and-retry on 401 (used by auth endpoints). */
  noRetry?: boolean;
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: Query): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

async function toApiError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as ApiErrorBody;
    const e = body.error;
    return new ApiError(res.status, e.code, e.message, e.details, e.request_id);
  } catch {
    return new ApiError(res.status, "http_error", res.statusText || "Request failed");
  }
}

// Single-flight refresh: concurrent 401s share one refresh request.
let refreshing: Promise<boolean> | null = null;

export function refreshAccessToken(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const res = await fetch(`${API_BASE}/auth/refresh`, {
          method: "POST",
          credentials: "same-origin",
        });
        if (!res.ok) return false;
        const data = (await res.json()) as TokenResponse;
        useAuthStore.getState().setToken(data.access_token);
        return true;
      } catch {
        return false;
      } finally {
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

export async function apiRequest<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const send = () => {
    const token = useAuthStore.getState().accessToken;
    const headers: Record<string, string> = { Accept: "application/json" };
    const multipart = typeof FormData !== "undefined" && opts.body instanceof FormData;
    if (opts.body !== undefined && !multipart) headers["Content-Type"] = "application/json";
    if (token) headers.Authorization = `Bearer ${token}`;
    return fetch(buildUrl(path, opts.query), {
      method: opts.method ?? "GET",
      headers,
      body: multipart ? (opts.body as FormData) : opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin",
      signal: opts.signal,
    });
  };

  let res = await send();
  if (res.status === 401 && !opts.noRetry) {
    if (await refreshAccessToken()) {
      res = await send();
    } else {
      useAuthStore.getState().clear();
    }
  }
  if (!res.ok) throw await toApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string, query?: Query) => apiRequest<T>(path, { query }),
  post: <T>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "POST", body }),
  put: <T>(path: string, body?: unknown) => apiRequest<T>(path, { method: "PUT", body }),
  delete: <T = void>(path: string) => apiRequest<T>(path, { method: "DELETE" }),
};

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "validation_error" && Array.isArray(err.details)) {
      const first = err.details[0] as { loc?: unknown[]; msg?: string } | undefined;
      if (first?.msg) {
        const field = first.loc?.filter((p) => p !== "body").join(".");
        return field ? `${field}: ${first.msg}` : first.msg;
      }
    }
    return err.message;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}
