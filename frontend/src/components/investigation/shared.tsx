import {
  AlertTriangle,
  Ban,
  Bot,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  CirclePause,
  Clock,
  GitBranch,
  ListChecks,
  Loader2,
  MessageCircleQuestion,
  PlayCircle,
  SkipForward,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { Badge, type Tone } from "@/components/ui/display";
import type { InvestigationEvent, InvestigationStatus } from "@/types/api";
import { cn } from "@/utils/cn";

export const STATUS_META: Record<
  InvestigationStatus,
  { label: string; tone: Tone; icon: LucideIcon; spin?: boolean }
> = {
  queued: { label: "Queued", tone: "neutral", icon: Clock },
  running: { label: "Running", tone: "primary", icon: Loader2, spin: true },
  awaiting_input: { label: "Needs input", tone: "warning", icon: MessageCircleQuestion },
  completed: { label: "Completed", tone: "success", icon: CheckCircle2 },
  failed: { label: "Failed", tone: "danger", icon: AlertTriangle },
  cancelled: { label: "Cancelled", tone: "neutral", icon: Ban },
};

export const ACTIVE: InvestigationStatus[] = ["queued", "running", "awaiting_input"];

export function InvestigationStatusBadge({ status }: { status: InvestigationStatus }) {
  const meta = STATUS_META[status];
  return (
    <Badge tone={meta.tone}>
      <meta.icon className={cn("size-3", meta.spin && "animate-spin")} />
      {meta.label}
    </Badge>
  );
}

export function agentLabel(agentType: string | null | undefined): string {
  if (!agentType) return "System";
  return agentType
    .split("_")
    .map((w) => (w === "rca" ? "RCA" : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

export function requestTypeLabel(value: string | null | undefined): string {
  return value ? value.replace(/_/g, " ") : "unclassified";
}

export function urgencyTone(urgency: string | null | undefined): Tone {
  return (
    ({ critical: "danger", high: "danger", medium: "warning", low: "success" } as Record<string, Tone>)[
      urgency ?? ""
    ] ?? "neutral"
  );
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

interface EventView {
  icon: LucideIcon;
  tone: string;
  title: string;
  detail?: string;
}

const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));

/** Human-readable rendering of one event. */
export function describeEvent(e: InvestigationEvent): EventView {
  const d = e.event_data;
  const agent = agentLabel(e.agent_name);
  switch (e.event_type) {
    case "investigation.queued":
      return { icon: Clock, tone: "text-subtle", title: "Investigation queued", detail: `${str(d.execution_mode)} mode · requested by ${str(d.requested_by)}` };
    case "investigation.started":
      return { icon: PlayCircle, tone: "text-blue-400", title: "Supervisor started the investigation", detail: str(d.workflow) };
    case "investigation.resumed":
      return { icon: PlayCircle, tone: "text-blue-400", title: `Investigation resumed (attempt ${str(d.attempt)})`, detail: "Continuing from the last checkpoint" };
    case "agent.started":
      return { icon: CircleDot, tone: "text-blue-300", title: `${agent} · ${str(d.node).replace(/_/g, " ")}` };
    case "agent.completed": {
      const tokens = Number(d.prompt_tokens ?? 0) + Number(d.completion_tokens ?? 0);
      const parts = [formatDuration(Number(d.duration_ms))];
      if (Number(d.llm_calls)) parts.push(`${str(d.llm_calls)} model call${Number(d.llm_calls) > 1 ? "s" : ""}`);
      if (tokens) parts.push(`${tokens} tokens`);
      if (d.model) parts.push(str(d.model));
      return { icon: CheckCircle2, tone: "text-green-400", title: `${agent} finished ${str(d.node).replace(/_/g, " ")}`, detail: parts.join(" · ") };
    }
    case "agent.failed":
      return { icon: AlertTriangle, tone: "text-red-400", title: `${agent} failed in ${str(d.node).replace(/_/g, " ")}`, detail: str(d.error) };
    case "agent.skipped":
      return { icon: SkipForward, tone: "text-amber-400", title: `${agent} skipped`, detail: `${str(d.objective)} — ${str(d.reason)}` };
    case "supervisor.classified": {
      const conf = Math.round(Number(d.confidence ?? 0) * 100);
      return { icon: Sparkles, tone: "text-violet-400", title: `Classified as ${requestTypeLabel(str(d.request_type))}`, detail: `urgency ${str(d.urgency)} · confidence ${conf}%${d.needs_clarification ? " · needs clarification" : ""}` };
    }
    case "resources.resolved":
      return { icon: GitBranch, tone: "text-sky-400", title: `Affected entities: ${(d.entities as string[] | undefined)?.join(", ") || "none identified"}`, detail: str(d.note) };
    case "supervisor.plan_created": {
      const n = (d.tasks as unknown[] | undefined)?.length ?? 0;
      return { icon: ListChecks, tone: "text-violet-400", title: `Investigation plan: ${n} task${n === 1 ? "" : "s"}` };
    }
    case "clarification.requested":
      return { icon: MessageCircleQuestion, tone: "text-amber-400", title: "Supervisor needs more information", detail: str(d.question) };
    case "clarification.answered":
      return { icon: MessageCircleQuestion, tone: "text-green-400", title: `Answered by ${str(d.answered_by)}`, detail: str(d.answer) };
    case "evidence.aggregated":
      return { icon: Bot, tone: "text-sky-400", title: "Evidence aggregated", detail: `${str(d.evidence_items)} evidence items · ${str(d.skipped)} task(s) skipped · ${d.sufficient ? "sufficient" : "insufficient"}` };
    case "report.generated":
      return { icon: CheckCircle2, tone: "text-violet-400", title: "Report generated" };
    case "investigation.completed":
      return { icon: CheckCircle2, tone: "text-green-400", title: "Investigation completed" };
    case "investigation.failed":
      return { icon: AlertTriangle, tone: "text-red-400", title: "Investigation failed", detail: str(d.error) };
    case "investigation.cancel_requested":
      return { icon: CirclePause, tone: "text-amber-400", title: `Cancellation requested by ${str(d.requested_by)}` };
    case "investigation.cancelled":
      return { icon: Ban, tone: "text-subtle", title: "Investigation cancelled" };
    default:
      return { icon: CircleDashed, tone: "text-subtle", title: e.event_type };
  }
}
