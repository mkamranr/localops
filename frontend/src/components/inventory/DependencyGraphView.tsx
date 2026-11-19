import cytoscape, { type Core, type StylesheetJson } from "cytoscape";
import { useEffect, useRef } from "react";
import { toElements, type ElementOptions } from "@/components/inventory/graph";
import type { DependencyGraph } from "@/types/api";

const SHAPES: Record<string, string> = {
  linux_server: "round-rectangle",
  windows_server: "round-rectangle",
  docker_host: "round-rectangle",
  docker_container: "ellipse",
  http_service: "diamond",
  application: "round-hexagon",
  database: "barrel",
  network_device: "round-triangle",
  other: "round-octagon",
};

const STYLE: StylesheetJson = [
  {
    selector: "node",
    style: {
      label: "data(label)",
      color: "#e6ecf7",
      "font-size": 11,
      "text-valign": "bottom",
      "text-margin-y": 6,
      "text-wrap": "ellipsis",
      "text-max-width": "120px",
      "background-color": "#121c31",
      "border-width": 3,
      "border-color": "data(color)",
      width: 34,
      height: 34,
    },
  },
  ...Object.entries(SHAPES).map(([type, shape]) => ({
    selector: `node.type-${type}`,
    style: { shape } as Record<string, string>,
  })),
  { selector: "node.type-docker_container", style: { width: 26, height: 26 } },
  { selector: "node.stale", style: { "border-style": "dashed", opacity: 0.6 } },
  {
    selector: "node.focus",
    style: { "background-color": "#1d4ed8", "border-color": "#93c5fd", width: 44, height: 44 },
  },
  { selector: "node.hl", style: { "background-color": "#3b1d1d", "overlay-opacity": 0 } },
  { selector: ".dim", style: { opacity: 0.18 } },
  {
    selector: "node.group",
    style: {
      shape: "round-rectangle",
      "background-color": "#0d1526",
      "background-opacity": 0.6,
      "border-width": 1,
      "border-color": "#2c3b5c",
      "border-style": "dashed",
      label: "data(label)",
      "text-valign": "top",
      "text-halign": "center",
      color: "#8a99b8",
      "font-size": 12,
      padding: "18px",
    },
  },
  {
    selector: "edge",
    style: {
      width: 1.5,
      "line-color": "#2c3b5c",
      "target-arrow-color": "#2c3b5c",
      "target-arrow-shape": "triangle",
      "curve-style": "bezier",
      label: "data(label)",
      "font-size": 8,
      color: "#5d6c8c",
      "text-rotation": "autorotate",
      "text-background-color": "#070d1a",
      "text-background-opacity": 0.8,
      "text-background-padding": "1px",
    },
  },
  { selector: "edge.manual", style: { "line-color": "#3b82f6", "target-arrow-color": "#3b82f6" } },
  { selector: "edge.discovered", style: { "line-style": "solid" } },
  { selector: ":selected", style: { "border-color": "#ffffff", "border-width": 4 } },
];

export function DependencyGraphView({
  graph,
  options,
  layout = "cose",
  height = 520,
  onSelect,
}: {
  graph: DependencyGraph;
  options?: ElementOptions;
  layout?: "cose" | "breadthfirst";
  height?: number;
  onSelect?: (id: string | null) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const cy = useRef<Core | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  useEffect(() => {
    if (!container.current) return;
    const instance = cytoscape({
      container: container.current,
      style: STYLE,
      minZoom: 0.2,
      maxZoom: 3,
      wheelSensitivity: 0.3,
    });
    instance.on("tap", (evt) => {
      const target = evt.target;
      if (target === instance) onSelectRef.current?.(null);
      else if (target.isNode?.() && !target.hasClass("group")) onSelectRef.current?.(target.id());
    });
    cy.current = instance;
    return () => {
      instance.destroy();
      cy.current = null;
    };
  }, []);

  // Rebuild elements and re-run the layout only when the graph itself changes.
  const structure = `${graph.nodes.map((n) => n.id).join(",")}|${graph.edges.map((e) => e.id).join(",")}|${options?.groupBy ?? ""}`;
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    instance.elements().remove();
    instance.add(toElements(graph, options));
    const focus = options?.focusId;
    instance
      .layout(
        layout === "breadthfirst"
          ? { name: "breadthfirst", directed: true, padding: 30, spacingFactor: 1.2, roots: focus ? [focus] : undefined, animate: false }
          : { name: "cose", padding: 30, animate: false, nodeRepulsion: () => 9000, idealEdgeLength: () => 90 },
      )
      .run();
    instance.fit(undefined, 30);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structure, layout]);

  // Highlighting changes only update classes, keeping positions stable.
  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    const fresh = toElements(graph, options);
    instance.batch(() => {
      for (const el of fresh) {
        const existing = instance.getElementById(String(el.data.id));
        if (existing.nonempty()) existing.classes(el.classes ?? "");
      }
    });
  }, [graph, options]);

  return (
    <div
      ref={container}
      className="w-full rounded-lg border border-border bg-bg/60"
      style={{ height }}
      data-testid="dependency-graph"
    />
  );
}
