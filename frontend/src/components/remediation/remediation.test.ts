import { describe, expect, it } from "vitest";
import { describeEvent } from "@/components/investigation/shared";
import { EMPTY_FORM, formToRules, policyToForm, summariseEffect, summariseMatch } from "@/components/remediation/policy";
import { APPROVAL_STATUS_META, liveRemediation, STEP_STATUS_META, timeLeft } from "@/components/remediation/remediation";
import type { ApprovalStatus, InvestigationEvent, Policy, RemediationSection } from "@/types/api";

const ev = (seq: number, event_type: string, event_data: Record<string, unknown> = {}): InvestigationEvent => ({
  seq,
  event_type,
  agent_name: "supervisor",
  event_data,
  created_at: "2026-11-19T10:00:00Z",
});

const step = {
  index: 0,
  tool: "docker_container_restart",
  arguments: { resource_id: "r1", timeout_seconds: 10 },
  resource_id: "r1",
  resource_name: "migrate",
  resource_type: "docker_container",
  description: "Restart the Docker container 'migrate'",
  risk_level: "medium",
  status: "proposed",
  status_reason: null,
  approval_id: null,
  job_id: null,
  execution: null,
  verification: null,
  revisions: 0,
};

describe("live remediation", () => {
  it("follows a step from plan to verification", () => {
    const events = [
      ev(1, "remediation.planned", { status: "planned", steps: [step], notes: [] }),
      ev(2, "approval.requested", { step_index: 0, approval_id: "a1" }),
      ev(3, "approval.awaiting", { step_index: 0, approval_id: "a1", kind: "approval" }),
    ];
    let live = liveRemediation(null, events)!;
    expect(live.steps[0]?.status).toBe("awaiting_approval");
    expect(live.waiting).toEqual({ kind: "approval", stepIndex: 0, approvalId: "a1" });

    events.push(
      ev(4, "approval.decided", { step_index: 0, status: "approved", decided_by: "bob" }),
      ev(5, "execution.started", { step_index: 0 }),
    );
    live = liveRemediation(null, events)!;
    expect(live.steps[0]?.status).toBe("executing");
    expect(live.waiting).toBeNull();

    events.push(
      ev(6, "execution.completed", { step_index: 0, status: "succeeded", job_id: "j1", summary: "ok" }),
      ev(7, "verification.completed", { step_index: 0, status: "passed", summary: "Verified: running" }),
    );
    live = liveRemediation(null, events)!;
    expect(live.steps[0]?.status).toBe("verified");
    expect(live.steps[0]?.job_id).toBe("j1");
    expect(live.steps[0]?.verification?.summary).toBe("Verified: running");
  });

  it("handles manual requests, skips, revisions and failures", () => {
    const two = [step, { ...step, index: 1, tool: "linux_reboot", risk_level: "critical" }];
    const events = [
      ev(1, "remediation.planned", { status: "planned", steps: two }),
      ev(2, "remediation.awaiting_request", { step_index: 0 }),
    ];
    expect(liveRemediation(null, events)!.waiting).toEqual({ kind: "remediation_step", stepIndex: 0 });
    events.push(
      ev(3, "remediation.step_skipped", { step_index: 0, by: "alice" }),
      ev(4, "approval.decided", { step_index: 1, status: "changes_requested", comment: "wait" }),
      ev(5, "remediation.revised", { step_index: 1, step: { ...two[1], revisions: 1, description: "Reboot later" } }),
    );
    let live = liveRemediation(null, events)!;
    expect(live.steps[0]).toMatchObject({ status: "skipped", status_reason: "Skipped by alice" });
    expect(live.steps[1]).toMatchObject({ status: "proposed", revisions: 1, description: "Reboot later" });
    events.push(ev(6, "execution.completed", { step_index: 1, status: "failed", summary: "sudo denied" }));
    live = liveRemediation(null, events)!;
    expect(live.steps[1]).toMatchObject({ status: "failed", status_reason: "sudo denied" });
  });

  it("prefers the persisted section and reports skipped planning", () => {
    const section = { status: "verified", reason: null, mode: "assisted", notes: [], steps: [] } as RemediationSection;
    expect(liveRemediation(section, [ev(1, "remediation.skipped", { reason: "x" })])!.status).toBe("verified");
    expect(liveRemediation(null, [ev(1, "remediation.skipped", { reason: "No root cause" })])).toMatchObject({
      status: "skipped",
      reason: "No root cause",
    });
    expect(liveRemediation(null, [])).toBeNull();
  });

  it("labels every status and counts down", () => {
    const statuses: ApprovalStatus[] = ["pending", "approved", "rejected", "changes_requested", "cancelled", "expired", "executed", "failed"];
    for (const s of statuses) expect(APPROVAL_STATUS_META[s].label).toBeTruthy();
    expect(STEP_STATUS_META.unverified.tone).toBe("danger");
    const now = Date.parse("2026-11-19T10:00:00Z");
    expect(timeLeft("2026-11-19T10:12:00Z", now)).toBe("in 12 min");
    expect(timeLeft("2026-11-19T11:05:00Z", now)).toBe("in 1 h 5 min");
    expect(timeLeft("2026-11-19T09:59:00Z", now)).toBe("expired");
  });

  it("describes remediation events in the timeline", () => {
    expect(describeEvent(ev(1, "approval.requested", { risk_level: "high", description: "Restart x" })).title).toBe(
      "Approval requested (high risk)",
    );
    expect(describeEvent(ev(2, "verification.completed", { status: "failed", summary: "still down" })).title).toBe(
      "Verification failed",
    );
    expect(describeEvent(ev(3, "remediation.planned", { status: "advisory", steps: [step] })).title).toContain("(advisory)");
    expect(describeEvent(ev(4, "approval.decided", { status: "changes_requested", decided_by: "bob" })).title).toBe(
      "Approval changes requested by bob",
    );
  });
});

describe("policy form", () => {
  it("only sends the fields that are set", () => {
    expect(formToRules(EMPTY_FORM)).toEqual({ match: {}, effect: {} });
    const rules = formToRules({
      ...EMPTY_FORM,
      tools: ["docker_container_restart"],
      environments: " dev, *nonprod* ,",
      tags: "db",
      riskLevel: "low",
      autoApprove: "yes",
      selfApproval: "no",
      deny: true,
      denyReason: " frozen ",
      ttl: "15",
    });
    expect(rules).toEqual({
      match: { tools: ["docker_container_restart"], environments: ["dev", "*nonprod*"], tags: ["db"] },
      effect: { risk_level: "low", auto_approve: true, allow_self_approval: false, deny: true, deny_reason: "frozen", approval_ttl_minutes: 15 },
    });
  });

  it("round-trips a policy and summarises it", () => {
    const policy: Policy = {
      id: "p",
      name: "Non-production container restarts are low risk",
      description: "",
      policy_type: "remediation",
      priority: 20,
      rules: { match: { tools: ["docker_container_restart"], environments: ["*nonprod*"] }, effect: { risk_level: "low", auto_approve: true } },
      enabled: true,
      builtin: true,
      created_at: "",
      updated_at: null,
    };
    const form = policyToForm(policy);
    expect(form.autoApprove).toBe("yes");
    expect(form.selfApproval).toBe("inherit");
    expect(formToRules(form)).toEqual(policy.rules);
    expect(summariseMatch(policy.rules)).toBe("docker_container_restart · env non-production");
    expect(summariseEffect(policy.rules)).toBe("risk → low · may run unattended (autonomous)");
    expect(summariseMatch({ match: {}, effect: {} })).toBe("every remediation action");
  });
});
