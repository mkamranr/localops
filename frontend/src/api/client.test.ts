import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiRequest, errorMessage } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  useAuthStore.setState({ accessToken: "old", user: null, status: "anonymous" });
});
afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

const authHeader = (call: number) =>
  (fetchMock.mock.calls[call]?.[1]?.headers as Record<string, string> | undefined)?.Authorization;

describe("apiRequest", () => {
  it("sends the bearer token and parses JSON", async () => {
    fetchMock.mockResolvedValueOnce(json(200, { ok: true }));
    await expect(apiRequest("/x", { query: { a: 1, b: undefined } })).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/x?a=1");
    expect(authHeader(0)).toBe("Bearer old");
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    fetchMock
      .mockResolvedValueOnce(json(401, { error: { code: "token_expired", message: "expired" } }))
      .mockResolvedValueOnce(json(200, { access_token: "new", token_type: "bearer", expires_in: 900 }))
      .mockResolvedValueOnce(json(200, { ok: true }));
    await expect(apiRequest("/x")).resolves.toEqual({ ok: true });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/auth/refresh");
    expect(authHeader(2)).toBe("Bearer new");
    expect(useAuthStore.getState().accessToken).toBe("new");
  });

  it("shares one refresh between concurrent 401s", async () => {
    let refreshCalls = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith("/auth/refresh")) {
        refreshCalls += 1;
        return json(200, { access_token: "new", token_type: "bearer", expires_in: 900 });
      }
      const auth = (init?.headers as Record<string, string>).Authorization;
      return auth === "Bearer new" ? json(200, { url }) : json(401, { error: { code: "x", message: "x" } });
    });
    await Promise.all([apiRequest("/a"), apiRequest("/b"), apiRequest("/c")]);
    expect(refreshCalls).toBe(1);
  });

  it("clears the session when refresh fails", async () => {
    useAuthStore.setState({ status: "authenticated" });
    fetchMock
      .mockResolvedValueOnce(json(401, { error: { code: "unauthenticated", message: "no" } }))
      .mockResolvedValueOnce(json(401, { error: { code: "invalid_refresh_token", message: "no" } }));
    await expect(apiRequest("/x")).rejects.toBeInstanceOf(ApiError);
    expect(useAuthStore.getState().status).toBe("anonymous");
    expect(useAuthStore.getState().accessToken).toBeNull();
  });

  it("does not retry when noRetry is set", async () => {
    fetchMock.mockResolvedValueOnce(json(401, { error: { code: "invalid_credentials", message: "bad" } }));
    await expect(apiRequest("/auth/login", { noRetry: true })).rejects.toMatchObject({
      status: 401,
      code: "invalid_credentials",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns undefined for 204", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(apiRequest("/x", { method: "DELETE" })).resolves.toBeUndefined();
  });
});

describe("errorMessage", () => {
  it("formats validation errors with the field name", () => {
    const err = new ApiError(422, "validation_error", "Request validation failed", [
      { loc: ["body", "admin", "email"], msg: "invalid email" },
    ]);
    expect(errorMessage(err)).toBe("admin.email: invalid email");
  });
  it("falls back to the message", () => {
    expect(errorMessage(new ApiError(409, "conflict", "exists"))).toBe("exists");
    expect(errorMessage(new Error("boom"))).toBe("boom");
  });
});
