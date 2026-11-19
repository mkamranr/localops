import type { EvidenceItem, InvestigationDetail, InvestigationReport } from "@/types/api";

export type Report = Partial<InvestigationReport>;

/** Evidence items by id (E1.2 …). */
export function evidenceIndex(report: Report): Map<string, EvidenceItem> {
  const map = new Map<string, EvidenceItem>();
  for (const e of report.evidence ?? []) if (e.id) map.set(e.id, e);
  return map;
}

export function confidencePercent(value: number | null | undefined): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

const cite = (ids: string[] | undefined) => (ids?.length ? ` [${ids.join(", ")}]` : "");

/** The RCA report as Markdown (spec §6.6 sections), for download or a ticket. */
export function reportToMarkdown(inv: Pick<InvestigationDetail, "title" | "request" | "started_at" | "completed_at" | "result">): string {
  const r = inv.result;
  const out: string[] = [];
  const h = (n: number, title: string) => out.push("", `## ${n}. ${title}`, "");
  out.push(`# ${inv.title}`, "", `> ${inv.request.replace(/\n/g, " ")}`, "");
  out.push(`Started ${inv.started_at}${inv.completed_at ? ` · completed ${inv.completed_at}` : ""}`);
  out.push("", `**Conclusion:** ${r.conclusion ?? "—"}`);

  h(1, "Incident summary");
  out.push(r.incident_summary || r.summary || "—");
  if (r.impact) out.push("", `Impact: ${r.impact}`);

  h(2, "Affected resources");
  const resolved = (r.resources ?? []).filter((x) => x.resolved);
  out.push(...(resolved.length ? resolved.map((x) => `- ${x.name} (${x.resource_type ?? "?"}, health ${x.health ?? "?"})`) : ["- none resolved in the inventory"]));

  h(3, "Timeline");
  out.push(...((r.timeline ?? []).length ? (r.timeline ?? []).map((t) => `- ${t.at ?? ""} ${t.summary} [${t.evidence_id}]`) : ["- no diagnostic steps recorded"]));

  h(4, "Observed symptoms");
  out.push(...((r.symptoms ?? []).length ? (r.symptoms ?? []).map((s) => `- ${s}`) : ["- none stated"]));

  h(5, "Diagnostic evidence");
  for (const e of r.evidence ?? []) {
    out.push(`- **${e.id ?? "-"}** ${e.tool ?? e.source} on ${e.resource ?? "-"}: ${e.summary}${e.ok === false ? " (failed)" : ""}`);
  }
  if ((r.findings ?? []).length) {
    out.push("", "Findings:");
    for (const f of r.findings ?? []) out.push(`- _${f.kind.replace("_", " ")}_: ${f.statement}${cite(f.evidence_ids)}`);
  }

  h(6, "Recent changes");
  out.push(...((r.recent_changes ?? []).length ? (r.recent_changes ?? []).map((c) => `- ${c.summary} [${c.evidence_id}]`) : ["- none collected"]));

  h(7, "Dependency analysis");
  for (const d of r.dependency_analysis ?? []) {
    out.push(`- ${d.resource}: depends on ${d.depends_on.join(", ") || "nothing recorded"}; ${d.dependents} dependent(s) [${d.evidence_id}]`);
  }
  for (const i of r.change_impact?.impacts ?? []) {
    out.push(`- Change risk for ${i.resource}: **${i.risk_level}**; direct: ${i.direct.join(", ") || "none"}; indirect: ${i.indirect.join(", ") || "none"}`);
  }
  if (!(r.dependency_analysis ?? []).length && !r.change_impact) out.push("- not analysed");

  h(8, "Identified root cause");
  if (r.root_cause) out.push(`**${r.root_cause.status}**: ${r.root_cause.statement}${cite(r.root_cause.evidence_ids)}`);
  else out.push("No root cause could be established from the evidence.");
  for (const a of r.alternative_hypotheses ?? []) out.push(`- Alternative (${a.likelihood}, unverified unless cited): ${a.statement}${cite(a.evidence_ids)}`);

  h(9, "Contributing factors");
  out.push(...((r.contributing_factors ?? []).length ? (r.contributing_factors ?? []).map((c) => `- ${c.statement}${cite(c.evidence_ids)}`) : ["- none identified"]));

  h(10, "Confidence level");
  out.push(r.root_cause ? `${confidencePercent(r.confidence)} (${r.confidence_label})` : "none");

  h(11, "Recommended remediation (advisory)");
  out.push(...((r.recommended_remediation ?? []).length ? (r.recommended_remediation ?? []).map((m) => `- ${m.action} — ${m.rationale} (risk ${m.risk})${cite(m.evidence_ids)}`) : ["- none"]));

  h(12, "Preventive recommendations");
  out.push(...((r.preventive_recommendations ?? []).length ? (r.preventive_recommendations ?? []).map((p) => `- ${p}`) : ["- none"]));

  if ((r.evidence_gaps ?? []).length) {
    out.push("", "## Evidence gaps", "", ...(r.evidence_gaps ?? []).map((g) => `- ${g}`));
  }
  return out.join("\n") + "\n";
}

export function downloadText(filename: string, text: string, type = "text/markdown") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function slug(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "report";
}
