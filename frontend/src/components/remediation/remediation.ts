import type { Tone } from "@/components/ui/display";
import type {
  ApprovalStatus,
  InvestigationEvent,
  RemediationSection,
  RemediationStep,
  RemediationStepStatus,
  RiskLevel,
} from "@/types/api";

export const RISK_META: Record<RiskLevel, { label: string; tone: Tone }> = {
  low: { label: "Low risk", tone: "success" },
  medium: { label: "Medium risk", tone: "warning" },
  high: { label: "High risk", tone: "danger" },
  critical: { label: "Critical", tone: "danger" },
};

export const APPROVAL_STATUS_META: Record<ApprovalStatus, { label: string; tone: Tone }> = {
  pending: { label: "Pending", tone: "warning" },
  approved: { label: "Approved", tone: "primary" },
  rejected: { label: "Rejected", tone: "danger" },
  changes_requested: { label: "Changes requested", tone: "violet" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  expired: { label: "Expired", tone: "neutral" },
  executed: { label: "Executed", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

/** Live states shown while a step is in flight, on top of the persisted ones. */
export type LiveStepStatus = RemediationStepStatus | "awaiting_request" | "awaiting_approval" | "executing";

export const STEP_STATUS_META: Record<LiveStepStatus, { label: string; tone: Tone }> = {
  proposed: { label: "Proposed", tone: "neutral" },
  advisory: { label: "Advisory only", tone: "neutral" },
  blocked: { label: "Blocked by policy", tone: "danger" },
  awaiting_request: { label: "Ready to request", tone: "warning" },
  awaiting_approval: { label: "Pending approval", tone: "warning" },
  approved: { label: "Approved", tone: "primary" },
  changes_requested: { label: "Changes requested", tone: "violet" },
  rejected: { label: "Rejected", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  expired: { label: "Expired", tone: "neutral" },
  skipped: { label: "Skipped", tone: "neutral" },
  dropped: { label: "Dropped", tone: "neutral" },
  executing: { label: "Executing", tone: "primary" },
  executed: { label: "Verifying", tone: "primary" },
  failed: { label: "Failed", tone: "danger" },
  verified: { label: "Verified", tone: "success" },
  unverified: { label: "Not verified", tone: "danger" },
  not_run: { label: "Not run", tone: "neutral" },
};

export const SECTION_META: Record<RemediationSection["status"], { label: string; tone: Tone }> = {
  none: { label: "No remediation", tone: "neutral" },
  skipped: { label: "Not planned", tone: "neutral" },
  advisory: { label: "Advisory", tone: "neutral" },
  planned: { label: "In progress", tone: "primary" },
  stopped: { label: "Stopped", tone: "danger" },
  verified: { label: "Verified", tone: "success" },
  unverified: { label: "Not verified", tone: "danger" },
  not_executed: { label: "Not executed", tone: "neutral" },
};

export type LiveStep = Omit<RemediationStep, "status"> & { status: LiveStepStatus };

export interface LiveRemediation {
  status: RemediationSection["status"];
  reason: string | null;
  notes: string[];
  steps: LiveStep[];
  /** The step the run is paused on, if any. */
  waiting: { kind: "approval" | "remediation_step"; stepIndex: number; approvalId?: string } | null;
}

const num = (v: unknown) => (typeof v === "number" ? v : Number(v));
const text = (v: unknown) => (typeof v === "string" ? v : v == null ? null : String(v));

/**
 * The remediation part of an investigation: the persisted report section when the run
 * has finished, otherwise rebuilt from the live events so the view fills in as it runs.
 */
export function liveRemediation(
  section: RemediationSection | null | undefined,
  events: InvestigationEvent[],
): LiveRemediation | null {
  let state: LiveRemediation | null = null;
  const patch = (index: number, change: Partial<LiveStep>) => {
    if (!state) return;
    state.steps = state.steps.map((s) => (s.index === index ? { ...s, ...change } : s));
  };
  for (const e of events) {
    const d = e.event_data;
    const idx = num(d.step_index);
    switch (e.event_type) {
      case "remediation.skipped":
        state = { status: "skipped", reason: text(d.reason), notes: [], steps: [], waiting: null };
        break;
      case "remediation.planned":
        state = {
          status: (text(d.status) as RemediationSection["status"]) ?? "planned",
          reason: text(d.reason),
          notes: (d.notes as string[] | undefined) ?? [],
          steps: ((d.steps as RemediationStep[] | undefined) ?? []).map((s) => ({ ...s })),
          waiting: null,
        };
        break;
      case "remediation.awaiting_request":
        patch(idx, { status: "awaiting_request" });
        if (state) state.waiting = { kind: "remediation_step", stepIndex: idx };
        break;
      case "approval.requested":
        patch(idx, { status: "awaiting_approval", approval_id: text(d.approval_id) });
        break;
      case "approval.awaiting":
        patch(idx, { status: "awaiting_approval", approval_id: text(d.approval_id) });
        if (state) state.waiting = { kind: "approval", stepIndex: idx, approvalId: text(d.approval_id) ?? undefined };
        break;
      case "approval.auto_approved":
        patch(idx, { status: "approved", approval_id: text(d.approval_id) });
        break;
      case "approval.decided": {
        const status = text(d.status);
        const mapped: Record<string, LiveStepStatus> = {
          approved: "approved",
          rejected: "rejected",
          cancelled: "cancelled",
          expired: "expired",
          changes_requested: "changes_requested",
        };
        if (status && mapped[status]) patch(idx, { status: mapped[status], status_reason: text(d.comment) });
        if (state) state.waiting = null;
        break;
      }
      case "remediation.step_requested":
        if (state) state.waiting = null;
        break;
      case "remediation.step_skipped":
        patch(idx, { status: "skipped", status_reason: text(d.reason) ?? (d.by ? `Skipped by ${text(d.by)}` : null) });
        if (state) state.waiting = null;
        break;
      case "remediation.revised": {
        const step = d.step as RemediationStep | undefined;
        if (step && state) state.steps = state.steps.map((s) => (s.index === step.index ? { ...step } : s));
        break;
      }
      case "execution.started":
        patch(idx, { status: "executing" });
        break;
      case "execution.completed":
        patch(idx, {
          status: d.status === "succeeded" ? "executed" : "failed",
          job_id: text(d.job_id),
          execution: { status: text(d.status) ?? "", summary: text(d.summary) ?? "" },
          status_reason: d.status === "succeeded" ? null : text(d.summary),
        });
        break;
      case "verification.completed":
        patch(idx, {
          status: d.status === "passed" ? "verified" : "unverified",
          verification: {
            status: d.status === "passed" ? "passed" : "failed",
            summary: text(d.summary) ?? "",
            reasons: (d.reasons as string[] | undefined) ?? [],
          },
        });
        break;
      default:
        break;
    }
  }
  if (section) {
    return { status: section.status, reason: section.reason, notes: section.notes, steps: section.steps, waiting: null };
  }
  return state;
}

/** "in 12 min", "in 1 h 5 min", "expired". */
export function timeLeft(expiresAt: string, now: number = Date.now()): string {
  const ms = new Date(expiresAt).getTime() - now;
  if (ms <= 0) return "expired";
  const minutes = Math.ceil(ms / 60_000);
  if (minutes < 60) return `in ${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `in ${h} h ${m} min` : `in ${h} h`;
}

export function toolLabel(tool: string): string {
  return tool.replace(/_/g, " ");
}
