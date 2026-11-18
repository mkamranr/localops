import { describe, expect, it } from "vitest";
import { NAVIGATION, visibleNavigation } from "@/layouts/navigation";
import { useAuthStore } from "@/stores/authStore";

describe("navigation", () => {
  it("has the 13 spec sidebar entries", () => {
    expect(NAVIGATION.flatMap((g) => g.items)).toHaveLength(13);
  });

  it("hides entries the user lacks permissions for", () => {
    const perms = new Set(["dashboard.read"]);
    const labels = visibleNavigation((p) => perms.has(p)).flatMap((g) => g.items.map((i) => i.label));
    expect(labels).toEqual(["Dashboard", "Settings"]);
  });
});

describe("authStore", () => {
  it("tracks permissions of the current user", () => {
    const store = useAuthStore.getState();
    store.setUser({
      id: "1",
      username: "a",
      email: "a@b.c",
      display_name: "A",
      must_change_password: false,
      tenant: { id: "t", name: "T", slug: "t" },
      roles: ["Auditor"],
      permissions: ["audit.read"],
    });
    expect(useAuthStore.getState().status).toBe("authenticated");
    expect(useAuthStore.getState().hasPermission("audit.read")).toBe(true);
    expect(useAuthStore.getState().hasPermission("user.create")).toBe(false);
    useAuthStore.getState().clear();
    expect(useAuthStore.getState().hasPermission("audit.read")).toBe(false);
  });
});
