import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Ban,
  Brain,
  CheckCircle2,
  CircleDashed,
  FileText,
  Info,
  ListChecks,
  Loader2,
  MessageCircleQuestion,
  Radio,
  Send,
  SkipForward,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { investigationsApi } from "@/api/endpoints";
import {
  ACTIVE,
  agentLabel,
  describeEvent,
  formatDuration,
  InvestigationStatusBadge,
  requestTypeLabel,
  urgencyTone,
} from "@/components/investigation/shared";
import { Button } from "@/components/ui/button";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner } from "@/components/ui/display";
import { Input } from "@/components/ui/form";
import { useInvestigationStream, type StreamState } from "@/hooks/useInvestigationStream";
import { useAuthStore } from "@/stores/authStore";
import type { InvestigationDetail, InvestigationEvent, ReportTask } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime } from "@/utils/format";

interface Classification {
  request_type?: string;
  summary?: string;
  affected_entities?: string[];
  symptoms?: string[];
  urgency?: string;
  confidence?: number;
}

/** Combine the persisted report with live events so the view fills in as it runs. */
function useDerived(inv: InvestigationDetail | undefined, events: InvestigationEvent[]) {
  return useMemo(() => {
    const report = inv?.result ?? {};
    let classification: Classification | null = report.summary
      ? {
          request_type: report.request_type ?? undefined,
          summary: report.summary,
          affected_entities: report.affected_entities,
          symptoms: report.symptoms,
          urgency: report.urgency ?? undefined,
          confidence: report.classification_confidence ?? undefined,
        }
      : null;
    let tasks: ReportTask[] = report.tasks ?? [];
    const outcomes = new Map<string, { status: string; reason: string | null }>();
    for (const e of events) {
      if (e.event_type === "supervisor.classified" && !report.summary) {
        classification = e.event_data as Classification;
      }
      if (e.event_type === "supervisor.plan_created" && !report.tasks) {
        tasks = ((e.event_data.tasks as ReportTask[]) ?? []).map((t) => ({
          ...t,
          status: "pending",
          reason: null,
        }));
      }
      if (e.event_type === "agent.skipped") {
        outcomes.set(String(e.event_data.task_id), {
          status: "skipped",
          reason: String(e.event_data.reason ?? ""),
        });
      }
    }
    if (!report.tasks) {
      tasks = tasks.map((t) => ({ ...t, ...(outcomes.get(t.id) ?? {}) }));
    }
    return { classification, tasks };
  }, [inv, events]);
}

function ConfidenceRing({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const r = 42;
  const c = 2 * Math.PI * r;
  const color = pct >= 70 ? "var(--color-success)" : pct >= 40 ? "var(--color-warning)" : "var(--color-danger)";
  return (
    <div className="relative size-24 shrink-0">
      <svg viewBox="0 0 100 100" className="size-full -rotate-90">
        <circle cx="50" cy="50" r={r} fill="none" stroke="var(--color-surface-3)" strokeWidth="7" />
        <circle cx="50" cy="50" r={r} fill="none" stroke={color} strokeWidth="7" strokeLinecap="round" strokeDasharray={c} strokeDashoffset={c - (c * pct) / 100} />
      </svg>
      <div className="absolute inset-0 grid place-items-center text-center">
        <div>
          <div className="font-mono text-lg font-semibold" style={{ color }}>
            {(pct / 100).toFixed(2)}
          </div>
          <div className="text-[8px] tracking-wider text-subtle uppercase">confidence</div>
        </div>
      </div>
    </div>
  );
}

function ReasoningPanel({
  classification,
  waiting,
}: {
  classification: Classification | null;
  waiting: boolean;
}) {
  return (
    <Panel title={<span className="flex items-center gap-2"><Brain className="size-4 text-violet-400" /> Supervisor reasoning</span>}>
      {!classification ? (
        waiting ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" /> Classifying request…
          </div>
        ) : (
          <EmptyState title="No classification" description="The request was not classified." />
        )
      ) : (
        <div className="space-y-4">
          <div className="flex items-center gap-4">
            <ConfidenceRing value={classification.confidence ?? 0} />
            <div className="space-y-1.5">
              <Badge tone="violet" className="capitalize">{requestTypeLabel(classification.request_type)}</Badge>
              <div>
                <Badge tone={urgencyTone(classification.urgency)} className="capitalize">
                  {classification.urgency ?? "unknown"} urgency
                </Badge>
              </div>
            </div>
          </div>
          {classification.summary && <p className="text-sm text-fg/90">{classification.summary}</p>}
          <div>
            <div className="mb-1.5 text-xs font-medium tracking-wide text-muted uppercase">Affected entities</div>
            {classification.affected_entities?.length ? (
              <div className="flex flex-wrap gap-1.5">
                {classification.affected_entities.map((e) => (
                  <span key={e} className="rounded-md border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 font-mono text-xs text-sky-300">{e}</span>
                ))}
              </div>
            ) : (
              <p className="text-xs text-subtle">None identified</p>
            )}
          </div>
          {!!classification.symptoms?.length && (
            <div>
              <div className="mb-1.5 text-xs font-medium tracking-wide text-muted uppercase">Symptoms</div>
              <ul className="list-inside list-disc space-y-0.5 text-sm text-fg/80">
                {classification.symptoms.map((s) => <li key={s}>{s}</li>)}
              </ul>
            </div>
          )}
          <p className="flex gap-1.5 text-[11px] text-subtle">
            <Info className="mt-0.5 size-3 shrink-0" />
            Model-generated classification. Entities are not yet verified against the inventory.
          </p>
        </div>
      )}
    </Panel>
  );
}

function StreamIndicator({ state }: { state: StreamState }) {
  const meta = {
    connecting: { label: "connecting", tone: "neutral" as const },
    live: { label: "live", tone: "success" as const },
    reconnecting: { label: "reconnecting", tone: "warning" as const },
    ended: { label: "complete", tone: "neutral" as const },
  }[state];
  return (
    <Badge tone={meta.tone}>
      <Radio className={cn("size-3", state === "live" && "animate-pulse")} /> {meta.label}
    </Badge>
  );
}

/** Hide "started" lines once the same step has finished, keeping in-progress ones. */
export function collapseTimeline(events: InvestigationEvent[]): InvestigationEvent[] {
  const finished = new Set(
    events
      .filter((e) => e.event_type === "agent.completed" || e.event_type === "agent.failed")
      .map((e) => `${e.agent_name}:${String(e.event_data.node)}`),
  );
  return events.filter(
    (e) =>
      e.event_type !== "agent.started" ||
      !finished.has(`${e.agent_name}:${String(e.event_data.node)}`),
  );
}

function Timeline({ events: all, state }: { events: InvestigationEvent[]; state: StreamState }) {
  const events = useMemo(() => collapseTimeline(all), [all]);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [events.length]);
  return (
    <Panel title="Agent timeline" actions={<StreamIndicator state={state} />} bodyClassName="max-h-[520px] overflow-y-auto">
      {!events.length ? (
        <Spinner label="Waiting for events…" />
      ) : (
        <ol className="relative space-y-0">
          {events.map((e, i) => {
            const v = describeEvent(e);
            return (
              <li key={e.seq} className="relative flex gap-3 pb-4">
                {i < events.length - 1 && <span className="absolute top-6 left-[9px] h-[calc(100%-18px)] w-px bg-border" />}
                <v.icon className={cn("relative mt-0.5 size-[18px] shrink-0", v.tone)} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-3">
                    <p className="text-sm text-fg">{v.title}</p>
                    <time className="shrink-0 font-mono text-[11px] text-subtle">
                      {new Date(e.created_at).toLocaleTimeString()}
                    </time>
                  </div>
                  {v.detail && <p className="mt-0.5 text-xs break-words text-muted">{v.detail}</p>}
                </div>
              </li>
            );
          })}
          <div ref={end} />
        </ol>
      )}
    </Panel>
  );
}

function PlanPanel({ tasks, waiting }: { tasks: ReportTask[]; waiting: boolean }) {
  return (
    <Panel title={<span className="flex items-center gap-2"><ListChecks className="size-4 text-blue-400" /> Investigation plan</span>}>
      {!tasks.length ? (
        waiting ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted">
            <Loader2 className="size-4 animate-spin" /> Waiting for the plan…
          </div>
        ) : (
          <EmptyState title="No tasks planned" />
        )
      ) : (
        <ol className="space-y-2">
          {tasks.map((t, i) => (
            <li key={t.id} className="rounded-lg border border-border bg-surface-2 p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="flex items-center gap-2 text-sm font-medium">
                  <span className="grid size-5 place-items-center rounded bg-surface-3 font-mono text-[11px] text-muted">{i + 1}</span>
                  {agentLabel(t.agent)}
                </span>
                {t.status === "skipped" ? (
                  <Badge tone="warning"><SkipForward className="size-3" /> skipped</Badge>
                ) : t.status === "completed" ? (
                  <Badge tone="success"><CheckCircle2 className="size-3" /> done</Badge>
                ) : (
                  <Badge><CircleDashed className="size-3" /> {t.status.replace("_", " ")}</Badge>
                )}
              </div>
              <p className="mt-1.5 text-sm text-fg/90">{t.objective}</p>
              {t.rationale && <p className="mt-1 text-xs text-muted">{t.rationale}</p>}
              {t.reason && <p className="mt-1.5 text-[11px] text-amber-300/80">{t.reason}</p>}
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}

function ClarificationBox({ inv }: { inv: InvestigationDetail }) {
  const [answer, setAnswer] = useState("");
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => investigationsApi.resume(inv.id, answer.trim()),
    onSuccess: () => {
      setAnswer("");
      void queryClient.invalidateQueries({ queryKey: ["investigation", inv.id] });
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (answer.trim()) mutation.mutate();
  };
  return (
    <div className="panel border-warning/40 bg-warning/5 p-4">
      <div className="flex items-start gap-3">
        <MessageCircleQuestion className="mt-0.5 size-5 shrink-0 text-amber-400" />
        <div className="min-w-0 flex-1 space-y-3">
          <div>
            <p className="text-xs font-medium tracking-wide text-amber-300 uppercase">The Supervisor needs more information</p>
            <p className="mt-1 text-sm text-fg">{inv.pending_question}</p>
          </div>
          <form onSubmit={submit} className="flex gap-2">
            <Input value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer…" maxLength={2000} aria-label="Answer" autoFocus />
            <Button type="submit" loading={mutation.isPending} disabled={!answer.trim()}>
              <Send /> Answer
            </Button>
          </form>
          {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
        </div>
      </div>
    </div>
  );
}

function ReportPanel({ inv }: { inv: InvestigationDetail }) {
  const r = inv.result;
  if (inv.status !== "completed" || !r.conclusion) return null;
  return (
    <Panel title={<span className="flex items-center gap-2"><FileText className="size-4 text-green-400" /> Investigation report</span>}>
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="space-y-3">
          <div>
            <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">Conclusion</div>
            <p className="text-sm text-fg">{r.conclusion}</p>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">Root cause</div>
            <p className="text-sm text-muted">{r.root_cause ?? "Not determined — no supporting evidence was collected."}</p>
          </div>
          {!r.evidence_sufficient && (
            <Alert tone="warning">
              Evidence is insufficient. Diagnostic agents that collect evidence from your infrastructure arrive in a
              later release; until then reports state only what was classified and planned.
            </Alert>
          )}
        </div>
        <div className="space-y-3">
          <div>
            <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">Recommended next steps</div>
            <ol className="space-y-1.5">
              {(r.recommended_next_steps ?? []).map((s, i) => (
                <li key={s} className="flex gap-2 text-sm text-fg/90">
                  <span className="grid size-5 shrink-0 place-items-center rounded bg-surface-3 font-mono text-[11px] text-muted">{i + 1}</span>
                  {s}
                </li>
              ))}
            </ol>
          </div>
          {!!r.clarifications?.length && (
            <div>
              <div className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">Clarifications</div>
              {r.clarifications.map((c) => (
                <p key={c.question} className="text-xs text-muted">
                  <span className="text-fg/80">{c.question}</span> — {c.answer}
                </p>
              ))}
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

export function InvestigationWorkspace({ id, compact = false }: { id: string; compact?: boolean }) {
  const queryClient = useQueryClient();
  const canInvestigate = useAuthStore((s) => s.hasPermission("incident.investigate"));
  const query = useQuery({
    queryKey: ["investigation", id],
    queryFn: () => investigationsApi.get(id),
  });
  const { events, state } = useInvestigationStream(id);
  const { classification, tasks } = useDerived(query.data, events);
  const cancel = useMutation({
    mutationFn: () => investigationsApi.cancel(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["investigation", id] }),
  });

  // While running, show a ticking duration.
  const [now, setNow] = useState(Date.now());
  const inv = query.data;
  const active = inv ? ACTIVE.includes(inv.status) : false;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);

  if (query.isLoading) return <Spinner />;
  if (query.error || !inv) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;

  const end = inv.completed_at ? new Date(inv.completed_at).getTime() : now;
  const duration = end - new Date(inv.started_at).getTime();

  return (
    <div className="space-y-5">
      <div className="panel flex flex-wrap items-center gap-x-6 gap-y-3 px-5 py-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-3">
            <h1 className="truncate text-lg font-semibold">{inv.title}</h1>
            <InvestigationStatusBadge status={inv.status} />
          </div>
          {!compact && <p className="mt-1 line-clamp-2 text-sm text-muted">{inv.request}</p>}
        </div>
        <dl className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs">
          <div><dt className="text-subtle uppercase">Mode</dt><dd className="capitalize">{inv.execution_mode}</dd></div>
          <div><dt className="text-subtle uppercase">Started</dt><dd>{formatDateTime(inv.started_at)}</dd></div>
          <div><dt className="text-subtle uppercase">Duration</dt><dd className="tabular-nums">{formatDuration(duration)}</dd></div>
          <div><dt className="text-subtle uppercase">By</dt><dd>{inv.username ?? "—"}</dd></div>
        </dl>
        {canInvestigate && active && (
          <Button variant="secondary" size="sm" onClick={() => cancel.mutate()} loading={cancel.isPending} disabled={inv.cancel_requested}>
            <Ban /> {inv.cancel_requested ? "Cancelling…" : "Cancel"}
          </Button>
        )}
      </div>

      {cancel.error && <Alert>{errorMessage(cancel.error)}</Alert>}
      {inv.status === "failed" && inv.error && <Alert>{inv.error}</Alert>}
      {inv.status === "awaiting_input" && canInvestigate && <ClarificationBox inv={inv} />}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)_minmax(0,1fr)]">
        <ReasoningPanel classification={classification} waiting={active} />
        <Timeline events={events} state={state} />
        <PlanPanel tasks={tasks} waiting={active} />
      </div>

      <ReportPanel inv={inv} />
    </div>
  );
}
