import type { LiveRemediation } from "@/components/remediation/remediation";
import type { InvestigationEvent, InvestigationStatus, ReportTask } from "@/types/api";

/**
 * The investigation as a workflow of agent steps, rebuilt from the event stream.
 *
 * Mirrors the LangGraph pipeline: classify → match inventory → plan → specialist agents
 * (in parallel) → aggregate → root cause → remediation (plan → approval → execute →
 * verify) → report. Every step's live state comes from the same events the activity log
 * shows, so the diagram fills in as the agents work.
 */

export type FlowStatus = "pending" | "running" | "waiting" | "done" | "failed" | "skipped";

export interface FlowTool {
  key: number;
  name: string;
  status: "running" | "ok" | "failed";
  resource: string | null;
  summary: string | null;
}

export interface FlowNode {
  id: string;
  label: string;
  agent: string;
  status: FlowStatus;
  detail: string | null;
  durationMs: number | null;
  runs: number;
  tools: FlowTool[];
}

export type FlowStage =
  | { kind: "single"; id: string; status: FlowStatus; node: FlowNode }
  | { kind: "parallel"; id: "agents"; status: FlowStatus; label: string; nodes: FlowNode[] };

export interface Flow {
  stages: FlowStage[];
  /** The stage the run moves to once the current one finishes (only while running). */
  next: string | null;
  /** Agents working right now. */
  running: number;
  done: number;
  total: number;
}

const STEPS: { id: string; label: string; agent: string }[] = [
  { id: "classify", label: "Understand request", agent: "supervisor" },
  { id: "resolve_resources", label: "Match inventory", agent: "supervisor" },
  { id: "plan_tasks", label: "Plan investigation", agent: "supervisor" },
  { id: "agents", label: "Specialist agents", agent: "" },
  { id: "aggregate", label: "Aggregate evidence", agent: "supervisor" },
  { id: "analyse_root_cause", label: "Root cause analysis", agent: "root_cause_analysis" },
  { id: "plan_remediation", label: "Plan remediation", agent: "remediation_planner" },
  { id: "request_approval", label: "Approval gate", agent: "policy" },
  { id: "execute_remediation", label: "Execute change", agent: "execution_gateway" },
  { id: "verify_remediation", label: "Verify fix", agent: "verification" },
  { id: "report", label: "Final report", agent: "supervisor" },
];

const ACTIVE_STATUSES: InvestigationStatus[] = ["queued", "running", "awaiting_input", "awaiting_approval"];
const str = (v: unknown) => (typeof v === "string" ? v : v == null ? null : String(v));
const num = (v: unknown) => (typeof v === "number" ? v : null);

function blank(id: string, label: string, agent: string): FlowNode {
  return { id, label, agent, status: "pending", detail: null, durationMs: null, runs: 0, tools: [] };
}

/** Tool calls per task: started → finished, matched in order per tool name. */
function toolsByTask(events: InvestigationEvent[]): Map<string, FlowTool[]> {
  const byTask = new Map<string, FlowTool[]>();
  for (const e of events) {
    if (!e.event_type.startsWith("tool.")) continue;
    const task = str(e.event_data.task_id);
    const name = str(e.event_data.tool);
    if (!task || !name) continue;
    const list = byTask.get(task) ?? [];
    if (e.event_type === "tool.started") {
      list.push({ key: e.seq, name, status: "running", resource: null, summary: null });
    } else {
      const open = list.find((t) => t.name === name && t.status === "running");
      const finished: Partial<FlowTool> = {
        status: e.event_type === "tool.completed" ? "ok" : "failed",
        resource: str(e.event_data.resource),
        summary: str(e.event_data.summary) ?? str(e.event_data.error),
      };
      if (open) Object.assign(open, finished);
      else list.push({ key: e.seq, name, status: "failed", resource: null, summary: null, ...finished });
    }
    byTask.set(task, list);
  }
  return byTask;
}

function taskStatus(status: string | undefined): FlowStatus {
  switch (status) {
    case "running":
      return "running";
    case "completed":
    case "partial":
      return "done";
    case "failed":
      return "failed";
    case "skipped":
      return "skipped";
    default:
      return "pending";
  }
}

function stageStatus(nodes: FlowNode[]): FlowStatus {
  if (!nodes.length) return "pending";
  if (nodes.some((n) => n.status === "running")) return "running";
  if (nodes.some((n) => n.status === "waiting")) return "waiting";
  if (nodes.every((n) => n.status === "pending")) return "pending";
  if (nodes.some((n) => n.status === "pending")) return "running";
  if (nodes.every((n) => n.status === "skipped")) return "skipped";
  if (nodes.every((n) => n.status === "failed")) return "failed";
  return "done";
}

export function buildFlow(
  events: InvestigationEvent[],
  status: InvestigationStatus,
  tasks: ReportTask[],
  remediation: LiveRemediation | null,
): Flow {
  const nodes = new Map(STEPS.map((s) => [s.id, blank(s.id, s.label, s.agent)]));
  const node = (id: string) => nodes.get(id) as FlowNode;

  for (const e of events) {
    const d = e.event_data;
    const step = str(d.node);
    if (step && step !== "run_agent" && nodes.has(step)) {
      const n = node(step);
      if (e.event_type === "agent.started") {
        n.status = "running";
        n.runs += 1;
        n.detail = null;
      } else if (e.event_type === "agent.completed") {
        n.status = "done";
        n.durationMs = num(d.duration_ms);
      } else if (e.event_type === "agent.failed") {
        n.status = "failed";
        n.durationMs = num(d.duration_ms);
        n.detail = str(d.error);
      }
    }
    switch (e.event_type) {
      case "supervisor.classified":
        node("classify").detail = str(d.summary);
        break;
      case "clarification.requested":
        node("classify").status = "waiting";
        node("classify").detail = str(d.question) ?? "Waiting for your answer";
        break;
      case "supervisor.plan_created": {
        const count = Array.isArray(d.tasks) ? d.tasks.length : 0;
        node("plan_tasks").detail = `${count} task${count === 1 ? "" : "s"} for ${count === 1 ? "one agent" : "specialist agents"}`;
        break;
      }
      case "evidence.aggregated":
        node("aggregate").detail =
          num(d.evidence_items) != null ? `${num(d.evidence_items)} evidence items · ${num(d.tool_calls) ?? 0} tool calls` : null;
        break;
      case "remediation.skipped":
        node("plan_remediation").detail = str(d.reason);
        break;
      default:
        break;
    }
  }

  // Specialist agents: one card per planned task, with its live tool calls.
  const tools = toolsByTask(events);
  const agents: FlowNode[] = tasks.map((t) => ({
    ...blank(t.id, t.objective, t.agent),
    status: taskStatus(t.status),
    detail: t.status === "skipped" || t.status === "failed" ? (t.reason ?? null) : (t.summary ?? null),
    tools: tools.get(t.id) ?? [],
  }));
  for (const e of events) {
    if (e.event_data.node !== "run_agent" || (e.event_type !== "agent.completed" && e.event_type !== "agent.failed")) continue;
    const a = agents.find((x) => x.id === str(e.event_data.task_id));
    if (a) a.durationMs = num(e.event_data.duration_ms);
  }

  // Remediation branch: approval and execution come from the remediation state.
  const steps = remediation?.steps ?? [];
  const approval = node("request_approval");
  const execute = node("execute_remediation");
  if (remediation?.waiting || status === "awaiting_approval") {
    approval.status = "waiting";
    approval.detail = remediation?.waiting?.kind === "remediation_step" ? "Ready to request approval" : "Waiting for an approver";
  } else if (steps.some((s) => ["approved", "executing", "executed", "verified", "unverified", "failed"].includes(s.status))) {
    approval.status = "done";
    approval.detail = "Approved";
  } else if (steps.some((s) => s.status === "rejected")) {
    approval.status = "failed";
    approval.detail = "Rejected";
  }
  if (steps.some((s) => s.status === "executing")) {
    execute.status = "running";
  } else if (steps.some((s) => s.status === "failed")) {
    execute.status = "failed";
  } else if (steps.some((s) => ["executed", "verified", "unverified"].includes(s.status))) {
    execute.status = "done";
  }
  const proposed = steps.filter((s) => s.status !== "advisory" && s.status !== "blocked").length;
  if (remediation && remediation.status !== "planned" && !proposed) {
    for (const id of ["request_approval", "execute_remediation", "verify_remediation"]) {
      const n = node(id);
      if (n.status === "pending") {
        n.status = "skipped";
        n.detail = remediation.status === "advisory" ? "Advisory steps only" : "No change needed";
      }
    }
  }

  // Finished runs: steps that never ran were not needed (or not reached).
  const active = ACTIVE_STATUSES.includes(status);
  if (!active) {
    const reached = status === "completed";
    for (const n of [...nodes.values(), ...agents]) {
      if (n.status === "running") n.status = reached ? "done" : "failed";
      if (n.status === "pending" || n.status === "waiting") {
        n.status = "skipped";
        n.detail = n.detail ?? (reached ? "Not needed" : "Not reached");
      }
    }
  }
  if (status === "awaiting_input" && node("classify").status !== "done") node("classify").status = "waiting";

  const stages: FlowStage[] = STEPS.map((s) => {
    if (s.id === "agents") {
      return { kind: "parallel", id: "agents", label: s.label, nodes: agents, status: stageStatus(agents) };
    }
    const n = node(s.id);
    return { kind: "single", id: s.id, status: n.status, node: n };
  });

  // The next stop: the first pending stage after the furthest one that has started.
  let next: string | null = null;
  if (active) {
    let furthest = -1;
    stages.forEach((st, i) => {
      if (st.status !== "pending" && st.status !== "skipped") furthest = i;
    });
    next = stages.slice(furthest + 1).find((st) => st.status === "pending")?.id ?? null;
  }

  const all = [...[...nodes.values()].filter((n) => n.id !== "agents"), ...agents];
  return {
    stages,
    next,
    running: all.filter((n) => n.status === "running").length,
    done: all.filter((n) => n.status === "done").length,
    total: all.filter((n) => n.status !== "skipped").length,
  };
}
