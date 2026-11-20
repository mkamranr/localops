import { describe, expect, it } from "vitest";
import { childrenNeedAttention, childSummary, effectiveView } from "@/components/inventory/hierarchy";

describe("inventory hierarchy", () => {
  it("defaults to the grouped view", () => {
    expect(effectiveView(null, { q: "", type: "" })).toEqual({ view: "grouped", forced: false });
    expect(effectiveView("all", { q: "", type: "" })).toEqual({ view: "all", forced: false });
  });

  it("switches to the flat list when searching or filtering on containers", () => {
    expect(effectiveView(null, { q: "migrate", type: "" })).toEqual({ view: "all", forced: true });
    expect(effectiveView("grouped", { q: "", type: "docker_container" })).toEqual({ view: "all", forced: true });
    expect(effectiveView(null, { q: "", type: "linux_server" })).toEqual({ view: "grouped", forced: false });
  });

  it("summarises children by health", () => {
    expect(childSummary({})).toBe("");
    expect(childSummary({ healthy: 1 })).toBe("1 container");
    expect(childSummary({ healthy: 11, unhealthy: 2, degraded: 1 })).toBe("14 containers · 2 unhealthy, 1 degraded");
    expect(childrenNeedAttention({ healthy: 3 })).toBe(false);
    expect(childrenNeedAttention({ healthy: 3, degraded: 1 })).toBe(true);
  });
});
