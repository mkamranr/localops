import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Download,
  FileText,
  GitBranch,
  History,
  Lightbulb,
  Printer,
  ShieldCheck,
  Target,
  Wrench,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { investigationsApi } from "@/api/endpoints";
import {
  agentLabel,
  FINDING_META,
  formatArgs,
  formatDuration,
  ROOT_CAUSE_META,
} from "@/components/investigation/shared";
import {
  confidencePercent,
  downloadText,
  evidenceIndex,
  reportToMarkdown,
  slug,
  type Report,
} from "@/components/investigation/report";
import { HealthBadge } from "@/components/inventory/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, Panel } from "@/components/ui/display";
import type { EvidenceItem, InvestigationDetail } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, formatRelative } from "@/utils/format";

function Section({ n, title, children, icon }: { n: number; title: string; children: ReactNode; icon?: ReactNode }) {
  return (
    <section className="break-inside-avoid space-y-2">
      <h3 className="flex items-center gap-2 text-xs font-semibold tracking-wide text-muted uppercase">
        <span className="grid size-5 place-items-center rounded bg-surface-3 font-mono text-[10px] text-subtle">{n}</span>
        {icon}
        {title}
      </h3>
      <div className="text-sm text-fg/90">{children}</div>
    </section>
  );
}

function Muted({ children }: { children: ReactNode }) {
  return <p className="text-xs text-subtle">{children}</p>;
}

export function EvidenceChips({ ids, onOpen }: { ids: string[] | undefined; onOpen: (id: string) => void }) {
  if (!ids?.length) return <span className="text-[11px] text-subtle italic">no evidence</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {ids.map((id) => (
        <button
          key={id}
          type="button"
          onClick={() => onOpen(id)}
          className="rounded border border-cyan-500/30 bg-cyan-500/10 px-1.5 font-mono text-[11px] text-cyan-300 hover:bg-cyan-500/20"
          title="Show evidence"
        >
          {id}
        </button>
      ))}
    </span>
  );
}

function EvidenceDrawer({
  inv,
  item,
  onClose,
}: {
  inv: InvestigationDetail;
  item: EvidenceItem | null;
  onClose: () => void;
}) {
  const executions = useQuery({
    queryKey: ["investigation", inv.id, "tool-executions"],
    queryFn: () => investigationsApi.toolExecutions(inv.id),
    enabled: !!item && item.source === "tool",
  });
  const full = executions.data?.find((x) => x.evidence_id === item?.id);
  return (
    <Dialog
      wide
      open={!!item}
      onOpenChange={(o) => !o && onClose()}
      title={item ? `${item.id} · ${item.tool ?? item.source}` : ""}
      description={item ? `${agentLabel(item.agent)}${item.resource ? ` · ${item.resource}` : ""}` : undefined}
    >
      {item && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge tone={item.ok === false ? "danger" : "success"}>{item.ok === false ? "failed" : "collected"}</Badge>
            <Badge>{item.source}</Badge>
            {item.baseline && <Badge tone="warning">baseline collection</Badge>}
            {item.health && <HealthBadge status={item.health} />}
            {item.observed_at && <span className="text-subtle">{formatDateTime(item.observed_at)}</span>}
            {item.duration_ms != null && <span className="text-subtle">{formatDuration(item.duration_ms)}</span>}
          </div>
          <p className="text-sm text-fg">{item.summary}</p>
          {!!item.findings?.length && (
            <ul className="list-inside list-disc text-xs text-muted">
              {item.findings.map((f) => <li key={f}>{f}</li>)}
            </ul>
          )}
          {item.arguments && Object.keys(item.arguments).length > 0 && (
            <p className="font-mono text-[11px] text-subtle">arguments: {formatArgs(item.arguments) || "(resource only)"}</p>
          )}
          {item.ref && (
            <p className="text-xs">
              {item.ref.kind === "incident" ? (
                <Link className="text-blue-300 hover:underline" to={`/incidents/${item.ref.id}`}>Open {item.ref.number}</Link>
              ) : (
                <Link className="text-blue-300 hover:underline" to={`/investigations/${item.ref.id}`}>Open investigation</Link>
              )}
            </p>
          )}
          {item.resource_id && (
            <p className="text-xs">
              <Link className="text-blue-300 hover:underline" to={`/infrastructure/${item.resource_id}`}>Open {item.resource}</Link>
            </p>
          )}
          {(full?.output || item.excerpt) && (
            <div>
              <div className="mb-1 text-[11px] tracking-wide text-subtle uppercase">
                {full?.output ? "Output (redacted)" : "Excerpt (redacted)"}
              </div>
              <pre className="max-h-[50vh] overflow-auto rounded-lg border border-border bg-black/40 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-fg/85">
                {full?.output ?? item.excerpt}
              </pre>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
}

export function RootCauseCard({ report, onOpen }: { report: Report; onOpen: (id: string) => void }) {
  const rc = report.root_cause;
  if (!rc) {
    return (
      <div className="rounded-lg border border-border bg-surface-2 p-4">
        <div className="flex items-center gap-2 text-sm font-medium text-muted">
          <AlertTriangle className="size-4 text-amber-400" /> No root cause established
        </div>
        <p className="mt-1 text-xs text-subtle">
          The collected evidence does not support a cause. Unverified ideas are listed as alternative hypotheses, never as
          the root cause.
        </p>
      </div>
    );
  }
  const meta = ROOT_CAUSE_META[rc.status] ?? { label: "Probable cause", tone: "warning" as const };
  return (
    <div
      className={cn(
        "rounded-lg border p-4",
        rc.status === "confirmed" ? "border-success/40 bg-success/5" : "border-warning/40 bg-warning/5",
      )}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Target className={cn("size-4", rc.status === "confirmed" ? "text-green-400" : "text-amber-400")} />
        <Badge tone={meta.tone}>{meta.label}</Badge>
        <span className="font-mono text-xs text-muted">confidence {confidencePercent(rc.confidence)}</span>
      </div>
      <p className="mt-2 text-sm font-medium text-fg">{rc.statement}</p>
      <div className="mt-2 flex items-center gap-2 text-xs text-subtle">
        Evidence: <EvidenceChips ids={rc.evidence_ids} onOpen={onOpen} />
      </div>
      {rc.status === "probable" && (
        <p className="mt-2 text-[11px] text-amber-300/80">
          The evidence points to this cause but does not prove it (confirmation needs at least two successful tool results and
          high confidence).
        </p>
      )}
    </div>
  );
}

export function RcaReport({ inv, printable = false }: { inv: InvestigationDetail; printable?: boolean }) {
  const r = inv.result;
  const [open, setOpen] = useState<string | null>(null);
  const index = evidenceIndex(r);
  const openItem = open ? (index.get(open) ?? null) : null;
  const resolved = (r.resources ?? []).filter((x) => x.resolved);

  return (
    <Panel
      className={printable ? "print:border-0 print:bg-transparent" : undefined}
      title={<span className="flex items-center gap-2"><FileText className="size-4 text-green-400" /> Root cause analysis report</span>}
      actions={
        <div className="flex gap-2 print:hidden">
          <Button variant="secondary" size="sm" onClick={() => downloadText(`${slug(inv.title)}.md`, reportToMarkdown(inv))}>
            <Download /> Markdown
          </Button>
          {printable && (
            <Button variant="secondary" size="sm" onClick={() => window.print()}>
              <Printer /> Print
            </Button>
          )}
        </div>
      }
    >
      <div className="space-y-6">
        <div className="space-y-3">
          <p className="text-sm text-fg">{r.conclusion}</p>
          <RootCauseCard report={r} onOpen={setOpen} />
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-6">
            <Section n={1} title="Incident summary">
              <p>{r.incident_summary || r.summary || "—"}</p>
              {r.impact && <p className="mt-1 text-xs text-muted">Impact: {r.impact}</p>}
              {r.triage && (
                <p className="mt-1 text-xs text-muted">
                  Suggested severity <b className="text-fg">{r.triage.severity}</b> (priority {r.triage.priority})
                </p>
              )}
            </Section>
            <Section n={2} title="Affected resources">
              {resolved.length ? (
                <ul className="space-y-1">
                  {resolved.map((x) => (
                    <li key={x.resource_id} className="flex items-center gap-2">
                      <Link to={`/infrastructure/${x.resource_id}`} className="font-medium hover:underline">{x.name}</Link>
                      <span className="text-xs text-subtle">{x.resource_type}</span>
                      {x.health && <HealthBadge status={x.health as never} />}
                    </li>
                  ))}
                </ul>
              ) : (
                <Muted>No affected resource was resolved in the inventory.</Muted>
              )}
            </Section>
            <Section n={3} title="Timeline" icon={<History className="size-3.5" />}>
              {(r.timeline ?? []).length ? (
                <ol className="space-y-1 border-l border-border pl-3">
                  {(r.timeline ?? []).map((t) => (
                    <li key={t.evidence_id} className="text-xs">
                      <span className="font-mono text-subtle">{t.at ? new Date(t.at).toLocaleTimeString() : ""}</span>{" "}
                      <EvidenceChips ids={[t.evidence_id]} onOpen={setOpen} /> {t.summary}
                    </li>
                  ))}
                </ol>
              ) : (
                <Muted>No diagnostic tool ran.</Muted>
              )}
            </Section>
            <Section n={4} title="Observed symptoms">
              {(r.symptoms ?? []).length ? (
                <ul className="list-inside list-disc">{(r.symptoms ?? []).map((s) => <li key={s}>{s}</li>)}</ul>
              ) : (
                <Muted>None stated.</Muted>
              )}
            </Section>
            <Section n={5} title={`Diagnostic evidence (${r.evidence?.length ?? 0})`} icon={<Wrench className="size-3.5" />}>
              <ul className="space-y-1.5">
                {(r.evidence ?? []).map((e, i) => (
                  <li key={e.id ?? i} className="flex items-start gap-2 text-xs">
                    {e.id ? <EvidenceChips ids={[e.id]} onOpen={setOpen} /> : null}
                    <span className={cn("min-w-0", e.ok === false && "text-red-300")}>
                      <span className="text-subtle">{e.tool ?? e.source} · {e.resource ?? "—"}: </span>
                      {e.summary}
                      {e.observed_at && <span className="text-subtle"> · {formatRelative(e.observed_at)}</span>}
                    </span>
                  </li>
                ))}
              </ul>
              {(r.findings ?? []).length > 0 && (
                <div className="mt-3 space-y-1.5">
                  <div className="text-[11px] tracking-wide text-subtle uppercase">Findings</div>
                  {(r.findings ?? []).map((f, i) => (
                    <div key={`${f.statement}-${i}`} className="flex flex-wrap items-start gap-2 text-xs">
                      <Badge tone={FINDING_META[f.kind]?.tone ?? "neutral"}>{FINDING_META[f.kind]?.label ?? f.kind}</Badge>
                      <span className="min-w-0 flex-1">{f.statement}</span>
                      <EvidenceChips ids={f.evidence_ids} onOpen={setOpen} />
                    </div>
                  ))}
                </div>
              )}
            </Section>
            <Section n={6} title="Recent changes">
              {(r.recent_changes ?? []).length ? (
                <ul className="space-y-1">
                  {(r.recent_changes ?? []).map((c) => (
                    <li key={c.evidence_id} className="text-xs"><EvidenceChips ids={[c.evidence_id]} onOpen={setOpen} /> {c.summary}</li>
                  ))}
                </ul>
              ) : (
                <Muted>No change history was collected for this investigation.</Muted>
              )}
            </Section>
          </div>

          <div className="space-y-6">
            <Section n={7} title="Dependency analysis" icon={<GitBranch className="size-3.5" />}>
              {(r.dependency_analysis ?? []).map((d) => (
                <p key={d.evidence_id} className="text-xs">
                  <EvidenceChips ids={[d.evidence_id]} onOpen={setOpen} /> <b>{d.resource}</b> depends on{" "}
                  {d.depends_on.join(", ") || "nothing recorded"} · {d.dependents} dependent(s)
                </p>
              ))}
              {(r.change_impact?.impacts ?? []).map((i) => (
                <div key={i.resource_id} className="mt-2 rounded-lg border border-border bg-surface-2 p-3 text-xs">
                  <div className="flex items-center gap-2">
                    <b>{i.resource}</b>
                    <Badge tone={i.risk_level === "low" ? "success" : i.risk_level === "medium" ? "warning" : "danger"}>
                      risk {i.risk_level}
                    </Badge>
                  </div>
                  <p className="mt-1">Direct: {i.direct.join(", ") || "none"} · Indirect: {i.indirect.join(", ") || "none"}</p>
                  {i.active_incidents.length > 0 && <p>Active incidents: {i.active_incidents.join(", ")}</p>}
                  <ul className="mt-1 list-inside list-disc text-muted">{i.precautions.map((p) => <li key={p}>{p}</li>)}</ul>
                  <p className="mt-1 text-subtle">{i.required_approvals.join("; ")}</p>
                </div>
              ))}
              {!(r.dependency_analysis ?? []).length && !r.change_impact && <Muted>Not analysed.</Muted>}
            </Section>
            <Section n={8} title="Identified root cause" icon={<Target className="size-3.5" />}>
              {r.root_cause ? (
                <p>
                  <Badge tone={ROOT_CAUSE_META[r.root_cause.status]?.tone}>{r.root_cause.status}</Badge> {r.root_cause.statement}
                </p>
              ) : (
                <Muted>Not established.</Muted>
              )}
              {(r.alternative_hypotheses ?? []).length > 0 && (
                <div className="mt-2 space-y-1">
                  <div className="text-[11px] tracking-wide text-subtle uppercase">Alternative hypotheses (not confirmed)</div>
                  {(r.alternative_hypotheses ?? []).map((h) => (
                    <div key={h.statement} className="flex flex-wrap items-center gap-2 text-xs">
                      <Badge tone="warning">{h.likelihood}</Badge>
                      <span className="flex-1">{h.statement}</span>
                      <EvidenceChips ids={h.evidence_ids} onOpen={setOpen} />
                    </div>
                  ))}
                </div>
              )}
            </Section>
            <Section n={9} title="Contributing factors">
              {(r.contributing_factors ?? []).length ? (
                <ul className="space-y-1">
                  {(r.contributing_factors ?? []).map((c) => (
                    <li key={c.statement} className="flex flex-wrap items-center gap-2 text-xs">
                      <span className="flex-1">{c.statement}</span>
                      <EvidenceChips ids={c.evidence_ids} onOpen={setOpen} />
                    </li>
                  ))}
                </ul>
              ) : (
                <Muted>None identified.</Muted>
              )}
            </Section>
            <Section n={10} title="Confidence level">
              {r.root_cause ? (
                <p>
                  <span className="font-mono">{confidencePercent(r.confidence)}</span>{" "}
                  <span className="text-xs text-muted">({r.confidence_label})</span>
                </p>
              ) : (
                <Muted>No root cause, so no confidence is stated.</Muted>
              )}
            </Section>
            <Section n={11} title="Recommended remediation" icon={<Lightbulb className="size-3.5" />}>
              {(r.recommended_remediation ?? []).length ? (
                <ol className="space-y-2">
                  {(r.recommended_remediation ?? []).map((m, i) => (
                    <li key={m.action} className="rounded-lg border border-border bg-surface-2 p-2.5 text-xs">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-subtle">{i + 1}.</span>
                        <span className="flex-1 font-medium text-fg">{m.action}</span>
                        <Badge tone={m.risk === "low" ? "success" : m.risk === "medium" ? "warning" : "danger"}>risk {m.risk}</Badge>
                      </div>
                      {m.rationale && <p className="mt-1 text-muted">{m.rationale}</p>}
                      <div className="mt-1"><EvidenceChips ids={m.evidence_ids} onOpen={setOpen} /></div>
                    </li>
                  ))}
                </ol>
              ) : (
                <Muted>No remediation suggested.</Muted>
              )}
              <p className="mt-2 flex items-center gap-1.5 text-[11px] text-subtle">
                <ShieldCheck className="size-3" /> The analysis is advisory; actions run only from the Remediation plan, after policy checks and approval.
              </p>
            </Section>
            <Section n={12} title="Preventive recommendations">
              {(r.preventive_recommendations ?? []).length ? (
                <ul className="list-inside list-disc text-xs">{(r.preventive_recommendations ?? []).map((p) => <li key={p}>{p}</li>)}</ul>
              ) : (
                <Muted>None.</Muted>
              )}
            </Section>
          </div>
        </div>

        {(r.evidence_gaps ?? []).length > 0 && (
          <Alert tone="warning">
            <div className="font-medium">Evidence gaps</div>
            <ul className="mt-1 list-inside list-disc text-xs">{(r.evidence_gaps ?? []).map((g) => <li key={g}>{g}</li>)}</ul>
          </Alert>
        )}

        {((r.similar_incidents ?? []).length > 0 || (r.similar_investigations ?? []).length > 0) && (
          <div className="space-y-1.5 print:hidden">
            <div className="text-xs font-semibold tracking-wide text-muted uppercase">Similar past issues</div>
            {[...(r.similar_incidents ?? []), ...(r.similar_investigations ?? [])].map((s) => (
              <Link
                key={s.id}
                to={s.kind === "incident" ? `/incidents/${s.id}` : `/investigations/${s.id}`}
                className="flex items-center gap-2 rounded-lg border border-border bg-surface-2 px-3 py-2 text-xs hover:border-border-strong"
              >
                <CheckCircle2 className="size-3.5 text-subtle" />
                <span className="font-mono text-subtle">{s.number ?? "INV"}</span>
                <span className="flex-1 truncate">{s.title}</span>
                <span className="truncate text-muted">{s.root_cause ?? s.resolution ?? s.conclusion ?? ""}</span>
              </Link>
            ))}
          </div>
        )}
      </div>
      <EvidenceDrawer inv={inv} item={openItem} onClose={() => setOpen(null)} />
    </Panel>
  );
}
