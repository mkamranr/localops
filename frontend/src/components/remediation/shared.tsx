import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  BadgeCheck,
  CircleDashed,
  ExternalLink,
  Hammer,
  Loader2,
  PlayCircle,
  ShieldAlert,
  ShieldCheck,
  SkipForward,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { investigationsApi } from "@/api/endpoints";
import { formatArgs, formatDuration } from "@/components/investigation/shared";
import {
  APPROVAL_STATUS_META,
  RISK_META,
  SECTION_META,
  STEP_STATUS_META,
  type LiveRemediation,
  type LiveStep,
  type LiveStepStatus,
} from "@/components/remediation/remediation";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, Panel, Table, Td, Th } from "@/components/ui/display";
import { useAuthStore } from "@/stores/authStore";
import type { ApprovalStatus, Execution, RiskLevel } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, formatRelative } from "@/utils/format";

export function RiskBadge({ risk }: { risk: RiskLevel }) {
  const meta = RISK_META[risk] ?? RISK_META.high;
  return (
    <Badge tone={meta.tone} className={cn(risk === "critical" && "font-semibold uppercase")}>
      {meta.label}
    </Badge>
  );
}

export function ApprovalStatusBadge({ status, auto }: { status: ApprovalStatus; auto?: boolean }) {
  const meta = APPROVAL_STATUS_META[status] ?? { label: status, tone: "neutral" as const };
  return (
    <Badge tone={meta.tone}>
      {meta.label}
      {auto && " · auto"}
    </Badge>
  );
}

const STEP_ICON: Partial<Record<LiveStepStatus, typeof CircleDashed>> = {
  executing: Loader2,
  executed: Loader2,
  verified: BadgeCheck,
  awaiting_approval: ShieldCheck,
  awaiting_request: ShieldCheck,
  skipped: SkipForward,
};

export function StepStatusBadge({ status }: { status: LiveStepStatus }) {
  const meta = STEP_STATUS_META[status] ?? { label: status, tone: "neutral" as const };
  const Icon = STEP_ICON[status];
  return (
    <Badge tone={meta.tone}>
      {Icon && <Icon className={cn("size-3", (status === "executing" || status === "executed") && "animate-spin")} />}
      {meta.label}
    </Badge>
  );
}

/** Executions (remediation jobs) with before/after state and the verification verdict. */
export function ExecutionsTable({ rows, showResource = true }: { rows: Execution[]; showResource?: boolean }) {
  const [open, setOpen] = useState<Execution | null>(null);
  if (!rows.length) {
    return (
      <EmptyState
        title="No remediation executed"
        description="Approved remediation actions appear here with their output and verification."
      />
    );
  }
  return (
    <>
      <Table>
        <thead>
          <tr>
            <Th>Action</Th>
            {showResource && <Th>Resource</Th>}
            <Th>Risk</Th>
            <Th>Result</Th>
            <Th>Verification</Th>
            <Th>Duration</Th>
            <Th>When</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((x) => (
            <tr key={x.id} className="cursor-pointer hover:bg-surface-2/60" onClick={() => setOpen(x)}>
              <Td className="max-w-sm">
                <div className="font-mono text-xs">{x.tool}</div>
                <div className="truncate text-[11px] text-muted">{x.description}</div>
              </Td>
              {showResource && <Td className="text-xs">{x.resource_name ?? "—"}</Td>}
              <Td><RiskBadge risk={x.risk_level} /></Td>
              <Td>
                <Badge tone={x.status === "succeeded" ? "success" : x.status === "running" ? "primary" : "danger"}>{x.status}</Badge>
              </Td>
              <Td>
                <Badge tone={x.verification_status === "passed" ? "success" : x.verification_status === "failed" ? "danger" : "neutral"}>
                  {x.verification_status}
                </Badge>
              </Td>
              <Td className="text-xs text-muted">{formatDuration(x.duration_ms)}</Td>
              <Td className="text-xs text-muted">{formatRelative(x.started_at)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <Dialog
        wide
        open={!!open}
        onOpenChange={(o) => !o && setOpen(null)}
        title={open ? `${open.tool} on ${open.resource_name ?? "—"}` : ""}
        description={open ? `${formatDateTime(open.started_at)} · ${open.actor ?? "system"}` : undefined}
      >
        {open && <ExecutionDetail job={open} />}
      </Dialog>
    </>
  );
}

function StateList({ title, state }: { title: string; state: Execution["before"] }) {
  const entries = Object.entries(state ?? {});
  return (
    <div className="rounded-lg border border-border bg-surface-2 p-3">
      <div className="mb-1 text-[11px] font-medium tracking-wide text-muted uppercase">{title}</div>
      {entries.length ? (
        entries.map(([tool, v]) => (
          <p key={tool} className="text-xs">
            <span className="font-mono text-cyan-300">{tool}</span>: <span className="text-fg/85">{v.summary}</span>
          </p>
        ))
      ) : (
        <p className="text-xs text-subtle">Not captured</p>
      )}
    </div>
  );
}

export function ExecutionDetail({ job }: { job: Execution }) {
  const r = job.execution_result ?? {};
  return (
    <div className="space-y-3">
      <p className="text-sm">{job.description}</p>
      <div className="flex flex-wrap gap-2">
        <RiskBadge risk={job.risk_level} />
        <Badge tone={job.status === "succeeded" ? "success" : "danger"}>{job.status}</Badge>
        <Badge tone={job.verification_status === "passed" ? "success" : job.verification_status === "failed" ? "danger" : "neutral"}>
          verification {job.verification_status}
        </Badge>
      </div>
      {r.error && <Alert tone="warning">{r.error}</Alert>}
      {r.summary && <p className="text-sm text-fg/85">{r.summary}</p>}
      <p className="font-mono text-[11px] text-subtle">parameters: {formatArgs(job.parameters) || "(resource only)"}</p>
      <div className="grid gap-2 sm:grid-cols-2">
        <StateList title="Before" state={job.before} />
        <StateList title="After" state={job.after} />
      </div>
      {job.verification?.summary && (
        <Alert tone={job.verification_status === "passed" ? "success" : "danger"}>{job.verification.summary}</Alert>
      )}
      {r.output && (
        <pre className="max-h-[40vh] overflow-auto rounded-lg border border-border bg-black/40 p-3 font-mono text-[11px] whitespace-pre-wrap text-fg/85">
          {r.output}
        </pre>
      )}
      {job.approval_id && (
        <Link to={`/approvals/${job.approval_id}`} className="inline-flex items-center gap-1 text-xs text-blue-300 hover:underline">
          <ExternalLink className="size-3" /> Approval record
        </Link>
      )}
    </div>
  );
}

function StepCard({ step, investigationId, canAct, waitingHere }: { step: LiveStep; investigationId?: string; canAct: boolean; waitingHere: "approval" | "remediation_step" | null }) {
  const queryClient = useQueryClient();
  const act = useMutation({
    mutationFn: (action: "request" | "skip") => investigationsApi.remediationStep(investigationId ?? "", step.index, action),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["investigation", investigationId] }),
  });
  return (
    <li className="rounded-lg border border-border bg-surface-2 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2 text-sm font-medium">
          <span className="grid size-5 place-items-center rounded bg-surface-3 font-mono text-[11px] text-muted">{step.index + 1}</span>
          <span className="font-mono text-xs">{step.tool}</span>
          <span className="text-xs text-muted">on {step.resource_name}</span>
        </span>
        <span className="flex items-center gap-1.5">
          <RiskBadge risk={step.risk_level} />
          <StepStatusBadge status={step.status} />
        </span>
      </div>
      <p className="mt-2 text-sm text-fg/90">{step.description}</p>
      {step.rationale && <p className="mt-1 text-xs text-muted">{step.rationale}</p>}
      {step.expected_impact && <p className="mt-1 text-xs text-amber-200/80">Impact: {step.expected_impact}</p>}
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-subtle">
        {!!step.evidence_ids?.length && <span>evidence {step.evidence_ids.join(", ")}</span>}
        {!!step.policy?.matched_rules?.length && <span>policy: {step.policy.matched_rules.join(", ")}</span>}
        {step.revisions > 0 && <span>revised {step.revisions}×</span>}
      </div>
      {step.status_reason && <p className="mt-1.5 text-xs text-amber-300/90">{step.status_reason}</p>}
      {step.execution?.summary && step.status !== "failed" && <p className="mt-1.5 text-xs text-fg/80">{step.execution.summary}</p>}
      {step.verification && (
        <p className={cn("mt-1.5 text-xs", step.verification.status === "passed" ? "text-green-300" : "text-red-300")}>{step.verification.summary}</p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {step.approval_id && (
          <Link to={`/approvals/${step.approval_id}`} className="inline-flex items-center gap-1 text-xs text-blue-300 hover:underline">
            <ShieldCheck className="size-3" /> Approval
          </Link>
        )}
        {waitingHere === "remediation_step" && canAct && investigationId && (
          <>
            <Button size="sm" onClick={() => act.mutate("request")} loading={act.isPending && act.variables === "request"}>
              <ShieldCheck /> Request approval
            </Button>
            <Button size="sm" variant="secondary" onClick={() => act.mutate("skip")} loading={act.isPending && act.variables === "skip"}>
              <SkipForward /> Skip
            </Button>
          </>
        )}
      </div>
      {act.error && <div className="mt-2"><Alert>{errorMessage(act.error)}</Alert></div>}
    </li>
  );
}

/** The remediation plan of an investigation with each step's live status. */
export function RemediationPanel({ data, investigationId }: { data: LiveRemediation; investigationId?: string }) {
  const canRemediate = useAuthStore((s) => s.hasPermission("tool.execute.remediation"));
  const canInvestigate = useAuthStore((s) => s.hasPermission("incident.investigate"));
  const meta = SECTION_META[data.status] ?? SECTION_META.none;
  return (
    <Panel
      title={<span className="flex items-center gap-2"><Hammer className="size-4 text-violet-400" /> Remediation</span>}
      actions={<Badge tone={meta.tone}>{meta.label}</Badge>}
    >
      {data.reason && <p className="mb-3 text-sm text-muted">{data.reason}</p>}
      {data.steps.length ? (
        <ol className="space-y-2">
          {data.steps.map((s) => (
            <StepCard
              key={`${s.index}-${s.revisions}`}
              step={s}
              investigationId={investigationId}
              canAct={canRemediate && canInvestigate}
              waitingHere={data.waiting?.stepIndex === s.index ? data.waiting.kind : null}
            />
          ))}
        </ol>
      ) : (
        !data.reason && <EmptyState title="No remediation proposed" />
      )}
      {data.notes.filter(Boolean).map((n) => (
        <p key={n} className="mt-2 text-xs text-subtle">{n}</p>
      ))}
      <p className="mt-3 flex gap-1.5 text-[11px] text-subtle">
        <ShieldAlert className="mt-0.5 size-3 shrink-0" />
        Only registered remediation tools can run, each after policy checks and, unless policy allows unattended
        execution, a second person's approval. Results are verified by re-checking the system.
      </p>
    </Panel>
  );
}

/** Banner while the run is paused on a remediation step. */
export function AwaitingApprovalBanner({ data, description }: { data: LiveRemediation | null; description: string | null }) {
  const waiting = data?.waiting;
  const step = data?.steps.find((s) => s.index === waiting?.stepIndex);
  const manual = waiting?.kind === "remediation_step";
  return (
    <div className="panel border-warning/40 bg-warning/5 p-4">
      <div className="flex items-start gap-3">
        {manual ? <PlayCircle className="mt-0.5 size-5 shrink-0 text-amber-400" /> : <ShieldCheck className="mt-0.5 size-5 shrink-0 text-amber-400" />}
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium tracking-wide text-amber-300 uppercase">
            {manual ? "Manual mode: request approval for the proposed step, or skip it" : "Waiting for an approver"}
          </p>
          <p className="mt-1 text-sm text-fg">{step?.description ?? description}</p>
          {!manual && (step?.approval_id ?? waiting?.approvalId) && (
            <Link
              to={`/approvals/${step?.approval_id ?? waiting?.approvalId}`}
              className="mt-2 inline-flex items-center gap-1 text-sm text-blue-300 hover:underline"
            >
              <ExternalLink className="size-3.5" /> Open the approval request
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
