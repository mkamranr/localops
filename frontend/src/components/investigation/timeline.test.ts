import { describe, expect, it } from "vitest";
import { collapseTimeline } from "@/components/investigation/InvestigationWorkspace";
import { agentLabel, describeEvent, formatDuration } from "@/components/investigation/shared";
import type { InvestigationEvent } from "@/types/api";

const ev = (seq: number, event_type: string, event_data: Record<string, unknown> = {}, agent_name: string | null = "supervisor"): InvestigationEvent => ({
  seq,
  event_type,
  agent_name,
  event_data,
  created_at: "2026-01-01T00:00:00Z",
});

describe("investigation timeline", () => {
  it("hides started lines for finished steps only", () => {
    const events = [
      ev(1, "agent.started", { node: "classify" }),
      ev(2, "agent.completed", { node: "classify", duration_ms: 1200 }),
      ev(3, "agent.started", { node: "plan_tasks" }),
    ];
    expect(collapseTimeline(events).map((e) => e.seq)).toEqual([2, 3]);
  });

  it("closes tool starts one-for-one, keeping calls still running", () => {
    const t = { task_id: "t1", tool: "linux_journal" };
    const events = [
      ev(1, "tool.started", t, "infrastructure_diagnostics"),
      ev(2, "tool.started", t, "infrastructure_diagnostics"),
      ev(3, "tool.completed", { ...t, evidence_id: "E1.1" }, "infrastructure_diagnostics"),
      ev(4, "tool.started", { task_id: "t1", tool: "linux_top_processes" }, "infrastructure_diagnostics"),
      ev(5, "tool.failed", { task_id: "t1", tool: "linux_top_processes" }, "infrastructure_diagnostics"),
    ];
    expect(collapseTimeline(events).map((e) => e.seq)).toEqual([2, 3, 5]);
  });

  it("keys steps by task so parallel agents do not hide each other", () => {
    const events = [
      ev(1, "agent.started", { node: "run_agent", task_id: "t1" }, "infrastructure_diagnostics"),
      ev(2, "agent.started", { node: "run_agent", task_id: "t2" }, "infrastructure_diagnostics"),
      ev(3, "agent.completed", { node: "run_agent", task_id: "t1" }, "infrastructure_diagnostics"),
    ];
    expect(collapseTimeline(events).map((e) => e.seq)).toEqual([2, 3]);
  });

  it("describes events in plain language", () => {
    expect(describeEvent(ev(1, "agent.skipped", { objective: "Check disk", reason: "not yet" }, "network_diagnostics")).title).toBe(
      "Network Diagnostics skipped",
    );
    expect(describeEvent(ev(2, "supervisor.classified", { request_type: "incident_investigation", urgency: "high", confidence: 0.84 })).detail).toContain("confidence 84%");
    expect(describeEvent(ev(3, "something.new")).title).toBe("something.new");
    expect(agentLabel(null)).toBe("System");
    expect(formatDuration(1500)).toBe("1.5 s");
    expect(formatDuration(65_000)).toBe("1m 5s");
  });
});
