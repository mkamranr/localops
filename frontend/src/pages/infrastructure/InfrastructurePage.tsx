import { useQuery } from "@tanstack/react-query";
import { Activity, AlertTriangle, CheckCircle2, Clock, Plus, Radar, Server, type LucideIcon } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { integrationsApi, resourcesApi, type ResourceQuery } from "@/api/endpoints";
import { ResourceDialog } from "@/components/inventory/ResourceDialog";
import { HEALTH_META, HealthBadge, RESOURCE_TYPES, TYPE_META, TypeIcon } from "@/components/inventory/shared";
import { Button } from "@/components/ui/button";
import { Badge, EmptyState, ErrorState, Pagination, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Input, Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { HealthStatus } from "@/types/api";
import { formatRelative } from "@/utils/format";

const PAGE_SIZE = 25;
const FILTERS = ["q", "type", "environment", "health", "group", "tag", "integration_id", "stale"] as const;

function StatCard({ label, value, hint, icon: Icon, tone }: { label: string; value: string | number; hint?: string; icon: LucideIcon; tone: string }) {
  return (
    <div className="panel flex items-center justify-between p-5">
      <div className="min-w-0">
        <div className="text-xs text-muted">{label}</div>
        <div className="mt-1 truncate text-2xl font-semibold">{value}</div>
        {hint && <div className="mt-0.5 truncate text-xs text-subtle">{hint}</div>}
      </div>
      <span className={`grid size-10 shrink-0 place-items-center rounded-xl ${tone}`}>
        <Icon className="size-5" />
      </span>
    </div>
  );
}

export function InfrastructurePage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const canCreate = useAuthStore((s) => s.hasPermission("resource.create"));
  const canIntegrations = useAuthStore((s) => s.hasPermission("integration.read"));
  const page = Number(params.get("page") ?? 1) || 1;

  const filter = Object.fromEntries(FILTERS.map((k) => [k, params.get(k) ?? ""])) as Record<(typeof FILTERS)[number], string>;
  const query: ResourceQuery = {
    page,
    page_size: PAGE_SIZE,
    q: filter.q || undefined,
    type: filter.type || undefined,
    environment: filter.environment || undefined,
    health: filter.health || undefined,
    group: filter.group || undefined,
    tag: filter.tag || undefined,
    integration_id: filter.integration_id || undefined,
    stale: filter.stale === "" ? undefined : filter.stale === "true",
  };
  const update = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setParams(next, { replace: true });
  };

  const summary = useQuery({ queryKey: ["resources", "summary"], queryFn: resourcesApi.summary, refetchInterval: 30_000 });
  const facets = useQuery({ queryKey: ["resources", "facets"], queryFn: resourcesApi.facets });
  const list = useQuery({ queryKey: ["resources", "list", query], queryFn: () => resourcesApi.list(query), placeholderData: (prev) => prev });
  const integrations = useQuery({ queryKey: ["integrations"], queryFn: integrationsApi.list, enabled: canIntegrations });
  const integrationName = (id: string | null) => integrations.data?.find((i) => i.id === id)?.name;

  const s = summary.data;
  const health = (h: HealthStatus) => s?.by_health[h] ?? 0;
  const filtered = FILTERS.some((k) => filter[k]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Infrastructure</h1>
          <p className="text-sm text-muted">Servers, containers and services discovered by integrations or added by hand.</p>
        </div>
        <div className="flex gap-2">
          {canIntegrations && (
            <Button variant="secondary" onClick={() => navigate("/integrations")}><Radar /> Run discovery</Button>
          )}
          {canCreate && <Button onClick={() => setAdding(true)}><Plus /> Add resource</Button>}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Resources" value={s?.total ?? "—"} hint={s ? `${s.integrations} integration${s.integrations === 1 ? "" : "s"}` : undefined} icon={Server} tone="bg-blue-600/20 text-blue-300" />
        <StatCard label="Healthy" value={s ? health("healthy") : "—"} hint={s ? `${health("unknown")} unknown` : undefined} icon={CheckCircle2} tone="bg-emerald-600/20 text-emerald-300" />
        <StatCard label="Degraded / unhealthy" value={s ? `${health("degraded")} / ${health("unhealthy")}` : "—"} hint={s?.stale ? `${s.stale} stale` : undefined} icon={AlertTriangle} tone="bg-amber-600/20 text-amber-300" />
        <StatCard label="Last discovery" value={s?.last_discovery_at ? formatRelative(s.last_discovery_at) : "Never"} icon={Clock} tone="bg-violet-600/20 text-violet-300" />
      </div>

      <div className="panel">
        <div className="flex flex-wrap items-center gap-2 border-b border-border p-3">
          <Input
            aria-label="Search"
            placeholder="Search name, hostname or IP…"
            className="w-64"
            defaultValue={filter.q}
            onKeyDown={(e) => e.key === "Enter" && update("q", (e.target as HTMLInputElement).value.trim())}
            onBlur={(e) => e.target.value.trim() !== filter.q && update("q", e.target.value.trim())}
          />
          <Select aria-label="Type" className="w-44" value={filter.type} onChange={(e) => update("type", e.target.value)}>
            <option value="">All types</option>
            {RESOURCE_TYPES.map((t) => <option key={t} value={t}>{TYPE_META[t].label}</option>)}
          </Select>
          <Select aria-label="Health" className="w-36" value={filter.health} onChange={(e) => update("health", e.target.value)}>
            <option value="">Any health</option>
            {(Object.keys(HEALTH_META) as HealthStatus[]).map((h) => <option key={h} value={h}>{HEALTH_META[h].label}</option>)}
          </Select>
          <Select aria-label="Environment" className="w-48" value={filter.environment} onChange={(e) => update("environment", e.target.value)}>
            <option value="">All environments</option>
            {facets.data?.environments.map((v) => <option key={v} value={v}>{v}</option>)}
          </Select>
          {!!facets.data?.groups.length && (
            <Select aria-label="Group" className="w-36" value={filter.group} onChange={(e) => update("group", e.target.value)}>
              <option value="">All groups</option>
              {facets.data.groups.map((v) => <option key={v} value={v}>{v}</option>)}
            </Select>
          )}
          {!!facets.data?.tags.length && (
            <Select aria-label="Tag" className="w-36" value={filter.tag} onChange={(e) => update("tag", e.target.value)}>
              <option value="">Any tag</option>
              {facets.data.tags.map((v) => <option key={v} value={v}>{v}</option>)}
            </Select>
          )}
          <Select aria-label="Staleness" className="w-44" value={filter.stale} onChange={(e) => update("stale", e.target.value)}>
            <option value="">Current &amp; stale</option>
            <option value="false">Current only</option>
            <option value="true">Stale only</option>
          </Select>
          {filter.integration_id && (
            <Badge tone="primary">
              {integrationName(filter.integration_id) ?? "Integration"}
              <button type="button" className="ml-1" aria-label="Clear integration filter" onClick={() => update("integration_id", "")}>×</button>
            </Badge>
          )}
          {filtered && (
            <Button variant="ghost" size="sm" onClick={() => setParams(new URLSearchParams(), { replace: true })}>Clear filters</Button>
          )}
        </div>

        {list.isLoading ? (
          <Spinner />
        ) : list.error || !list.data ? (
          <ErrorState message={errorMessage(list.error)} onRetry={() => list.refetch()} />
        ) : !list.data.items.length ? (
          <EmptyState
            icon={<Activity className="size-8" />}
            title={filtered ? "No resources match these filters" : "No resources yet"}
            description={filtered ? "Try clearing some filters." : "Add an integration under Tools & Integrations and run discovery, or add a resource by hand."}
          />
        ) : (
          <>
            <Table>
              <thead>
                <tr><Th>Name</Th><Th>Type</Th><Th>Health</Th><Th>Address</Th><Th>Environment</Th><Th>Source</Th><Th>Last seen</Th></tr>
              </thead>
              <tbody>
                {list.data.items.map((r) => (
                  <tr key={r.id} className={`cursor-pointer hover:bg-surface-2/60 ${r.stale ? "opacity-60" : ""}`} onClick={() => navigate(`/infrastructure/${r.id}`)}>
                    <Td>
                      <Link to={`/infrastructure/${r.id}`} className="font-medium hover:underline" onClick={(e) => e.stopPropagation()}>{r.name}</Link>
                      {r.health_reasons.length > 0 && <div className="mt-0.5 max-w-xs truncate text-xs text-muted" title={r.health_reasons.join("; ")}>{r.health_reasons[0]}</div>}
                    </Td>
                    <Td><span className="inline-flex items-center gap-2 text-xs text-muted"><TypeIcon type={r.resource_type} /> {TYPE_META[r.resource_type]?.label ?? r.resource_type}</span></Td>
                    <Td><HealthBadge status={r.health_status} stale={r.stale} /></Td>
                    <Td className="font-mono text-xs text-muted">{r.ip_addresses[0] ?? r.hostname ?? "—"}{r.ip_addresses.length > 1 ? ` +${r.ip_addresses.length - 1}` : ""}</Td>
                    <Td className="text-xs">
                      {r.environment ? <Badge>{r.environment}</Badge> : <span className="text-subtle">—</span>}
                      {r.group_name && <span className="ml-1.5 text-muted">{r.group_name}</span>}
                    </Td>
                    <Td className="text-xs text-muted">{r.source === "manual" ? "Manual" : integrationName(r.integration_id) ?? "Discovered"}</Td>
                    <Td className="whitespace-nowrap text-xs text-muted">{r.source === "manual" ? "—" : formatRelative(r.last_seen_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination page={page} pageSize={PAGE_SIZE} total={list.data.total} onPage={(p) => update("page", String(p))} />
          </>
        )}
      </div>
      {adding && <ResourceDialog resource={null} onClose={() => setAdding(false)} onSaved={(r) => navigate(`/infrastructure/${r.id}`)} />}
    </div>
  );
}
