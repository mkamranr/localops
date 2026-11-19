import { Badge, type Tone } from "@/components/ui/display";
import type { IncidentStatus, Severity } from "@/types/api";

export const SEVERITY_META: Record<Severity, { label: string; tone: Tone; order: number }> = {
  critical: { label: "Critical", tone: "danger", order: 0 },
  high: { label: "High", tone: "danger", order: 1 },
  medium: { label: "Medium", tone: "warning", order: 2 },
  low: { label: "Low", tone: "primary", order: 3 },
  informational: { label: "Info", tone: "neutral", order: 4 },
};
export const SEVERITIES = Object.keys(SEVERITY_META) as Severity[];

export const INCIDENT_STATUS_META: Record<IncidentStatus, { label: string; tone: Tone }> = {
  new: { label: "New", tone: "primary" },
  investigating: { label: "Investigating", tone: "violet" },
  awaiting_approval: { label: "Awaiting approval", tone: "warning" },
  remediating: { label: "Remediating", tone: "warning" },
  monitoring: { label: "Monitoring", tone: "primary" },
  resolved: { label: "Resolved", tone: "success" },
  closed: { label: "Closed", tone: "neutral" },
  failed: { label: "Failed", tone: "danger" },
};
export const INCIDENT_STATUSES = Object.keys(INCIDENT_STATUS_META) as IncidentStatus[];
export const OPEN_STATUSES: IncidentStatus[] = [
  "new",
  "investigating",
  "awaiting_approval",
  "remediating",
  "monitoring",
];

export const SOURCE_LABEL: Record<string, string> = {
  manual: "Manual",
  investigation: "From investigation",
  discovery: "Discovery (automatic)",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  const meta = SEVERITY_META[severity] ?? SEVERITY_META.medium;
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}

export function IncidentStatusBadge({ status }: { status: IncidentStatus }) {
  const meta = INCIDENT_STATUS_META[status] ?? { label: status, tone: "neutral" as Tone };
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}

/** Map investigation urgency to a starting severity for a new incident. */
export function severityFromUrgency(urgency: string | null | undefined): Severity {
  return ({ critical: "critical", high: "high", medium: "medium", low: "low" } as Record<string, Severity>)[
    urgency ?? ""
  ] ?? "medium";
}

/** Labels for status-change buttons; transitions come from the server. */
export function transitionLabel(to: IncidentStatus): string {
  return (
    {
      investigating: "Start investigating",
      monitoring: "Monitor",
      resolved: "Resolve",
      closed: "Close",
      failed: "Mark failed",
      new: "Reset",
    } as Partial<Record<IncidentStatus, string>>
  )[to] ?? INCIDENT_STATUS_META[to].label;
}
