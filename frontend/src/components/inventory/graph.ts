import type { ElementDefinition } from "cytoscape";
import type { DependencyGraph, GraphEdge, RelationshipType } from "@/types/api";
import { HEALTH_META } from "@/components/inventory/shared";

/** Same semantics as the backend's dependency traversal. */
const FORWARD: RelationshipType[] = ["DEPENDS_ON", "RUNS_ON", "CONNECTS_TO", "STORES_DATA_IN"];
const INVERSE: RelationshipType[] = ["HOSTS", "EXPOSES"];

/** [dependent, dependency] for an edge, or null if it is not a dependency. */
export function dependencyPair(edge: GraphEdge): [string, string] | null {
  if (FORWARD.includes(edge.relationship_type)) return [edge.source_id, edge.target_id];
  if (INVERSE.includes(edge.relationship_type)) return [edge.target_id, edge.source_id];
  return null;
}

function walk(edges: GraphEdge[], start: string, downstream: boolean): Set<string> {
  const next = new Map<string, string[]>();
  for (const e of edges) {
    const pair = dependencyPair(e);
    if (!pair) continue;
    const [dependent, dependency] = pair;
    const [from, to] = downstream ? [dependency, dependent] : [dependent, dependency];
    next.set(from, [...(next.get(from) ?? []), to]);
  }
  const seen = new Set<string>();
  const queue = [start];
  while (queue.length) {
    const id = queue.shift()!;
    for (const n of next.get(id) ?? []) {
      if (n !== start && !seen.has(n)) {
        seen.add(n);
        queue.push(n);
      }
    }
  }
  return seen;
}

/** Everything that (transitively) depends on `id`: its blast radius. */
export function dependentsOf(graph: Pick<DependencyGraph, "edges">, id: string): Set<string> {
  return walk(graph.edges, id, true);
}

/** Everything `id` (transitively) depends on. */
export function dependenciesOf(graph: Pick<DependencyGraph, "edges">, id: string): Set<string> {
  return walk(graph.edges, id, false);
}

export interface ElementOptions {
  /** Node ids to emphasise (e.g. blast radius); others are dimmed when non-empty. */
  highlight?: Set<string>;
  focusId?: string | null;
  groupBy?: "environment" | "group_name" | null;
}

export function toElements(graph: DependencyGraph, opts: ElementOptions = {}): ElementDefinition[] {
  const { highlight, focusId, groupBy } = opts;
  const dimming = !!highlight && highlight.size > 0;
  const elements: ElementDefinition[] = [];
  const parents = new Set<string>();
  for (const n of graph.nodes) {
    const groupValue = groupBy ? n[groupBy] : null;
    const parent = groupValue ? `group:${groupValue}` : undefined;
    if (parent && !parents.has(parent)) {
      parents.add(parent);
      elements.push({ data: { id: parent, label: groupValue }, classes: "group" });
    }
    const classes = [
      `type-${n.resource_type}`,
      n.stale ? "stale" : "",
      n.id === focusId ? "focus" : "",
      dimming && n.id !== focusId ? (highlight!.has(n.id) ? "hl" : "dim") : "",
    ].filter(Boolean);
    elements.push({
      data: {
        id: n.id,
        label: n.name,
        parent,
        color: (HEALTH_META[n.health_status] ?? HEALTH_META.unknown).color,
        type: n.resource_type,
      },
      classes: classes.join(" "),
    });
  }
  const ids = new Set(graph.nodes.map((n) => n.id));
  for (const e of graph.edges) {
    if (!ids.has(e.source_id) || !ids.has(e.target_id)) continue;
    const touched = !dimming || (highlight!.has(e.source_id) || e.source_id === focusId) && (highlight!.has(e.target_id) || e.target_id === focusId);
    elements.push({
      data: {
        id: e.id,
        source: e.source_id,
        target: e.target_id,
        label: e.relationship_type.replace(/_/g, " ").toLowerCase(),
      },
      classes: [e.source === "discovered" ? "discovered" : "manual", touched ? "" : "dim"]
        .filter(Boolean)
        .join(" "),
    });
  }
  return elements;
}
