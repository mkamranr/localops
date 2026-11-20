import type { HealthStatus } from "@/types/api";

/** Filters that mean "find a specific resource": the grouped view would hide matches. */
export interface ViewFilter {
  q: string;
  type: string;
}

export type InventoryView = "grouped" | "all";

/** The view actually shown: searching by name or filtering on containers needs the flat list. */
export function effectiveView(requested: string | null, filter: ViewFilter): { view: InventoryView; forced: boolean } {
  const wanted: InventoryView = requested === "all" ? "all" : "grouped";
  if (wanted === "grouped" && (filter.q.trim() !== "" || filter.type === "docker_container")) {
    return { view: "all", forced: true };
  }
  return { view: wanted, forced: false };
}

const ATTENTION: HealthStatus[] = ["unhealthy", "degraded"];

/** "14 containers · 2 unhealthy, 1 degraded" */
export function childSummary(health: Partial<Record<string, number>>): string {
  const total = Object.values(health).reduce<number>((sum, n) => sum + (n ?? 0), 0);
  if (!total) return "";
  const base = `${total} container${total === 1 ? "" : "s"}`;
  const issues = ATTENTION.filter((h) => (health[h] ?? 0) > 0).map((h) => `${health[h]} ${h}`);
  return issues.length ? `${base} · ${issues.join(", ")}` : base;
}

/** Whether any child needs attention (drives the warning tone of the summary). */
export function childrenNeedAttention(health: Partial<Record<string, number>>): boolean {
  return ATTENTION.some((h) => (health[h] ?? 0) > 0);
}
