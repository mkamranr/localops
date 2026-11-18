import { create } from "zustand";
import type { Me } from "@/types/api";

/**
 * Authentication state. The access token lives only in memory; the refresh token is an
 * httpOnly cookie the browser sends to /api/v1/auth automatically.
 */
interface AuthState {
  accessToken: string | null;
  user: Me | null;
  status: "initialising" | "authenticated" | "anonymous";
  setToken: (token: string | null) => void;
  setUser: (user: Me | null) => void;
  clear: () => void;
  hasPermission: (permission: string) => boolean;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  accessToken: null,
  user: null,
  status: "initialising",
  setToken: (accessToken) => set({ accessToken }),
  setUser: (user) => set({ user, status: user ? "authenticated" : "anonymous" }),
  clear: () => set({ accessToken: null, user: null, status: "anonymous" }),
  hasPermission: (permission) => get().user?.permissions.includes(permission) ?? false,
}));
