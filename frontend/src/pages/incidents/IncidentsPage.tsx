import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, ShieldAlert, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { incidentsApi, resourcesApi } from "@/api/endpoints";
import {
  INCIDENT_STATUS_META,
  INCIDENT_STATUSES,
  IncidentStatusBadge,
  SEVERITIES,
  SEVERITY_META,
  SeverityBadge,
  SOURCE_LABEL,
} from "@/components/incidents/shared";
import { TypeIcon } from "@/components/inventory/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Pagination, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { ResourceSummary, Severity } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, formatRelative } from "@/utils/format";

const PAGE_SIZE = 20;

export function ResourcePicker({
  selected,
  onChange,
}: {
  selected: ResourceSummary[];
  onChange: (value: ResourceSummary[]) => void;
}) {
  const [q, setQ] = useState("");
  const query = useQuery({
    queryKey: ["resources", "picker", q],
    queryFn: () => resourcesApi.list({ page: 1, page_size: 8, q: q || undefined }),
    enabled: q.length > 0,
  });
  const ids = new Set(selected.map((r) => r.id));
  return (
    <div className="space-y-2">
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {selected.map((r) => (
            <Badge key={r.id} tone="primary">
              {r.name}
              <button type="button" aria-label={`Remove ${r.name}`} onClick={() => onChange(selected.filter((x) => x.id !== r.id))}>
                <X className="size-3" />
              </button>
            </Badge>
          ))}
        </div>
      )}
      <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search the inventory…" aria-label="Search resources" />
      {q && (
        <ul className="max-h-40 overflow-y-auto rounded-lg border border-border">
          {(query.data?.items ?? []).filter((r) => !ids.has(r.id)).map((r) => (
            <li key={r.id}>
              <button
                type="button"
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-surface-2"
                onClick={() => {
                  onChange([...selected, r]);
                  setQ("");
                }}
              >
                <TypeIcon type={r.resource_type} className="size-3.5 text-muted" /> {r.name}
                <span className="text-xs text-subtle">{r.environment}</span>
              </button>
            </li>
          ))}
          {query.data && !query.data.items.length && <li className="px-3 py-2 text-xs text-subtle">No match</li>}
        </ul>
      )}
    </div>
  );
}

function CreateIncidentDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState<Severity>("medium");
  const [resources, setResources] = useState<ResourceSummary[]>([]);
  const mutation = useMutation({
    mutationFn: () =>
      incidentsApi.create({ title: title.trim(), description, severity, resource_ids: resources.map((r) => r.id) }),
    onSuccess: (inc) => {
      void queryClient.invalidateQueries({ queryKey: ["incidents"] });
      onOpenChange(false);
      navigate(`/incidents/${inc.id}`);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Report an incident"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="incident-form" loading={mutation.isPending} disabled={title.trim().length < 3}>
            Create incident
          </Button>
        </>
      }
    >
      <form id="incident-form" onSubmit={submit} className="space-y-4">
        <Field label="Title" htmlFor="i-title">
          <Input id="i-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} autoFocus required />
        </Field>
        <Field label="Description" htmlFor="i-desc" hint="Symptoms, when it started, who is affected.">
          <Textarea id="i-desc" value={description} onChange={(e) => setDescription(e.target.value)} rows={4} maxLength={8000} />
        </Field>
        <Field label="Severity" htmlFor="i-sev" hint="Priority follows from severity unless changed later.">
          <Select id="i-sev" value={severity} onChange={(e) => setSeverity(e.target.value as Severity)}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{SEVERITY_META[s].label}</option>)}
          </Select>
        </Field>
        <Field label="Affected resources" htmlFor="i-res">
          <ResourcePicker selected={resources} onChange={setResources} />
        </Field>
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}

function Counter({ label, value, tone, active, onClick }: { label: string; value: number; tone: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "panel flex min-w-28 flex-1 flex-col items-start px-4 py-3 text-left transition-colors hover:border-border-strong",
        active && "border-primary/60",
      )}
    >
      <span className={cn("font-mono text-2xl font-semibold tabular-nums", tone)}>{value}</span>
      <span className="text-xs text-muted">{label}</span>
    </button>
  );
}

export function IncidentsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const canCreate = useAuthStore((s) => s.hasPermission("incident.create"));
  const [creating, setCreating] = useState(false);
  const [page, setPage] = useState(1);
  const q = params.get("q") ?? "";
  const status = params.get("status") ?? "";
  const severity = params.get("severity") ?? "";
  const source = params.get("source") ?? "";
  const openOnly = params.get("open") !== "false" && !status;
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
    setPage(1);
  };
  const stats = useQuery({ queryKey: ["incidents", "summary"], queryFn: incidentsApi.summary, refetchInterval: 15_000 });
  const query = useQuery({
    queryKey: ["incidents", { page, q, status, severity, source, openOnly }],
    queryFn: () =>
      incidentsApi.list({
        page,
        page_size: PAGE_SIZE,
        q: q || undefined,
        status: status || undefined,
        severity: severity || undefined,
        source: source || undefined,
        open: openOnly || undefined,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });
  const s = stats.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Incidents</h1>
          <p className="text-sm text-muted">Reported and automatically detected incidents, their investigations and root causes.</p>
        </div>
        {canCreate && (
          <Button onClick={() => setCreating(true)}>
            <Plus /> Report incident
          </Button>
        )}
      </div>

      <div className="flex flex-wrap gap-3">
        <Counter label="Open" value={s?.open ?? 0} tone="text-fg" active={openOnly && !severity} onClick={() => { setParam("severity", ""); setParam("open", ""); }} />
        {(["critical", "high", "medium", "low"] as Severity[]).map((sev) => (
          <Counter
            key={sev}
            label={`${SEVERITY_META[sev].label} (open)`}
            value={s?.by_severity_open[sev] ?? 0}
            tone={sev === "critical" || sev === "high" ? "text-red-400" : sev === "medium" ? "text-amber-400" : "text-blue-300"}
            active={severity === sev}
            onClick={() => setParam("severity", severity === sev ? "" : sev)}
          />
        ))}
        <Counter label="Resolved (7 days)" value={s?.resolved_last_7d ?? 0} tone="text-green-400" active={status === "resolved"} onClick={() => setParam("status", status === "resolved" ? "" : "resolved")} />
        <div className="panel flex min-w-28 flex-1 flex-col px-4 py-3">
          <span className="font-mono text-2xl font-semibold text-fg tabular-nums">
            {s?.mean_time_to_resolve_minutes != null ? `${Math.round(s.mean_time_to_resolve_minutes)}m` : "—"}
          </span>
          <span className="text-xs text-muted">Mean time to resolve (30 d)</span>
        </div>
      </div>

      <Panel bodyClassName="px-0 pb-0">
        <div className="flex flex-wrap gap-3 px-5 pb-4">
          <div className="relative min-w-60 flex-1">
            <Search className="absolute top-2.5 left-3 size-4 text-subtle" />
            <Input value={q} onChange={(e) => setParam("q", e.target.value)} placeholder="Search title, description or INC-number…" className="pl-9" aria-label="Search" />
          </div>
          <Select value={status} onChange={(e) => setParam("status", e.target.value)} className="w-44" aria-label="Status">
            <option value="">Open incidents</option>
            {INCIDENT_STATUSES.map((st) => <option key={st} value={st}>{INCIDENT_STATUS_META[st].label}</option>)}
          </Select>
          <Select value={severity} onChange={(e) => setParam("severity", e.target.value)} className="w-36" aria-label="Severity">
            <option value="">Any severity</option>
            {SEVERITIES.map((sev) => <option key={sev} value={sev}>{SEVERITY_META[sev].label}</option>)}
          </Select>
          <Select value={source} onChange={(e) => setParam("source", e.target.value)} className="w-48" aria-label="Source">
            <option value="">Any source</option>
            {Object.entries(SOURCE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </Select>
          {!status && (
            <label className="flex items-center gap-2 text-xs text-muted">
              <input type="checkbox" className="size-4" checked={!openOnly} onChange={(e) => setParam("open", e.target.checked ? "false" : "")} />
              Include closed
            </label>
          )}
        </div>
        {query.isLoading ? (
          <Spinner />
        ) : query.error || !query.data ? (
          <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />
        ) : !query.data.items.length ? (
          <EmptyState
            icon={<ShieldAlert className="size-8" />}
            title="No incidents"
            description={q || status || severity || source ? "Nothing matches the filters." : "Incidents appear here when reported, or automatically when discovery finds an unhealthy resource."}
          />
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>Incident</Th>
                  <Th>Severity</Th>
                  <Th>Status</Th>
                  <Th>Resources</Th>
                  <Th>Assignee</Th>
                  <Th>Opened</Th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((inc) => (
                  <tr key={inc.id} className="cursor-pointer transition-colors hover:bg-surface-2/60" onClick={() => navigate(`/incidents/${inc.id}`)}>
                    <Td className="max-w-md">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs text-subtle">{inc.key}</span>
                        <Link to={`/incidents/${inc.id}`} className="truncate font-medium text-fg hover:text-blue-300" onClick={(e) => e.stopPropagation()}>
                          {inc.title}
                        </Link>
                      </div>
                      <p className="truncate text-xs text-muted">
                        {inc.root_cause?.statement ? `Root cause: ${inc.root_cause.statement}` : SOURCE_LABEL[inc.source]}
                      </p>
                    </Td>
                    <Td><SeverityBadge severity={inc.severity} /> <span className="ml-1 font-mono text-[11px] text-subtle">{inc.priority}</span></Td>
                    <Td><IncidentStatusBadge status={inc.status} /></Td>
                    <Td className="max-w-48 truncate text-xs text-muted">{inc.resources.map((r) => r.name).join(", ") || "—"}</Td>
                    <Td className="text-xs text-muted">{inc.assigned_username ?? "—"}</Td>
                    <Td className="text-xs text-muted" title={formatDateTime(inc.created_at)}>{formatRelative(inc.created_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination page={page} pageSize={PAGE_SIZE} total={query.data.total} onPage={setPage} />
          </>
        )}
      </Panel>
      {creating && <CreateIncidentDialog open={creating} onOpenChange={setCreating} />}
    </div>
  );
}
