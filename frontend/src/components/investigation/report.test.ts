import { describe, expect, it } from "vitest";
import { confidencePercent, evidenceIndex, reportToMarkdown, slug } from "@/components/investigation/report";
import type { InvestigationDetail } from "@/types/api";

const inv = {
  title: "Why is CPU high on web-01?",
  request: "Why is CPU high on web-01?\nIt started at noon.",
  started_at: "2026-11-19T10:00:00Z",
  completed_at: "2026-11-19T10:02:00Z",
  result: {
    conclusion: "Probable cause: a runaway yes process.",
    incident_summary: "CPU at 100% on web-01.",
    resources: [{ name: "web-01", resolved: true, resource_type: "linux_server", health: "degraded" }],
    evidence: [
      { id: "E1.1", source: "tool", tool: "linux_top_processes", resource: "web-01", summary: "yes at 99% CPU", ok: true },
      { id: "E1.2", source: "tool", tool: "linux_journal", resource: "web-01", summary: "journal read failed", ok: false },
    ],
    findings: [{ kind: "confirmed_fact", statement: "yes uses 99% CPU", evidence_ids: ["E1.1"] }],
    root_cause: { statement: "A runaway yes process", status: "probable", confidence: 0.62, evidence_ids: ["E1.1"] },
    confidence: 0.62,
    confidence_label: "medium",
    alternative_hypotheses: [{ statement: "A cron job", likelihood: "low", evidence_ids: [] }],
    recommended_remediation: [{ action: "Stop the yes process", rationale: "It is consuming the CPU", risk: "low", evidence_ids: ["E1.1"] }],
    evidence_gaps: ["Journal could not be read"],
  },
} as unknown as InvestigationDetail;

describe("RCA report", () => {
  it("renders the spec sections with citations", () => {
    const md = reportToMarkdown(inv);
    expect(md).toContain("# Why is CPU high on web-01?");
    expect(md).toContain("> Why is CPU high on web-01? It started at noon.");
    for (const n of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) expect(md).toContain(`## ${n}. `);
    expect(md).toContain("**probable**: A runaway yes process [E1.1]");
    expect(md).toContain("_confirmed fact_: yes uses 99% CPU [E1.1]");
    expect(md).toContain("journal read failed (failed)");
    expect(md).toContain("Alternative (low, unverified unless cited): A cron job");
    expect(md).toContain("62% (medium)");
    expect(md).toContain("## Evidence gaps");
  });

  it("says plainly when no root cause was established", () => {
    const md = reportToMarkdown({ ...inv, result: { ...inv.result, root_cause: null } });
    expect(md).toContain("No root cause could be established from the evidence.");
    expect(md).toMatch(/## 10\. Confidence level\n\nnone/);
  });

  it("indexes evidence and formats helpers", () => {
    expect(evidenceIndex(inv.result).get("E1.2")?.tool).toBe("linux_journal");
    expect(confidencePercent(0.856)).toBe("86%");
    expect(confidencePercent(null)).toBe("—");
    expect(slug("Why is CPU high on web-01?")).toBe("why-is-cpu-high-on-web-01");
    expect(slug("???")).toBe("report");
  });
});
