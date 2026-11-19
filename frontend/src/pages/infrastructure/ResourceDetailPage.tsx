import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, HeartPulse, Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { resourcesApi } from "@/api/endpoints";
import { DependencyGraphView } from "@/components/inventory/DependencyGraphView";
import { ResourceIncidents, ResourceLogs } from "@/components/inventory/ResourceDiagnostics";
import { ResourceDialog } from "@/components/inventory/ResourceDialog";
import { HealthBadge, percentTone, RELATIONSHIP_META, Sparkline, TYPE_META, TypeIcon } from "@/components/inventory/shared";
import { Button } from "@/components/ui/button";
import { Alert, Badge, EmptyState, ErrorState, Pagination, Spinner, StatusDot, Table, Td, Th } from "@/components/ui/display";
import { Field, Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { GraphNode, MetricSample, RelationshipType, Resource } from "@/types/api";
import { formatDateTime, formatRelative } from "@/utils/format";

type Facts = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const TABS = ["overview", "metrics", "services", "dependencies", "history", "logs", "incidents"] as const;
type Tab = (typeof TABS)[number];

function formatUptime(seconds: number | null | undefined) {
  if (!seconds) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  return d ? `${d}d ${h}h` : `${h}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-3 py-1.5 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children ?? "—"}</dd>
    </div>
  );
}

function Section({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <section className="panel p-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

// ------------------------------------------------------------------ overview

function Overview({ resource }: { resource: Resource }) {
  const f = resource.facts as Facts;
  const container = f.container as Facts | undefined;
  const http = f.http as Facts | undefined;
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Section title="Details">
        <dl>
          <Row label="Type">{TYPE_META[resource.resource_type]?.label}</Row>
          <Row label="Hostname">{resource.hostname}</Row>
          <Row label="IP addresses"><span className="font-mono text-xs">{resource.ip_addresses.join(", ") || "—"}</span></Row>
          <Row label="OS">{resource.os}</Row>
          <Row label="Environment">{resource.environment}</Row>
          <Row label="Group">{resource.group_name}</Row>
          <Row label="Owner">{resource.owner}</Row>
          <Row label="Tags">{resource.tags.length ? <span className="flex flex-wrap gap-1">{resource.tags.map((t) => <Badge key={t}>{t}</Badge>)}</span> : "—"}</Row>
          <Row label="Source">
            {resource.source === "manual" ? "Added manually" : <>Discovered by {resource.integration_name ?? "an integration"}{resource.discovery_target && <span className="ml-1 font-mono text-xs text-muted">({resource.discovery_target})</span>}</>}
          </Row>
          <Row label="Last seen">{resource.last_seen_at ? `${formatRelative(resource.last_seen_at)} · ${formatDateTime(resource.last_seen_at)}` : "—"}</Row>
          {resource.description && <Row label="Description"><span className="whitespace-pre-wrap">{resource.description}</span></Row>}
        </dl>
      </Section>

      <Section title="Health">
        <div className="mb-3 flex items-center gap-3">
          <HealthBadge status={resource.health_status} stale={resource.stale} />
          <span className="text-xs text-muted">checked {formatRelative(resource.last_health_at)}</span>
        </div>
        {resource.health_reasons.length ? (
          <ul className="space-y-1.5 text-sm">
            {resource.health_reasons.map((r) => <li key={r} className="flex gap-2"><span className="text-amber-300">•</span>{r}</li>)}
          </ul>
        ) : (
          <p className="text-sm text-muted">{resource.health_status === "unknown" ? "No health data yet. Manual resources have no automatic health checks." : "No problems detected."}</p>
        )}
        {resource.stale && <div className="mt-3"><Alert tone="warning">This resource was not found in the latest discovery run. It may have been removed or renamed.</Alert></div>}
      </Section>

      {(f.os || f.cpu_count || f.memory) && (
        <Section title="System">
          <dl>
            <Row label="Kernel / build">{f.kernel ?? f.os?.build}</Row>
            {f.domain && <Row label="Domain">{f.domain}</Row>}
            <Row label="Uptime">{formatUptime(f.uptime_seconds)}</Row>
            <Row label="CPUs">{f.cpu_count}</Row>
            {f.load && <Row label="Load (1/5/15m)">{(f.load as number[]).join(" / ")}</Row>}
            {f.cpu_percent != null && <Row label="CPU">{f.cpu_percent}%</Row>}
            {f.memory && (
              <Row label="Memory">
                <span className={percentTone(f.memory.used_percent)}>{f.memory.used_percent ?? "—"}%</span>
                <span className="text-muted"> used of {f.memory.total_mb ? `${(f.memory.total_mb / 1024).toFixed(1)} GB` : "—"}</span>
              </Row>
            )}
            {f.docker && (
              <Row label="Docker">
                {!f.docker.installed ? "not installed" : !f.docker.accessible ? `installed, not accessible (${f.docker.status ?? "permission denied"})` : `${f.docker.server_version ?? ""} · ${f.docker.running ?? 0} running / ${f.docker.containers ?? 0} containers`}
              </Row>
            )}
          </dl>
        </Section>
      )}

      {Array.isArray(f.disks) && f.disks.length > 0 && (
        <Section title="Disks">
          <ul className="space-y-2.5">
            {(f.disks as Facts[]).map((d) => (
              <li key={d.mount} className="text-sm">
                <div className="flex justify-between gap-3">
                  <span className="font-mono text-xs">{d.mount} <span className="text-subtle">{d.type}</span></span>
                  <span className={`text-xs ${percentTone(d.used_percent)}`}>{d.used_percent}% · {d.used_gb} / {d.size_gb} GB</span>
                </div>
                <div className="mt-1 h-1.5 rounded-full bg-surface-2">
                  <div className={`h-1.5 rounded-full ${d.used_percent >= 90 ? "bg-red-500" : d.used_percent >= 75 ? "bg-amber-500" : "bg-emerald-500"}`} style={{ width: `${Math.min(100, d.used_percent)}%` }} />
                </div>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {container && (
        <Section title="Container">
          <dl>
            <Row label="Image"><span className="font-mono text-xs">{container.image}</span></Row>
            <Row label="State">{container.state}{container.health ? ` · ${container.health}` : ""}{container.state === "exited" ? ` (exit ${container.exit_code})` : ""}</Row>
            <Row label="Host">{container.host}</Row>
            <Row label="Started">{container.started_at ? formatDateTime(container.started_at) : "—"}</Row>
            <Row label="Restarts">{container.restart_count} {container.restart_policy && <span className="text-muted">(policy {container.restart_policy})</span>}</Row>
            {container.compose?.project && <Row label="Compose">{container.compose.project} / {container.compose.service}</Row>}
            <Row label="Ports">
              <span className="font-mono text-xs">
                {(container.ports as Facts[]).map((p) => (p.host_port ? `${p.host_port}→${p.container_port}/${p.protocol}` : `${p.container_port}/${p.protocol}`)).join(", ") || "—"}
              </span>
            </Row>
            <Row label="Networks"><span className="font-mono text-xs">{Object.entries(container.networks ?? {}).map(([n, ip]) => `${n}${ip ? ` (${ip})` : ""}`).join(", ") || "—"}</span></Row>
          </dl>
        </Section>
      )}

      {http && (
        <Section title="HTTP check">
          <dl>
            <Row label="URL"><span className="font-mono text-xs">{http.url}</span></Row>
            <Row label="Status">{http.status_code ?? "no response"} <span className="text-muted">(expected {Array.isArray(http.expected_status) ? http.expected_status.join(", ") : http.expected_status})</span></Row>
            <Row label="Latency">{http.latency_ms != null ? `${http.latency_ms} ms` : "—"}</Row>
            {http.error && <Row label="Error"><span className="text-red-300">{http.error}</span></Row>}
            <Row label="Resolved to"><span className="font-mono text-xs">{(http.resolved_ips ?? []).join(", ") || "—"}</span></Row>
            {http.tls && (
              <>
                <Row label="Certificate">{http.tls.subject}</Row>
                <Row label="Expires"><span className={http.tls.days_left < 14 ? "text-red-300" : http.tls.days_left < 30 ? "text-amber-300" : ""}>{formatDateTime(http.tls.expires_at)} ({http.tls.days_left} days)</span></Row>
              </>
            )}
          </dl>
        </Section>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ metrics

function Metrics({ id }: { id: string }) {
  const [hours, setHours] = useState(168);
  const query = useQuery({ queryKey: ["resources", id, "metrics", hours], queryFn: () => resourcesApi.metrics(id, hours) });
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} />;
  const samples = query.data;
  if (!samples.length) return <div className="panel"><EmptyState title="No metric samples" description="Samples are taken on every discovery run and health check." /></div>;
  const last = samples[samples.length - 1] as MetricSample;
  const series: { key: keyof MetricSample; label: string; unit: string; max?: number; color: string }[] = [
    { key: "cpu_load", label: "CPU load (1m)", unit: "", color: "#3b82f6" },
    { key: "memory_percent", label: "Memory used", unit: "%", max: 100, color: "#a855f7" },
    { key: "disk_percent", label: "Fullest disk", unit: "%", max: 100, color: "#f59e0b" },
    { key: "latency_ms", label: "Latency", unit: " ms", color: "#22c55e" },
  ];
  const shown = series.filter((s) => samples.some((m) => m[s.key] != null));
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted">{samples.length} samples · latest {formatRelative(last.taken_at)}</p>
        <Select aria-label="Range" className="w-36" value={String(hours)} onChange={(e) => setHours(Number(e.target.value))}>
          <option value="24">Last 24 hours</option>
          <option value="168">Last 7 days</option>
          <option value="720">Last 30 days</option>
        </Select>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {shown.map((s) => {
          const value = last[s.key] as number | null;
          return (
            <div key={s.key} className="panel p-5">
              <div className="flex items-baseline justify-between">
                <span className="text-xs text-muted">{s.label}</span>
                <span className="text-lg font-semibold">{value != null ? `${value}${s.unit}` : "—"}{s.key === "cpu_load" && last.cpu_count ? <span className="text-xs text-muted"> / {last.cpu_count} CPUs</span> : null}</span>
              </div>
              <div className="mt-3"><Sparkline values={samples.map((m) => m[s.key] as number | null)} max={s.max} color={s.color} /></div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ services

function Services({ resource }: { resource: Resource }) {
  const f = resource.facts as Facts;
  const children = useQuery({
    queryKey: ["resources", resource.id, "dependencies", "down", 1],
    queryFn: () => resourcesApi.dependencies(resource.id, "down", 1),
    enabled: ["linux_server", "docker_host"].includes(resource.resource_type),
  });
  const containers = (children.data?.nodes ?? []).filter((n) => n.resource_type === "docker_container");
  const services = (f.services ?? []) as Facts[];
  const failed = [...((f.failed_services ?? []) as string[]), ...((f.stopped_auto_services ?? []) as Facts[]).map((s) => s.name as string)];
  const ports = (f.listening_ports ?? []) as Facts[];
  const outbound = (f.connections?.outbound ?? []) as Facts[];
  if (!services.length && !ports.length && !containers.length && !failed.length) {
    return <div className="panel"><EmptyState title="No services collected" description="Services, ports and containers appear here after discovery of a Linux, Windows or Docker host." /></div>;
  }
  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      {!!containers.length && (
        <Section title={`Containers (${containers.length})`}>
          <ul className="divide-y divide-border/60 text-sm">
            {containers.map((c) => (
              <li key={c.id} className="flex items-center justify-between gap-3 py-2">
                <Link to={`/infrastructure/${c.id}`} className="hover:underline">{c.name}</Link>
                <HealthBadge status={c.health_status} stale={c.stale} />
              </li>
            ))}
          </ul>
        </Section>
      )}
      {!!failed.length && (
        <Section title="Failed / stopped services">
          <ul className="space-y-1 text-sm">{failed.map((s) => <li key={s} className="flex items-center gap-2"><StatusDot tone="danger" /> <span className="font-mono text-xs">{s}</span></li>)}</ul>
        </Section>
      )}
      {!!ports.length && (
        <Section title={`Listening ports (${ports.length})`}>
          <div className="flex flex-wrap gap-1.5">
            {ports.map((p) => <Badge key={p.port} title={(p.addresses as string[]).join(", ")}>{p.port}</Badge>)}
          </div>
          {!!outbound.length && (
            <>
              <h3 className="mt-4 mb-2 text-xs font-medium text-muted">Outbound connections</h3>
              <ul className="space-y-1 font-mono text-xs">
                {outbound.slice(0, 30).map((c) => <li key={`${c.remote_ip}:${c.remote_port}`}>{c.remote_ip}:{c.remote_port} <span className="text-subtle">×{c.count}</span></li>)}
              </ul>
            </>
          )}
        </Section>
      )}
      {!!services.length && (
        <Section title={`Running services (${services.length})`}>
          <div className="max-h-96 overflow-y-auto">
            <Table>
              <tbody>
                {services.map((s) => (
                  <tr key={s.name}><Td className="py-1.5 font-mono text-xs">{s.name}</Td><Td className="py-1.5 text-xs text-muted">{s.description}</Td></tr>
                ))}
              </tbody>
            </Table>
          </div>
        </Section>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ dependencies

function AddRelationship({ resource, onDone }: { resource: Resource; onDone: () => void }) {
  const [direction, setDirection] = useState<"out" | "in">("out");
  const [type, setType] = useState<RelationshipType>("DEPENDS_ON");
  const [other, setOther] = useState("");
  const candidates = useQuery({ queryKey: ["resources", "list", "picker"], queryFn: () => resourcesApi.list({ page: 1, page_size: 200 }) });
  const mutation = useMutation({
    mutationFn: () =>
      resourcesApi.addRelationship(
        direction === "out"
          ? { source_id: resource.id, target_id: other, relationship_type: type }
          : { source_id: other, target_id: resource.id, relationship_type: type },
      ),
    onSuccess: () => { setOther(""); onDone(); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); if (other) mutation.mutate(); };
  return (
    <form onSubmit={submit} className="space-y-3">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-[8rem_12rem_1fr_auto] md:items-end">
        <Field label="Direction" htmlFor="rel-dir">
          <Select id="rel-dir" value={direction} onChange={(e) => setDirection(e.target.value as "out" | "in")}>
            <option value="out">{resource.name} →</option>
            <option value="in">→ {resource.name}</option>
          </Select>
        </Field>
        <Field label="Relationship" htmlFor="rel-type">
          <Select id="rel-type" value={type} onChange={(e) => setType(e.target.value as RelationshipType)}>
            {(Object.keys(RELATIONSHIP_META) as RelationshipType[]).map((t) => <option key={t} value={t}>{RELATIONSHIP_META[t].label}</option>)}
          </Select>
        </Field>
        <Field label={direction === "out" ? "Target" : "Source"} htmlFor="rel-other">
          <Select id="rel-other" value={other} onChange={(e) => setOther(e.target.value)} required>
            <option value="">Select a resource…</option>
            {candidates.data?.items.filter((r) => r.id !== resource.id).map((r) => <option key={r.id} value={r.id}>{r.name} ({TYPE_META[r.resource_type]?.label})</option>)}
          </Select>
        </Field>
        <Button type="submit" loading={mutation.isPending} disabled={!other}><Plus /> Add</Button>
      </div>
      <p className="text-xs text-subtle">{direction === "out" ? `${resource.name} ${RELATIONSHIP_META[type].label} the selected resource.` : `The selected resource ${RELATIONSHIP_META[type].label} ${resource.name}.`} {RELATIONSHIP_META[type].hint}.</p>
      {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
    </form>
  );
}

function NodeList({ nodes, empty }: { nodes: GraphNode[]; empty: string }) {
  if (!nodes.length) return <p className="text-xs text-subtle">{empty}</p>;
  return (
    <ul className="divide-y divide-border/60 text-sm">
      {nodes.map((n) => (
        <li key={n.id} className="flex items-center justify-between gap-3 py-2">
          <span className="flex min-w-0 items-center gap-2">
            <TypeIcon type={n.resource_type} />
            <Link to={`/infrastructure/${n.id}`} className="truncate hover:underline">{n.name}</Link>
            {n.depth != null && n.depth > 1 && <span className="text-xs text-subtle">depth {n.depth}</span>}
          </span>
          <HealthBadge status={n.health_status} stale={n.stale} />
        </li>
      ))}
    </ul>
  );
}

function Dependencies({ resource }: { resource: Resource }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const canEdit = useAuthStore((s) => s.hasPermission("resource.update"));
  const [depth, setDepth] = useState(3);
  const graph = useQuery({ queryKey: ["resources", resource.id, "dependencies", "both", depth], queryFn: () => resourcesApi.dependencies(resource.id, "both", depth) });
  const rels = useQuery({ queryKey: ["resources", resource.id, "relationships"], queryFn: () => resourcesApi.relationships(resource.id) });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["resources", resource.id] });
  const remove = useMutation({ mutationFn: (id: string) => resourcesApi.removeRelationship(id), onSuccess: refresh });
  const options = useMemo(() => ({ focusId: resource.id }), [resource.id]);
  if (graph.isLoading) return <Spinner />;
  if (graph.error || !graph.data) return <ErrorState message={errorMessage(graph.error)} />;
  const g = graph.data;
  const names = new Map(g.nodes.map((n) => [n.id, n.name]));
  const upstream = g.nodes.filter((n) => n.direction === "upstream");
  const downstream = g.nodes.filter((n) => n.direction === "downstream");

  return (
    <div className="space-y-4">
      <Section
        title="Dependency map"
        action={
          <Select aria-label="Depth" className="w-32" value={String(depth)} onChange={(e) => setDepth(Number(e.target.value))}>
            {[1, 2, 3, 5].map((d) => <option key={d} value={d}>Depth {d}</option>)}
          </Select>
        }
      >
        {g.nodes.length <= 1 ? (
          <EmptyState title="No relationships" description="Discovery links containers to hosts and hosts to the resources they connect to. You can also add relationships by hand." />
        ) : (
          <DependencyGraphView graph={g} options={options} layout="breadthfirst" height={380} onSelect={(id) => id && id !== resource.id && navigate(`/infrastructure/${id}`)} />
        )}
        {g.truncated && <p className="mt-2 text-xs text-amber-300">The graph was truncated; reduce the depth to see a complete picture.</p>}
      </Section>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Section title={`Depends on (${upstream.length})`}><NodeList nodes={upstream} empty="Nothing upstream." /></Section>
        <Section title={`Blast radius (${g.blast_radius.length})`}><NodeList nodes={downstream} empty="Nothing depends on this resource." /></Section>
        <Section title="Relationships">
          {!rels.data?.length ? (
            <p className="text-xs text-subtle">No direct relationships.</p>
          ) : (
            <ul className="space-y-2 text-xs">
              {rels.data.map((r) => (
                <li key={r.id} className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate">
                    <span className={r.source_id === resource.id ? "font-medium" : ""}>{names.get(r.source_id) ?? "…"}</span>
                    <span className="text-muted"> {RELATIONSHIP_META[r.relationship_type].label} </span>
                    <span className={r.target_id === resource.id ? "font-medium" : ""}>{names.get(r.target_id) ?? "…"}</span>
                  </span>
                  <Badge tone={r.source === "manual" ? "primary" : "neutral"}>{r.source}</Badge>
                  {canEdit && r.source === "manual" && (
                    <Button variant="ghost" size="icon" aria-label="Remove relationship" onClick={() => remove.mutate(r.id)}><Trash2 /></Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>
      {canEdit && <Section title="Add relationship"><AddRelationship resource={resource} onDone={refresh} /></Section>}
    </div>
  );
}

// ------------------------------------------------------------------ history

function History({ id }: { id: string }) {
  const [page, setPage] = useState(1);
  const query = useQuery({ queryKey: ["resources", id, "history", page], queryFn: () => resourcesApi.history(id, page), placeholderData: (p) => p });
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} />;
  if (!query.data.items.length) return <div className="panel"><EmptyState title="No history yet" /></div>;
  return (
    <div className="panel">
      <Table>
        <thead><tr><Th>When</Th><Th>Event</Th><Th>Summary</Th><Th>By</Th></tr></thead>
        <tbody>
          {query.data.items.map((e) => (
            <tr key={e.id}>
              <Td className="whitespace-nowrap text-xs text-muted" title={formatDateTime(e.created_at)}>{formatRelative(e.created_at)}</Td>
              <Td><Badge tone={e.event_type === "health_changed" ? "warning" : e.event_type === "stale" ? "neutral" : "primary"}>{e.event_type.replace(/_/g, " ")}</Badge></Td>
              <Td className="text-sm">{e.summary}</Td>
              <Td className="text-xs text-muted">{e.actor ?? "discovery"}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <Pagination page={page} pageSize={25} total={query.data.total} onPage={setPage} />
    </div>
  );
}

// ------------------------------------------------------------------ page

export function ResourceDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("overview");
  const [editing, setEditing] = useState(false);
  const canUpdate = useAuthStore((s) => s.hasPermission("resource.update"));
  const canDelete = useAuthStore((s) => s.hasPermission("resource.delete"));
  const query = useQuery({ queryKey: ["resources", id], queryFn: () => resourcesApi.get(id) });
  const check = useMutation({
    mutationFn: () => resourcesApi.checkHealth(id),
    onSuccess: () => {
      // The check runs in the worker; refresh shortly after it should be done.
      window.setTimeout(() => void queryClient.invalidateQueries({ queryKey: ["resources", id] }), 4000);
    },
  });
  const remove = useMutation({
    mutationFn: () => resourcesApi.remove(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["resources"] });
      navigate("/infrastructure");
    },
  });

  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const r = query.data;

  return (
    <div className="space-y-5">
      <Link to="/infrastructure" className="inline-flex items-center gap-1.5 text-xs text-muted hover:text-fg"><ArrowLeft className="size-3.5" /> Infrastructure</Link>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex items-start gap-3">
          <span className="grid size-11 place-items-center rounded-xl border border-border bg-surface-2"><TypeIcon type={r.resource_type} className="size-5 text-blue-400" /></span>
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">{r.name}</h1>
              <HealthBadge status={r.health_status} stale={r.stale} />
              {r.environment && <Badge>{r.environment}</Badge>}
            </div>
            <p className="mt-0.5 text-sm text-muted">
              {TYPE_META[r.resource_type]?.label} {r.os && `· ${r.os}`} {r.ip_addresses[0] && <span className="font-mono text-xs">· {r.ip_addresses[0]}</span>}
            </p>
          </div>
        </div>
        <div className="flex gap-2">
          {canUpdate && r.source === "discovered" && r.integration_id && (
            <Button variant="secondary" onClick={() => check.mutate()} loading={check.isPending}><HeartPulse /> Check health</Button>
          )}
          {canUpdate && <Button variant="secondary" onClick={() => setEditing(true)}><Pencil /> Edit</Button>}
          {canDelete && (
            <Button variant="ghost" size="icon" aria-label="Delete resource" onClick={() => window.confirm(`Delete ${r.name}? Its relationships, history and metrics are removed too.${r.source === "discovered" ? " It will reappear on the next discovery run if it still exists." : ""}`) && remove.mutate()}>
              <Trash2 />
            </Button>
          )}
        </div>
      </div>
      {check.isSuccess && <Alert tone="primary">Health check queued. Results appear here in a few seconds.</Alert>}
      {(check.error || remove.error) && <Alert>{errorMessage(check.error ?? remove.error)}</Alert>}

      <div className="flex gap-1 overflow-x-auto border-b border-border text-sm">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 capitalize ${tab === t ? "border-primary text-fg" : "border-transparent text-muted hover:text-fg"}`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "overview" && <Overview resource={r} />}
      {tab === "metrics" && <Metrics id={r.id} />}
      {tab === "services" && <Services resource={r} />}
      {tab === "dependencies" && <Dependencies resource={r} />}
      {tab === "history" && <History id={r.id} />}
      {tab === "logs" && <ResourceLogs resource={r} />}
      {tab === "incidents" && <ResourceIncidents resource={r} />}
      {editing && <ResourceDialog resource={r} onClose={() => setEditing(false)} onSaved={() => void queryClient.invalidateQueries({ queryKey: ["resources", id] })} />}
    </div>
  );
}
