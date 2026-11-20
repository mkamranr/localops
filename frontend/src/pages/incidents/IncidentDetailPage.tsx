import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  CheckCircle2,
  MessageSquare,
  Pencil,
  Play,
  Search,
  Send,
  ShieldAlert,
} from "lucide-react";
import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { auditApi, incidentsApi, investigationsApi, usersApi } from "@/api/endpoints";
import {
  IncidentStatusBadge,
  SEVERITIES,
  SEVERITY_META,
  SeverityBadge,
  SOURCE_LABEL,
  transitionLabel,
} from "@/components/incidents/shared";
import { RcaReport } from "@/components/investigation/RcaReport";
import { IncidentRemediation } from "@/components/remediation/History";
import { ToolExecutionsTable } from "@/components/investigation/ToolExecutionsTable";
import { MatchBadge } from "@/components/knowledge/shared";
import { InvestigationStatusBadge, ROOT_CAUSE_META } from "@/components/investigation/shared";
import { HealthBadge, TypeIcon } from "@/components/inventory/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { ResourcePicker } from "@/pages/incidents/IncidentsPage";
import { useAuthStore } from "@/stores/authStore";
import type { IncidentDetail, IncidentStatus, Priority, ResourceSummary, Severity } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, formatRelative } from "@/utils/format";

const TABS = ["overview", "investigation", "timeline", "diagnostics", "rca", "remediation", "related", "audit"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, string> = {
  overview: "Overview",
  investigation: "Investigation",
  timeline: "Timeline",
  diagnostics: "Diagnostics",
  rca: "RCA",
  remediation: "Remediation",
  related: "Related incidents",
  audit: "Audit history",
};

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-3 py-1.5 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  );
}

function useIncidentMutation(id: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({ queryKey: ["incident", id] });
    void queryClient.invalidateQueries({ queryKey: ["incidents"] });
  };
}

function EditDialog({ inc, open, onOpenChange }: { inc: IncidentDetail; open: boolean; onOpenChange: (o: boolean) => void }) {
  const refresh = useIncidentMutation(inc.id);
  const canListUsers = useAuthStore((s) => s.hasPermission("user.read"));
  const users = useQuery({
    queryKey: ["users", "assignable"],
    queryFn: () => usersApi.list({ page: 1, page_size: 200, status: "active" }),
    enabled: canListUsers && open,
  });
  const [form, setForm] = useState({
    title: inc.title,
    description: inc.description,
    severity: inc.severity,
    priority: inc.priority,
    assigned_to: inc.assigned_to ?? "",
  });
  const [resources, setResources] = useState<ResourceSummary[]>(inc.resources as unknown as ResourceSummary[]);
  const mutation = useMutation({
    mutationFn: () =>
      incidentsApi.update(inc.id, {
        title: form.title.trim(),
        description: form.description,
        severity: form.severity,
        priority: form.priority,
        ...(canListUsers ? { assigned_to: form.assigned_to || null } : {}),
        resource_ids: resources.map((r) => r.id),
      }),
    onSuccess: () => {
      refresh();
      onOpenChange(false);
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
      title={`Edit ${inc.key}`}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="edit-incident" loading={mutation.isPending}>Save</Button>
        </>
      }
    >
      <form id="edit-incident" onSubmit={submit} className="space-y-4">
        <Field label="Title" htmlFor="e-title">
          <Input id="e-title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} maxLength={200} required />
        </Field>
        <Field label="Description" htmlFor="e-desc">
          <Textarea id="e-desc" rows={4} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Severity" htmlFor="e-sev">
            <Select id="e-sev" value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value as Severity })}>
              {SEVERITIES.map((s) => <option key={s} value={s}>{SEVERITY_META[s].label}</option>)}
            </Select>
          </Field>
          <Field label="Priority" htmlFor="e-pri">
            <Select id="e-pri" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value as Priority })}>
              {["P1", "P2", "P3", "P4"].map((p) => <option key={p} value={p}>{p}</option>)}
            </Select>
          </Field>
        </div>
        {canListUsers && (
          <Field label="Assignee" htmlFor="e-assignee">
            <Select id="e-assignee" value={form.assigned_to} onChange={(e) => setForm({ ...form, assigned_to: e.target.value })}>
              <option value="">Unassigned</option>
              {(users.data?.items ?? []).map((u) => <option key={u.id} value={u.id}>{u.display_name || u.username}</option>)}
            </Select>
          </Field>
        )}
        <Field label="Affected resources" htmlFor="e-res">
          <ResourcePicker selected={resources} onChange={setResources} />
        </Field>
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}

function CloseDialog({ inc, target, onClose }: { inc: IncidentDetail; target: "resolved" | "closed" | null; onClose: () => void }) {
  const refresh = useIncidentMutation(inc.id);
  const [resolution, setResolution] = useState(inc.resolution ?? "");
  const mutation = useMutation({
    mutationFn: () => incidentsApi.close(inc.id, resolution.trim(), target ?? "closed"),
    onSuccess: () => {
      refresh();
      onClose();
    },
  });
  return (
    <Dialog
      open={!!target}
      onOpenChange={(o) => !o && onClose()}
      title={target === "resolved" ? `Resolve ${inc.key}` : `Close ${inc.key}`}
      description="Record what fixed it; the resolution is searchable for similar incidents later."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={() => mutation.mutate()} loading={mutation.isPending} disabled={resolution.trim().length < 3}>
            <CheckCircle2 /> {target === "resolved" ? "Resolve" : "Close"}
          </Button>
        </>
      }
    >
      <Field label="Resolution" htmlFor="c-res">
        <Textarea id="c-res" rows={4} value={resolution} onChange={(e) => setResolution(e.target.value)} autoFocus />
      </Field>
      {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
    </Dialog>
  );
}

function Overview({ inc }: { inc: IncidentDetail }) {
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Panel title="Details">
        <dl className="divide-y divide-border">
          <Row label="Severity"><SeverityBadge severity={inc.severity} /> <span className="ml-1 font-mono text-xs text-muted">{inc.priority}</span></Row>
          <Row label="Status"><IncidentStatusBadge status={inc.status} /></Row>
          <Row label="Source">{SOURCE_LABEL[inc.source]}</Row>
          <Row label="Assignee">{inc.assigned_username ?? <span className="text-subtle">Unassigned</span>}</Row>
          <Row label="Reported by">{inc.created_by ?? "—"}</Row>
          <Row label="Opened">{formatDateTime(inc.created_at)}</Row>
          {inc.resolved_at && <Row label="Resolved">{formatDateTime(inc.resolved_at)}</Row>}
          {inc.closed_at && <Row label="Closed">{formatDateTime(inc.closed_at)}</Row>}
        </dl>
        {inc.description && <p className="mt-4 text-sm whitespace-pre-wrap text-fg/90">{inc.description}</p>}
      </Panel>
      <div className="space-y-5">
        <Panel title="Affected resources">
          {inc.resources.length ? (
            <ul className="space-y-2">
              {inc.resources.map((r) => (
                <li key={r.id} className="flex items-center gap-2 text-sm">
                  <TypeIcon type={r.resource_type} className="size-4 text-muted" />
                  <Link to={`/infrastructure/${r.id}`} className="font-medium hover:underline">{r.name}</Link>
                  <HealthBadge status={r.health_status} />
                  {r.environment && <span className="text-xs text-subtle">{r.environment}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-subtle">None linked. Edit the incident to add the affected resources.</p>
          )}
        </Panel>
        <Panel title="Root cause">
          {inc.root_cause?.statement ? (
            <div className="space-y-1.5 text-sm">
              <Badge tone={ROOT_CAUSE_META[inc.root_cause.status ?? "probable"]?.tone ?? "warning"}>
                {ROOT_CAUSE_META[inc.root_cause.status ?? "probable"]?.label ?? inc.root_cause.status}
              </Badge>
              <p>{inc.root_cause.statement}</p>
              {inc.root_cause.confidence != null && <p className="text-xs text-muted">confidence {Math.round(inc.root_cause.confidence * 100)}%</p>}
            </div>
          ) : (
            <p className="text-xs text-subtle">Not established yet. Start an investigation to collect evidence.</p>
          )}
          {inc.investigation_summary && <p className="mt-3 text-xs text-muted">{inc.investigation_summary}</p>}
          {inc.resolution && (
            <p className="mt-3 text-sm"><span className="text-muted">Resolution: </span>{inc.resolution}</p>
          )}
        </Panel>
      </div>
    </div>
  );
}

function Investigations({ inc }: { inc: IncidentDetail }) {
  return (
    <Panel title="Linked investigations" bodyClassName="px-0 pb-0">
      {!inc.investigations.length ? (
        <EmptyState icon={<Search className="size-8" />} title="No investigation yet" description="Use Investigate to have the agents collect evidence on the affected resources." />
      ) : (
        <Table>
          <thead><tr><Th>Investigation</Th><Th>Status</Th><Th>Conclusion</Th><Th>Started</Th></tr></thead>
          <tbody>
            {inc.investigations.map((i) => (
              <tr key={i.id}>
                <Td><Link to={`/investigations/${i.id}`} className="font-medium hover:text-blue-300">{i.title}</Link></Td>
                <Td><InvestigationStatusBadge status={i.status} /></Td>
                <Td className="max-w-md truncate text-xs text-muted">{i.conclusion ?? "—"}</Td>
                <Td className="text-xs text-muted">{formatRelative(i.started_at)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
}

function TimelineTab({ inc }: { inc: IncidentDetail }) {
  const refresh = useIncidentMutation(inc.id);
  const queryClient = useQueryClient();
  const canUpdate = useAuthStore((s) => s.hasPermission("incident.update"));
  const [body, setBody] = useState("");
  const query = useQuery({ queryKey: ["incident", inc.id, "timeline"], queryFn: () => incidentsApi.timeline(inc.id), refetchInterval: 15_000 });
  const comment = useMutation({
    mutationFn: () => incidentsApi.comment(inc.id, body.trim()),
    onSuccess: () => {
      setBody("");
      refresh();
      void queryClient.invalidateQueries({ queryKey: ["incident", inc.id, "timeline"] });
    },
  });
  return (
    <Panel title="Timeline">
      {query.isLoading ? <Spinner /> : (
        <ol className="space-y-3 border-l border-border pl-4">
          {(query.data ?? []).map((e) => (
            <li key={e.id} className="relative">
              <span className={cn("absolute top-1.5 -left-[21px] size-2.5 rounded-full border-2 border-surface", e.event_type === "comment" ? "bg-blue-400" : e.event_type.startsWith("investigation") ? "bg-violet-400" : e.event_type === "resource_health" ? "bg-amber-400" : "bg-subtle")} />
              <div className="flex flex-wrap items-baseline gap-2">
                {e.event_type === "comment" && <MessageSquare className="size-3.5 text-blue-300" />}
                <span className="text-sm text-fg">{e.summary}</span>
              </div>
              <p className="text-[11px] text-subtle">
                {e.actor ?? "system"} · {formatDateTime(e.created_at)}
                {typeof e.details.investigation_id === "string" && (
                  <> · <Link className="text-blue-300 hover:underline" to={`/investigations/${e.details.investigation_id}`}>investigation</Link></>
                )}
              </p>
            </li>
          ))}
        </ol>
      )}
      {canUpdate && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (body.trim()) comment.mutate();
          }}
          className="mt-5 flex gap-2"
        >
          <Input value={body} onChange={(e) => setBody(e.target.value)} placeholder="Add a comment…" maxLength={4000} aria-label="Comment" />
          <Button type="submit" loading={comment.isPending} disabled={!body.trim()}><Send /> Comment</Button>
        </form>
      )}
      {comment.error && <Alert>{errorMessage(comment.error)}</Alert>}
    </Panel>
  );
}

function Diagnostics({ inc }: { inc: IncidentDetail }) {
  const query = useQuery({ queryKey: ["incident", inc.id, "tools"], queryFn: () => incidentsApi.toolExecutions(inc.id) });
  return (
    <Panel title="Diagnostic tool runs" bodyClassName="px-0 pb-0">
      {query.isLoading ? <Spinner /> : query.error ? <ErrorState message={errorMessage(query.error)} /> : <ToolExecutionsTable rows={query.data ?? []} />}
    </Panel>
  );
}

function RcaTab({ inc }: { inc: IncidentDetail }) {
  const latest = inc.investigations.find((i) => i.status === "completed");
  const query = useQuery({ queryKey: ["investigation", latest?.id], queryFn: () => investigationsApi.get(latest!.id), enabled: !!latest });
  if (!latest) return <Panel><EmptyState title="No RCA yet" description="An RCA report is produced when a linked investigation completes." /></Panel>;
  if (query.isLoading || !query.data) return <Spinner />;
  return <RcaReport inv={query.data} />;
}

function Related({ inc }: { inc: IncidentDetail }) {
  const query = useQuery({ queryKey: ["incident", inc.id, "similar"], queryFn: () => incidentsApi.similar(inc.id) });
  return (
    <Panel title="Similar incidents and investigations">
      {query.isLoading ? <Spinner /> : !(query.data ?? []).length ? (
        <EmptyState title="Nothing similar found" description="Matching uses the incident text and its affected resources." />
      ) : (
        <ul className="space-y-2">
          {(query.data ?? []).map((s) => (
            <li key={s.id}>
              <Link to={s.kind === "incident" ? `/incidents/${s.id}` : `/investigations/${s.id}`} className="block rounded-lg border border-border bg-surface-2 p-3 hover:border-border-strong">
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-mono text-xs text-subtle">{s.number ?? "Investigation"}</span>
                  <span className="flex-1 truncate font-medium">{s.title}</span>
                  {s.match && <MatchBadge match={s.match} />}
                  <Badge>{s.status}</Badge>
                </div>
                {(s.root_cause || s.resolution || s.conclusion) && (
                  <p className="mt-1 text-xs text-muted">
                    {s.root_cause && <>Root cause: {s.root_cause}. </>}
                    {s.resolution && <>Resolution: {s.resolution}</>}
                    {!s.root_cause && !s.resolution && s.conclusion}
                  </p>
                )}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function Audit({ inc }: { inc: IncidentDetail }) {
  const canAudit = useAuthStore((s) => s.hasPermission("audit.read"));
  const query = useQuery({
    queryKey: ["audit", "incident", inc.id],
    queryFn: () => auditApi.list({ page: 1, page_size: 100, entity_id: inc.id }),
    enabled: canAudit,
  });
  if (!canAudit) return <Panel><EmptyState title="Audit access required" description="Viewing audit history needs the audit.read permission." /></Panel>;
  return (
    <Panel title="Audit history" bodyClassName="px-0 pb-0">
      {query.isLoading ? <Spinner /> : (
        <Table>
          <thead><tr><Th>When</Th><Th>Action</Th><Th>User</Th><Th>Outcome</Th></tr></thead>
          <tbody>
            {(query.data?.items ?? []).map((a) => (
              <tr key={a.id}>
                <Td className="text-xs text-muted">{formatDateTime(a.created_at)}</Td>
                <Td className="font-mono text-xs">{a.action}</Td>
                <Td className="text-xs">{a.username ?? "system"}</Td>
                <Td><Badge tone={a.outcome === "success" ? "success" : "danger"}>{a.outcome}</Badge></Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Panel>
  );
}

export function IncidentDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const refresh = useIncidentMutation(id);
  const [tab, setTab] = useState<Tab>("overview");
  const [editing, setEditing] = useState(false);
  const [closing, setClosing] = useState<"resolved" | "closed" | null>(null);
  const canUpdate = useAuthStore((s) => s.hasPermission("incident.update"));
  const canInvestigate = useAuthStore((s) => s.hasPermission("incident.investigate"));
  const query = useQuery({ queryKey: ["incident", id], queryFn: () => incidentsApi.get(id), refetchInterval: 10_000 });
  const transition = useMutation({
    mutationFn: (status: IncidentStatus) => incidentsApi.update(id, { status }),
    onSuccess: refresh,
  });
  const investigate = useMutation({
    mutationFn: () => incidentsApi.investigate(id),
    onSuccess: (inv) => {
      refresh();
      navigate(`/investigations/${inv.id}`);
    },
  });

  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const inc = query.data;
  const running = inc.investigations.some((i) => ["queued", "running", "awaiting_input", "awaiting_approval"].includes(i.status));
  const canStartInvestigation = canInvestigate && !["resolved", "closed"].includes(inc.status) && !running;

  return (
    <div className="space-y-5">
      <Link to="/incidents" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg"><ArrowLeft className="size-4" /> Incidents</Link>
      <div className="panel flex flex-wrap items-center gap-4 px-5 py-4">
        <ShieldAlert className={cn("size-6", inc.severity === "critical" || inc.severity === "high" ? "text-red-400" : "text-amber-400")} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-sm text-subtle">{inc.key}</span>
            <h1 className="truncate text-lg font-semibold">{inc.title}</h1>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <SeverityBadge severity={inc.severity} />
            <IncidentStatusBadge status={inc.status} />
            <span className="text-xs text-subtle">opened {formatRelative(inc.created_at)} · {SOURCE_LABEL[inc.source]}</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {canStartInvestigation && (
            <Button onClick={() => investigate.mutate()} loading={investigate.isPending}><Play /> Investigate</Button>
          )}
          {running && (
            <Button variant="secondary" asChild>
              <Link to={`/investigations/${inc.investigations.find((i) => ["queued", "running", "awaiting_input", "awaiting_approval"].includes(i.status))!.id}`}>
                <Search /> Investigation running
              </Link>
            </Button>
          )}
          {canUpdate && (
            <>
              <Button variant="secondary" onClick={() => setEditing(true)}><Pencil /> Edit</Button>
              {inc.allowed_transitions.filter((t) => t !== "resolved" && t !== "closed").map((t) => (
                <Button key={t} variant="secondary" onClick={() => transition.mutate(t)} loading={transition.isPending && transition.variables === t}>
                  {transitionLabel(t)}
                </Button>
              ))}
              {inc.allowed_transitions.includes("resolved") && <Button variant="secondary" onClick={() => setClosing("resolved")}>Resolve</Button>}
              {inc.allowed_transitions.includes("closed") && <Button variant="secondary" onClick={() => setClosing("closed")}>Close</Button>}
            </>
          )}
        </div>
      </div>
      {(transition.error || investigate.error) && <Alert>{errorMessage(transition.error ?? investigate.error)}</Alert>}

      <div className="flex gap-1 overflow-x-auto border-b border-border text-sm">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 whitespace-nowrap ${tab === t ? "border-primary text-fg" : "border-transparent text-muted hover:text-fg"}`}
          >
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>

      {tab === "overview" && <Overview inc={inc} />}
      {tab === "investigation" && <Investigations inc={inc} />}
      {tab === "timeline" && <TimelineTab inc={inc} />}
      {tab === "diagnostics" && <Diagnostics inc={inc} />}
      {tab === "rca" && <RcaTab inc={inc} />}
      {tab === "remediation" && <IncidentRemediation inc={inc} />}
      {tab === "related" && <Related inc={inc} />}
      {tab === "audit" && <Audit inc={inc} />}

      {editing && <EditDialog inc={inc} open={editing} onOpenChange={setEditing} />}
      <CloseDialog key={closing ?? "none"} inc={inc} target={closing} onClose={() => setClosing(null)} />
    </div>
  );
}
