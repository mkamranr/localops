import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Network, X } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { resourcesApi } from "@/api/endpoints";
import { DependencyGraphView } from "@/components/inventory/DependencyGraphView";
import { dependenciesOf, dependentsOf, type ElementOptions } from "@/components/inventory/graph";
import { HEALTH_META, HealthBadge, RESOURCE_TYPES, RELATIONSHIP_META, TYPE_META, TypeIcon } from "@/components/inventory/shared";
import { Button } from "@/components/ui/button";
import { Badge, EmptyState, ErrorState, Spinner } from "@/components/ui/display";
import { Select } from "@/components/ui/form";
import type { HealthStatus } from "@/types/api";

type Mode = "blast" | "upstream" | "none";

export function DependenciesPage() {
  const [environment, setEnvironment] = useState("");
  const [group, setGroup] = useState("");
  const [type, setType] = useState("");
  const [includeStale, setIncludeStale] = useState(false);
  const [groupBy, setGroupBy] = useState<"" | "environment" | "group_name">("environment");
  const [layout, setLayout] = useState<"cose" | "breadthfirst">("cose");
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("blast");

  const facets = useQuery({ queryKey: ["resources", "facets"], queryFn: resourcesApi.facets });
  const query = useQuery({
    queryKey: ["resources", "graph", environment, group, type, includeStale],
    queryFn: () =>
      resourcesApi.graph({
        environment: environment || undefined,
        group: group || undefined,
        type: type || undefined,
        include_stale: includeStale,
      }),
  });
  const graph = query.data;

  const highlight = useMemo(() => {
    if (!graph || !selected || mode === "none") return undefined;
    return mode === "blast" ? dependentsOf(graph, selected) : dependenciesOf(graph, selected);
  }, [graph, selected, mode]);

  const options: ElementOptions = useMemo(
    () => ({ highlight, focusId: selected ?? undefined, groupBy: groupBy || undefined }),
    [highlight, selected, groupBy],
  );

  const node = graph?.nodes.find((n) => n.id === selected) ?? null;
  const names = useMemo(() => new Map(graph?.nodes.map((n) => [n.id, n]) ?? []), [graph]);
  const edges = graph && selected ? graph.edges.filter((e) => e.source_id === selected || e.target_id === selected) : [];
  const affected = highlight ? [...highlight].map((id) => names.get(id)).filter((n) => n !== undefined) : [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Dependencies</h1>
        <p className="text-sm text-muted">How resources depend on each other. Select a node to see its blast radius or what it relies on.</p>
      </div>

      <div className="panel flex flex-wrap items-center gap-2 p-3">
        <Select aria-label="Environment" className="w-48" value={environment} onChange={(e) => setEnvironment(e.target.value)}>
          <option value="">All environments</option>
          {facets.data?.environments.map((v) => <option key={v} value={v}>{v}</option>)}
        </Select>
        {!!facets.data?.groups.length && (
          <Select aria-label="Group" className="w-36" value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">All groups</option>
            {facets.data.groups.map((v) => <option key={v} value={v}>{v}</option>)}
          </Select>
        )}
        <Select aria-label="Type" className="w-40" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">All types</option>
          {RESOURCE_TYPES.map((t) => <option key={t} value={t}>{TYPE_META[t].label}</option>)}
        </Select>
        <Select aria-label="Group by" className="w-56" value={groupBy} onChange={(e) => setGroupBy(e.target.value as typeof groupBy)}>
          <option value="environment">Group by environment</option>
          <option value="group_name">Group by group</option>
          <option value="">No grouping</option>
        </Select>
        <Select aria-label="Layout" className="w-44" value={layout} onChange={(e) => setLayout(e.target.value as typeof layout)}>
          <option value="cose">Force layout</option>
          <option value="breadthfirst">Layered layout</option>
        </Select>
        <label className="flex items-center gap-2 px-2 text-sm">
          <input type="checkbox" className="size-4" checked={includeStale} onChange={(e) => setIncludeStale(e.target.checked)} /> Show stale
        </label>
        <div className="ml-auto flex items-center gap-3 text-xs text-muted">
          {(Object.keys(HEALTH_META) as HealthStatus[]).map((h) => (
            <span key={h} className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-full" style={{ background: HEALTH_META[h].color }} />{HEALTH_META[h].label}</span>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_20rem]">
        <div className="panel p-3">
          {query.isLoading ? (
            <Spinner />
          ) : query.error || !graph ? (
            <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />
          ) : !graph.nodes.length ? (
            <EmptyState icon={<Network className="size-8" />} title="Nothing to show" description="Run discovery or add resources and relationships to build the dependency map." />
          ) : (
            <>
              <DependencyGraphView graph={graph} options={options} layout={layout} height={620} onSelect={setSelected} />
              <p className="mt-2 text-xs text-subtle">
                {graph.nodes.length} resources · {graph.edges.length} relationships · blue edges were added manually
                {graph.truncated && <span className="text-amber-300"> · truncated, narrow the filters to see everything</span>}
              </p>
            </>
          )}
        </div>

        <aside className="panel p-5">
          {!node ? (
            <div className="text-sm text-muted">
              <p className="font-medium text-fg">No resource selected</p>
              <p className="mt-1">Click a node in the map. Its blast radius (everything that depends on it, directly or transitively) is highlighted.</p>
            </div>
          ) : (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2"><TypeIcon type={node.resource_type} /><h2 className="truncate font-semibold">{node.name}</h2></div>
                  <p className="mt-0.5 text-xs text-muted">{TYPE_META[node.resource_type]?.label}{node.environment ? ` · ${node.environment}` : ""}{node.group_name ? ` · ${node.group_name}` : ""}</p>
                </div>
                <Button variant="ghost" size="icon" aria-label="Clear selection" onClick={() => setSelected(null)}><X /></Button>
              </div>
              <HealthBadge status={node.health_status} stale={node.stale} />
              <div className="flex rounded-lg border border-border p-0.5 text-xs">
                {(["blast", "upstream", "none"] as const).map((m) => (
                  <button key={m} type="button" onClick={() => setMode(m)} className={`flex-1 rounded-md px-2 py-1 ${mode === m ? "bg-surface-2 text-fg" : "text-muted hover:text-fg"}`}>
                    {m === "blast" ? "Blast radius" : m === "upstream" ? "Depends on" : "Off"}
                  </button>
                ))}
              </div>
              {mode !== "none" && (
                <div>
                  <h3 className="mb-1.5 text-xs font-medium text-muted">
                    {mode === "blast" ? `${affected.length} resource${affected.length === 1 ? "" : "s"} affected if this fails` : `Relies on ${affected.length} resource${affected.length === 1 ? "" : "s"}`}
                  </h3>
                  <ul className="max-h-56 space-y-1 overflow-y-auto text-sm">
                    {affected.map((n) => (
                      <li key={n.id} className="flex items-center gap-2">
                        <span className="size-2 shrink-0 rounded-full" style={{ background: HEALTH_META[n.health_status].color }} />
                        <button type="button" className="truncate text-left hover:underline" onClick={() => setSelected(n.id)}>{n.name}</button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {!!edges.length && (
                <div>
                  <h3 className="mb-1.5 text-xs font-medium text-muted">Direct relationships</h3>
                  <ul className="space-y-1 text-xs">
                    {edges.map((e) => (
                      <li key={e.id}>
                        {names.get(e.source_id)?.name} <span className="text-muted">{RELATIONSHIP_META[e.relationship_type].label}</span> {names.get(e.target_id)?.name}
                        {e.source === "manual" && <Badge tone="primary">manual</Badge>}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <Link to={`/infrastructure/${node.id}`} className="inline-flex items-center gap-1.5 text-sm text-blue-300 hover:underline">
                Open resource <ExternalLink className="size-3.5" />
              </Link>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
