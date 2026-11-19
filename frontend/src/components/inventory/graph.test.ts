import { describe, expect, it } from "vitest";
import { dependenciesOf, dependentsOf, toElements } from "@/components/inventory/graph";
import type { DependencyGraph, GraphEdge, GraphNode } from "@/types/api";

const node = (id: string, extra: Partial<GraphNode> = {}): GraphNode => ({
  id,
  name: id,
  resource_type: "linux_server",
  health_status: "healthy",
  environment: null,
  group_name: null,
  stale: false,
  depth: null,
  direction: null,
  ...extra,
});

const edge = (id: string, s: string, t: string, type: GraphEdge["relationship_type"]): GraphEdge => ({
  id,
  source_id: s,
  target_id: t,
  relationship_type: type,
  source: "manual",
  confidence: 1,
});

// app DEPENDS_ON db; db STORES_DATA_IN san; host HOSTS app; mon MONITORED_BY (no dependency)
const graph: DependencyGraph = {
  nodes: [node("app"), node("db", { environment: "prod" }), node("san"), node("host"), node("mon")],
  edges: [
    edge("e1", "app", "db", "DEPENDS_ON"),
    edge("e2", "db", "san", "STORES_DATA_IN"),
    edge("e3", "host", "app", "HOSTS"),
    edge("e4", "app", "mon", "MONITORED_BY"),
  ],
  blast_radius: [],
  truncated: false,
};

describe("dependency traversal", () => {
  it("finds the blast radius (transitive dependents)", () => {
    expect([...dependentsOf(graph, "san")].sort()).toEqual(["app", "db"]);
    expect([...dependentsOf(graph, "host")]).toEqual(["app"]);
    expect(dependentsOf(graph, "mon").size).toBe(0);
  });

  it("finds upstream dependencies, honouring inverse relationship types", () => {
    expect([...dependenciesOf(graph, "app")].sort()).toEqual(["db", "host", "san"]);
  });

  it("terminates on cycles", () => {
    const cyclic = { edges: [edge("a", "x", "y", "DEPENDS_ON"), edge("b", "y", "x", "DEPENDS_ON")] };
    expect([...dependentsOf(cyclic, "x")]).toEqual(["y"]);
  });
});

describe("toElements", () => {
  it("builds nodes, edges and environment groups", () => {
    const els = toElements(graph, { groupBy: "environment" });
    const group = els.find((e) => e.data.id === "group:prod");
    expect(group?.classes).toBe("group");
    expect(els.find((e) => e.data.id === "db")?.data.parent).toBe("group:prod");
    expect(els.filter((e) => e.data.source).length).toBe(4);
  });

  it("dims everything outside the highlight set", () => {
    const els = toElements(graph, { highlight: new Set(["db", "app"]), focusId: "san" });
    const cls = (id: string) => els.find((e) => e.data.id === id)?.classes ?? "";
    expect(cls("san")).toContain("focus");
    expect(cls("db")).toContain("hl");
    expect(cls("host")).toContain("dim");
    expect(cls("e2")).not.toContain("dim");
    expect(cls("e3")).toContain("dim");
  });
});
