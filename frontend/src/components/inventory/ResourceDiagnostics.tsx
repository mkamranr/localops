import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, ShieldAlert } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { incidentsApi, toolsApi } from "@/api/endpoints";
import { IncidentStatusBadge, SeverityBadge } from "@/components/incidents/shared";
import { ToolExecutionsTable } from "@/components/investigation/ToolExecutionsTable";
import { formatDuration } from "@/components/investigation/shared";
import { Button } from "@/components/ui/button";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { Resource, ToolInfo } from "@/types/api";
import { formatRelative } from "@/utils/format";

/** The log tool to preselect for each resource type. */
const LOG_TOOL: Record<string, string> = {
  linux_server: "linux_journal",
  docker_host: "linux_journal",
  docker_container: "docker_container_logs",
  windows_server: "windows_event_log",
};

type SchemaProp = {
  type?: string;
  description?: string;
  enum?: string[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  anyOf?: SchemaProp[];
};

/** The effective type of a JSON-schema property ("anyOf: [X, null]" means optional X). */
export function propType(prop: SchemaProp): SchemaProp {
  if (prop.anyOf) {
    const inner = prop.anyOf.find((p) => p.type !== "null") ?? {};
    return { ...inner, description: prop.description, default: prop.default };
  }
  return prop;
}

/** Form values (strings) to tool arguments: numbers parsed, blanks left out. */
export function toArguments(tool: ToolInfo, values: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, raw] of Object.entries(tool.input_schema.properties ?? {})) {
    if (name === "resource_id") continue;
    const value = (values[name] ?? "").trim();
    if (!value) continue;
    const t = propType(raw as SchemaProp).type;
    out[name] = t === "integer" || t === "number" ? Number(value) : value;
  }
  return out;
}

function defaults(tool: ToolInfo | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, raw] of Object.entries(tool?.input_schema.properties ?? {})) {
    const d = (raw as SchemaProp).default;
    if (name !== "resource_id" && d != null) out[name] = String(d);
  }
  return out;
}

function ToolForm({ resource, tools }: { resource: Resource; tools: ToolInfo[] }) {
  const queryClient = useQueryClient();
  const preferred = tools.find((t) => t.name === LOG_TOOL[resource.resource_type]) ?? tools[0];
  const [name, setName] = useState(preferred?.name ?? "");
  const tool = tools.find((t) => t.name === name);
  const [values, setValues] = useState<Record<string, string>>(() => defaults(preferred));
  const run = useMutation({
    mutationFn: () => toolsApi.run(name, resource.id, toArguments(tool!, values)),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["resources", resource.id, "tool-runs"] }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (tool) run.mutate();
  };
  const fields = Object.entries(tool?.input_schema.properties ?? {}).filter(([n]) => n !== "resource_id");

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-64"><Field label="Tool" htmlFor="tool-name">
          <Select
            id="tool-name"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setValues(defaults(tools.find((t) => t.name === e.target.value)));
              run.reset();
            }}
          >
            {tools.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
          </Select>
        </Field></div>
        {fields.map(([field, raw]) => {
          const p = propType(raw as SchemaProp);
          const id = `arg-${field}`;
          return (
            <div key={field} className="w-40"><Field label={field.replace(/_/g, " ")} htmlFor={id}>
              {p.enum ? (
                <Select id={id} value={values[field] ?? ""} onChange={(e) => setValues({ ...values, [field]: e.target.value })}>
                  {p.enum.map((v) => <option key={v} value={v}>{v}</option>)}
                </Select>
              ) : (
                <Input
                  id={id}
                  type={p.type === "integer" ? "number" : "text"}
                  min={p.minimum}
                  max={p.maximum}
                  value={values[field] ?? ""}
                  title={p.description}
                  placeholder={p.description ? p.description.slice(0, 30) : undefined}
                  onChange={(e) => setValues({ ...values, [field]: e.target.value })}
                />
              )}
            </Field></div>
          );
        })}
        <Button type="submit" loading={run.isPending} disabled={!tool}><Play /> Run</Button>
      </div>
      {tool && <p className="text-xs text-muted">{tool.description}</p>}
      {run.error && <Alert>{errorMessage(run.error)}</Alert>}
      {run.data && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone={run.data.status === "success" ? "success" : run.data.status === "rejected" ? "warning" : "danger"}>{run.data.status}</Badge>
            <span>{run.data.summary}</span>
            <span className="text-xs text-subtle">{formatDuration(run.data.duration_ms)}</span>
          </div>
          {run.data.error && <Alert tone="warning">{run.data.error}</Alert>}
          {run.data.output && (
            <pre className="max-h-[60vh] overflow-auto rounded-lg border border-border bg-black/40 p-3 font-mono text-[11px] whitespace-pre-wrap text-fg/85">{run.data.output}</pre>
          )}
          <p className="text-[11px] text-subtle">Output is redacted for secrets before it is shown or stored.</p>
        </div>
      )}
    </form>
  );
}

/** Live logs and read-only diagnostics for one resource, plus its tool-run history. */
export function ResourceLogs({ resource }: { resource: Resource }) {
  const canRun = useAuthStore((s) => s.hasPermission("tool.execute.readonly"));
  const canListTools = useAuthStore((s) => s.hasPermission("incident.read"));
  const tools = useQuery({
    queryKey: ["tools", resource.resource_type],
    queryFn: () => toolsApi.list(resource.resource_type),
    enabled: canRun && canListTools,
  });
  const history = useQuery({
    queryKey: ["resources", resource.id, "tool-runs"],
    queryFn: () => toolsApi.forResource(resource.id),
  });
  const remote = useMemo(() => (tools.data ?? []).filter((t) => t.remote && t.kind !== "change"), [tools.data]);

  return (
    <div className="space-y-5">
      <Panel title="Logs and diagnostics">
        {!canRun || !canListTools ? (
          <EmptyState title="Diagnostics permission required" description="Running read-only diagnostic tools needs the tool.execute.readonly permission." />
        ) : tools.isLoading ? (
          <Spinner />
        ) : tools.error ? (
          <ErrorState message={errorMessage(tools.error)} />
        ) : !remote.length ? (
          <EmptyState title="No live tools for this resource type" description="Logs can be read from Linux and Windows hosts and from containers discovered through an integration." />
        ) : (
          <ToolForm resource={resource} tools={remote} />
        )}
      </Panel>
      <Panel title="Recent tool runs" bodyClassName="px-0 pb-0">
        {history.isLoading ? <Spinner /> : history.error ? <ErrorState message={errorMessage(history.error)} /> : <ToolExecutionsTable rows={history.data ?? []} />}
      </Panel>
    </div>
  );
}

/** Incidents that list this resource as affected. */
export function ResourceIncidents({ resource }: { resource: Resource }) {
  const canRead = useAuthStore((s) => s.hasPermission("incident.read"));
  const query = useQuery({
    queryKey: ["incidents", { resource_id: resource.id }],
    queryFn: () => incidentsApi.list({ page: 1, page_size: 50, resource_id: resource.id }),
    enabled: canRead,
  });
  if (!canRead) return <Panel><EmptyState title="Incident access required" description="Viewing incidents needs the incident.read permission." /></Panel>;
  return (
    <Panel title="Incidents" bodyClassName="px-0 pb-0">
      {query.isLoading ? (
        <Spinner />
      ) : query.error ? (
        <ErrorState message={errorMessage(query.error)} />
      ) : !query.data?.items.length ? (
        <EmptyState icon={<ShieldAlert className="size-8" />} title="No incidents" description="Incidents that list this resource as affected appear here." />
      ) : (
        <Table>
          <thead><tr><Th>Incident</Th><Th>Severity</Th><Th>Status</Th><Th>Opened</Th></tr></thead>
          <tbody>
            {query.data.items.map((i) => (
              <tr key={i.id}>
                <Td>
                  <Link to={`/incidents/${i.id}`} className="font-medium hover:text-blue-300">
                    <span className="mr-2 font-mono text-xs text-subtle">{i.key}</span>{i.title}
                  </Link>
                </Td>
                <Td><SeverityBadge severity={i.severity} /></Td>
                <Td><IncidentStatusBadge status={i.status} /></Td>
                <Td className="text-xs text-muted">{formatRelative(i.created_at)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
}
