import {
  BadgeCheck,
  Bot,
  Brain,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Database,
  FileText,
  Hourglass,
  Layers,
  ListChecks,
  Loader2,
  Play,
  ShieldCheck,
  SkipForward,
  Target,
  Wrench,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";
import type { Flow, FlowNode, FlowStatus, FlowTool } from "@/components/investigation/flow";
import { agentLabel, formatDuration } from "@/components/investigation/shared";
import { toolLabel } from "@/components/remediation/remediation";
import { Panel } from "@/components/ui/display";
import { cn } from "@/utils/cn";

const STEP_ICON: Record<string, LucideIcon> = {
  classify: Brain,
  resolve_resources: Database,
  plan_tasks: ListChecks,
  aggregate: Layers,
  analyse_root_cause: Target,
  plan_remediation: Wrench,
  request_approval: ShieldCheck,
  execute_remediation: Play,
  verify_remediation: BadgeCheck,
  report: FileText,
};

const STATUS: Record<FlowStatus, { icon: LucideIcon; label: string; tone: string; card: string }> = {
  pending: { icon: CircleDashed, label: "Queued", tone: "text-subtle", card: "border-dashed border-border-strong bg-surface-2/40" },
  running: {
    icon: Loader2,
    label: "Running",
    tone: "text-blue-300",
    card: "flow-live border-primary/70 bg-primary/10",
  },
  waiting: { icon: Hourglass, label: "Waiting", tone: "text-amber-300", card: "border-warning/60 bg-warning/10" },
  done: { icon: CheckCircle2, label: "Done", tone: "text-green-400", card: "border-success/35 bg-success/5" },
  failed: { icon: XCircle, label: "Failed", tone: "text-red-400", card: "border-danger/50 bg-danger/10" },
  skipped: { icon: SkipForward, label: "Skipped", tone: "text-subtle", card: "border-border bg-surface-2/30 opacity-55" },
};

function StatusPill({ status, next }: { status: FlowStatus; next?: boolean }) {
  if (next) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-primary/50 bg-primary/10 px-1.5 py-px text-[10px] font-medium tracking-wide text-blue-300 uppercase">
        <ChevronRight className="size-3" /> Next
      </span>
    );
  }
  const meta = STATUS[status];
  return (
    <span className={cn("inline-flex items-center gap-1 text-[10px] font-medium tracking-wide uppercase", meta.tone)}>
      <meta.icon className={cn("size-3", status === "running" && "animate-spin")} /> {meta.label}
    </span>
  );
}

type Link = "done" | "flowing" | "next" | "idle";

/** The arrow between two stages: solid when passed, animated while work flows through it. */
function Connector({ state }: { state: Link }) {
  return (
    <div className="flex w-10 shrink-0 items-center" aria-hidden>
      <span
        className={cn(
          "h-0.5 flex-1 rounded-full",
          state === "done" && "bg-success/50",
          state === "flowing" && "flow-dash",
          state === "next" && "border-t-2 border-dashed border-primary/50 bg-transparent",
          state === "idle" && "bg-border",
        )}
      />
      <ChevronRight
        className={cn(
          "-ml-1.5 size-4 shrink-0",
          state === "done" ? "text-success/60" : state === "idle" ? "text-border-strong" : "text-blue-400",
        )}
      />
    </div>
  );
}

function linkBetween(from: FlowStatus, to: FlowStatus, toIsNext: boolean): Link {
  if (to === "running" || to === "waiting") return "flowing";
  if (toIsNext && (from === "running" || from === "waiting")) return "next";
  if (toIsNext) return "flowing";
  if (from !== "pending" && from !== "skipped" && to !== "pending" && to !== "skipped") return "done";
  if (from === "done" && to === "skipped") return "done";
  return "idle";
}

function ToolLine({ tool }: { tool: FlowTool }) {
  return (
    <li className="flex min-w-0 items-center gap-1.5 text-[11px]">
      {tool.status === "running" ? (
        <Loader2 className="size-3 shrink-0 animate-spin text-blue-300" />
      ) : tool.status === "ok" ? (
        <CheckCircle2 className="size-3 shrink-0 text-green-400/80" />
      ) : (
        <XCircle className="size-3 shrink-0 text-red-400/80" />
      )}
      <span className={cn("truncate font-mono", tool.status === "running" ? "text-blue-200" : "text-muted")}>
        {toolLabel(tool.name)}
        {tool.resource && <span className="text-subtle"> · {tool.resource}</span>}
      </span>
    </li>
  );
}

function StepCard({ node, next, live }: { node: FlowNode; next: boolean; live: (el: HTMLElement | null) => void }) {
  const Icon = STEP_ICON[node.id] ?? Bot;
  const meta = STATUS[node.status];
  return (
    <div
      ref={node.status === "running" || node.status === "waiting" ? live : undefined}
      className={cn(
        "w-48 shrink-0 rounded-xl border p-3 transition-colors",
        next ? "border-dashed border-primary/60 bg-primary/5" : meta.card,
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className={cn("grid size-7 place-items-center rounded-lg bg-surface-3", node.status === "running" ? "text-blue-300" : meta.tone)}>
          <Icon className="size-4" />
        </span>
        <StatusPill status={node.status} next={next} />
      </div>
      <p className="mt-2 text-sm leading-tight font-medium text-fg">{node.label}</p>
      <p className="mt-0.5 text-[11px] text-subtle">
        {agentLabel(node.agent)}
        {node.runs > 1 && ` · ${node.runs} runs`}
        {node.durationMs != null && node.status !== "running" && ` · ${formatDuration(node.durationMs)}`}
      </p>
      {node.detail && <p className="mt-1.5 line-clamp-2 text-[11px] text-muted" title={node.detail}>{node.detail}</p>}
    </div>
  );
}

function AgentCard({ node, live }: { node: FlowNode; live: (el: HTMLElement | null) => void }) {
  const meta = STATUS[node.status];
  const running = node.tools.filter((t) => t.status === "running");
  const shown = [...running, ...node.tools.filter((t) => t.status !== "running").slice(-Math.max(0, 3 - running.length))];
  const hidden = node.tools.length - shown.length;
  return (
    <div
      ref={node.status === "running" ? live : undefined}
      className={cn("w-64 shrink-0 rounded-xl border p-3 transition-colors", meta.card)}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <span className={cn("grid size-7 shrink-0 place-items-center rounded-lg bg-surface-3", node.status === "running" ? "text-blue-300" : meta.tone)}>
            <Bot className="size-4" />
          </span>
          <span className="truncate text-sm font-medium">{agentLabel(node.agent)}</span>
        </span>
        <StatusPill status={node.status} />
      </div>
      <p className="mt-2 line-clamp-2 text-xs text-fg/85" title={node.label}>{node.label}</p>
      {shown.length > 0 && (
        <ul className="mt-2 space-y-1 border-t border-border/70 pt-2">
          {shown.map((t) => <ToolLine key={t.key} tool={t} />)}
          {hidden > 0 && <li className="text-[10px] text-subtle">+{hidden} earlier tool call{hidden === 1 ? "" : "s"}</li>}
        </ul>
      )}
      {node.detail && node.status !== "running" && (
        <p className="mt-1.5 line-clamp-2 text-[11px] text-muted" title={node.detail}>{node.detail}</p>
      )}
      {node.durationMs != null && node.status !== "running" && (
        <p className="mt-1 font-mono text-[10px] text-subtle">
          {formatDuration(node.durationMs)} · {node.tools.length} tool call{node.tools.length === 1 ? "" : "s"}
        </p>
      )}
    </div>
  );
}

/** Fan-out from the plan into parallel agents, and fan-in to the aggregation step. */
function ParallelAgents({ nodes, status, live }: { nodes: FlowNode[]; status: FlowStatus; live: (el: HTMLElement | null) => void }) {
  const line = status === "pending" || status === "skipped" ? "bg-border" : status === "done" ? "bg-success/50" : "bg-primary/60";
  if (!nodes.length) {
    return (
      <div className="flex w-60 shrink-0 flex-col items-center justify-center gap-1 rounded-xl border border-dashed border-border-strong bg-surface-2/40 p-4 text-center">
        <Bot className="size-5 text-subtle" />
        <p className="text-xs text-muted">Specialist agents</p>
        <p className="text-[11px] text-subtle">{status === "skipped" ? "None were needed" : "Assigned once the plan is ready"}</p>
      </div>
    );
  }
  return (
    <div className="flex shrink-0 flex-col" role="group" aria-label="Specialist agents running in parallel">
      {nodes.map((n, i) => (
        <div key={n.id} className="relative flex items-center py-1.5">
          {/* Spines joining the branches: each row draws the half above and below its centre. */}
          {i > 0 && (
            <>
              <span className={cn("absolute top-0 left-0 h-1/2 w-0.5", line)} />
              <span className={cn("absolute top-0 right-0 h-1/2 w-0.5", line)} />
            </>
          )}
          {i < nodes.length - 1 && (
            <>
              <span className={cn("absolute top-1/2 left-0 h-1/2 w-0.5", line)} />
              <span className={cn("absolute top-1/2 right-0 h-1/2 w-0.5", line)} />
            </>
          )}
          <span className={cn("h-0.5 w-4 shrink-0", line)} />
          <AgentCard node={n} live={live} />
          <span className={cn("h-0.5 w-4 shrink-0", line)} />
        </div>
      ))}
    </div>
  );
}

function Legend() {
  const items: [string, string][] = [
    ["flow-live border-primary/70 bg-primary/20", "Running"],
    ["border-warning/60 bg-warning/20", "Waiting"],
    ["border-success/40 bg-success/15", "Done"],
    ["border-dashed border-primary/60", "Next"],
    ["border-dashed border-border-strong", "Queued"],
  ];
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border pt-3 text-[11px] text-subtle">
      {items.map(([cls, label]) => (
        <span key={label} className="inline-flex items-center gap-1.5">
          <span className={cn("size-2.5 rounded-sm border", cls)} /> {label}
        </span>
      ))}
      <span className="inline-flex items-center gap-1.5">
        <span className="flow-dash h-0.5 w-6 rounded-full" /> Work flowing
      </span>
    </div>
  );
}

export function AgentFlow({ flow, actions }: { flow: Flow; actions?: ReactNode }) {
  const scroller = useRef<HTMLDivElement>(null);
  const liveEl = useRef<HTMLElement | null>(null);
  const live = (el: HTMLElement | null) => {
    if (el) liveEl.current = el;
  };
  const focus = flow.stages.find((s) => s.status === "running" || s.status === "waiting")?.id ?? flow.next;

  // Keep the step that is working right now in view as the run advances.
  useEffect(() => {
    const box = scroller.current;
    const el = liveEl.current;
    if (!box || !el || !box.contains(el)) return;
    const left = el.offsetLeft - box.clientWidth / 2 + el.clientWidth / 2;
    box.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
  }, [focus]);

  const summary = (
    <span className="flex items-center gap-3 text-xs text-muted">
      {flow.running > 0 && (
        <span className="inline-flex items-center gap-1.5 text-blue-300">
          <span className="relative flex size-2">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-blue-400 opacity-60" />
            <span className="relative inline-flex size-2 rounded-full bg-blue-400" />
          </span>
          {flow.running} agent{flow.running === 1 ? "" : "s"} working
        </span>
      )}
      <span className="font-mono tabular-nums">
        {flow.done}/{flow.total} steps
      </span>
      {actions}
    </span>
  );

  return (
    <Panel title="Agent workflow" actions={summary}>
      <div ref={scroller} className="overflow-x-auto pb-2">
        <div className="flex min-w-max items-center py-1">
          {flow.stages.map((stage, i) => {
            const prev = flow.stages[i - 1];
            return (
              <div key={stage.id} className="flex items-center">
                {prev && <Connector state={linkBetween(prev.status, stage.status, flow.next === stage.id)} />}
                {stage.kind === "parallel" ? (
                  <ParallelAgents nodes={stage.nodes} status={stage.status} live={live} />
                ) : (
                  <StepCard node={stage.node} next={flow.next === stage.id} live={live} />
                )}
              </div>
            );
          })}
        </div>
      </div>
      <Legend />
    </Panel>
  );
}
