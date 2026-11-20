import { describe, expect, it } from "vitest";
import { buildFlow, type FlowStage } from "@/components/investigation/flow";
import type { LiveRemediation } from "@/components/remediation/remediation";
import type { InvestigationEvent, ReportTask } from "@/types/api";

let seq = 0;
const ev = (event_type: string, agent: string | null, event_data: Record<string, unknown> = {}): InvestigationEvent => ({
  seq: ++seq,
  event_type,
  agent_name: agent,
  event_data,
  created_at: "2026-11-20T10:00:00Z",
});
const node = (step: string, type = "agent.completed", extra: Record<string, unknown> = {}) =>
  ev(type, "supervisor", { node: step, duration_ms: 1200, ...extra });

const TASKS: ReportTask[] = [
  { id: "t1", agent: "linux_diagnostics", objective: "Check disk on web-01", rationale: "", status: "running", reason: null },
  { id: "t2", agent: "log_analysis", objective: "Scan nginx errors", rationale: "", status: "pending", reason: null },
];

const stage = (stages: FlowStage[], id: string) => stages.find((s) => s.id === id) as FlowStage;

describe("agent workflow", () => {
  it("shows parallel agents running with live tool calls and points to the next step", () => {
    const events = [
      node("classify", "agent.started"),
      node("classify"),
      node("resolve_resources", "agent.started"),
      node("resolve_resources"),
      node("plan_tasks", "agent.started"),
      ev("supervisor.plan_created", "supervisor", { tasks: TASKS }),
      node("plan_tasks"),
      ev("agent.started", "linux_diagnostics", { node: "run_agent", task_id: "t1" }),
      ev("tool.started", "linux_diagnostics", { task_id: "t1", tool: "disk_usage" }),
      ev("tool.completed", "linux_diagnostics", { task_id: "t1", tool: "disk_usage", resource: "web-01", summary: "/var 97%" }),
      ev("tool.started", "linux_diagnostics", { task_id: "t1", tool: "top_processes" }),
    ];
    const flow = buildFlow(events, "running", TASKS, null);

    expect(stage(flow.stages, "classify").status).toBe("done");
    expect(stage(flow.stages, "plan_tasks").status).toBe("done");
    const agents = stage(flow.stages, "agents");
    expect(agents.kind).toBe("parallel");
    expect(agents.status).toBe("running");
    if (agents.kind !== "parallel") throw new Error("expected parallel stage");
    const linux = agents.nodes[0]!;
    const logs = agents.nodes[1]!;
    expect(linux.status).toBe("running");
    expect(linux.tools.map((t) => [t.name, t.status])).toEqual([
      ["disk_usage", "ok"],
      ["top_processes", "running"],
    ]);
    expect(linux.tools[0]?.resource).toBe("web-01");
    expect(logs.status).toBe("pending");
    expect(flow.next).toBe("aggregate");
    expect(flow.running).toBe(1);
  });

  it("marks the approval gate as waiting and the rest of the branch as upcoming", () => {
    const remediation: LiveRemediation = {
      status: "planned",
      reason: null,
      notes: [],
      steps: [{ index: 0, status: "awaiting_approval" } as LiveRemediation["steps"][number]],
      waiting: { kind: "approval", stepIndex: 0, approvalId: "a1" },
    };
    const done = TASKS.map((t) => ({ ...t, status: "completed" }));
    const events = ["classify", "resolve_resources", "plan_tasks", "aggregate", "analyse_root_cause", "plan_remediation"].map((s) => node(s));
    const flow = buildFlow(events, "awaiting_approval", done, remediation);
    expect(stage(flow.stages, "agents").status).toBe("done");
    expect(stage(flow.stages, "request_approval").status).toBe("waiting");
    expect(stage(flow.stages, "execute_remediation").status).toBe("pending");
    expect(flow.next).toBe("execute_remediation");
  });

  it("skips unneeded steps once the run completes and shows clarification waits", () => {
    const remediation: LiveRemediation = { status: "skipped", reason: "No fix needed", notes: [], steps: [], waiting: null };
    const done = TASKS.map((t) => ({ ...t, status: t.id === "t2" ? "skipped" : "completed", reason: t.id === "t2" ? "No logs" : null }));
    const events = ["classify", "resolve_resources", "plan_tasks", "aggregate", "analyse_root_cause", "plan_remediation", "report"].map((s) => node(s));
    const flow = buildFlow(events, "completed", done, remediation);
    expect(stage(flow.stages, "request_approval").status).toBe("skipped");
    expect(stage(flow.stages, "verify_remediation").status).toBe("skipped");
    expect(stage(flow.stages, "report").status).toBe("done");
    expect(flow.next).toBeNull();

    const asking = buildFlow(
      [node("classify", "agent.started"), node("classify"), ev("clarification.requested", null, { question: "Which host?" })],
      "awaiting_input",
      [],
      null,
    );
    const classify = stage(asking.stages, "classify");
    expect(classify.status).toBe("waiting");
    if (classify.kind === "single") expect(classify.node.detail).toBe("Which host?");
  });
});
