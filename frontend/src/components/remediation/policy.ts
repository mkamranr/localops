import type { Policy, PolicyInput, PolicyRules, RiskLevel } from "@/types/api";

export type TriState = "inherit" | "yes" | "no";

export interface PolicyForm {
  name: string;
  description: string;
  priority: number;
  enabled: boolean;
  tools: string[];
  risks: RiskLevel[];
  environments: string;
  resourceTypes: string;
  tags: string;
  groups: string;
  riskLevel: RiskLevel | "";
  autoApprove: TriState;
  selfApproval: TriState;
  deny: boolean;
  denyReason: string;
  ttl: string;
}

export const EMPTY_FORM: PolicyForm = {
  name: "",
  description: "",
  priority: 100,
  enabled: true,
  tools: [],
  risks: [],
  environments: "",
  resourceTypes: "",
  tags: "",
  groups: "",
  riskLevel: "",
  autoApprove: "inherit",
  selfApproval: "inherit",
  deny: false,
  denyReason: "",
  ttl: "",
};

const list = (v: string) =>
  v
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
const tri = (v: boolean | null | undefined): TriState => (v == null ? "inherit" : v ? "yes" : "no");
const fromTri = (v: TriState) => (v === "inherit" ? undefined : v === "yes");

export function policyToForm(p: Policy): PolicyForm {
  const m = p.rules.match ?? {};
  const e = p.rules.effect ?? {};
  return {
    name: p.name,
    description: p.description,
    priority: p.priority,
    enabled: p.enabled,
    tools: m.tools ?? [],
    risks: (m.risks ?? []) as RiskLevel[],
    environments: (m.environments ?? []).join(", "),
    resourceTypes: (m.resource_types ?? []).join(", "),
    tags: (m.tags ?? []).join(", "),
    groups: (m.groups ?? []).join(", "),
    riskLevel: e.risk_level ?? "",
    autoApprove: tri(e.auto_approve),
    selfApproval: tri(e.allow_self_approval),
    deny: !!e.deny,
    denyReason: e.deny_reason ?? "",
    ttl: e.approval_ttl_minutes ? String(e.approval_ttl_minutes) : "",
  };
}

/** Only the keys that are set, so empty fields mean "match anything" / "no change". */
export function formToRules(f: PolicyForm): PolicyRules {
  const match: PolicyRules["match"] = {};
  if (f.tools.length) match.tools = f.tools;
  if (f.risks.length) match.risks = f.risks;
  if (list(f.environments).length) match.environments = list(f.environments);
  if (list(f.resourceTypes).length) match.resource_types = list(f.resourceTypes);
  if (list(f.tags).length) match.tags = list(f.tags);
  if (list(f.groups).length) match.groups = list(f.groups);
  const effect: PolicyRules["effect"] = {};
  if (f.riskLevel) effect.risk_level = f.riskLevel;
  const auto = fromTri(f.autoApprove);
  if (auto !== undefined) effect.auto_approve = auto;
  const self = fromTri(f.selfApproval);
  if (self !== undefined) effect.allow_self_approval = self;
  if (f.deny) {
    effect.deny = true;
    if (f.denyReason.trim()) effect.deny_reason = f.denyReason.trim();
  }
  const ttl = Number(f.ttl);
  if (f.ttl && Number.isFinite(ttl) && ttl > 0) effect.approval_ttl_minutes = Math.round(ttl);
  return { match, effect };
}

export function formToInput(f: PolicyForm): PolicyInput {
  return {
    name: f.name.trim(),
    description: f.description.trim(),
    priority: f.priority,
    enabled: f.enabled,
    rules: formToRules(f),
  };
}

export function summariseMatch(rules: PolicyRules): string {
  const m = rules.match ?? {};
  const parts: string[] = [];
  if (m.tools?.length) parts.push(m.tools.join(", "));
  if (m.categories?.length) parts.push(`category ${m.categories.join("/")}`);
  if (m.risks?.length) parts.push(`${m.risks.join("/")} risk`);
  if (m.environments?.length) {
    parts.push(`env ${m.environments.map((e) => (e === "*nonprod*" ? "non-production" : e)).join("/")}`);
  }
  if (m.resource_types?.length) parts.push(m.resource_types.join("/"));
  if (m.tags?.length) parts.push(`tag ${m.tags.join("/")}`);
  if (m.groups?.length) parts.push(`group ${m.groups.join("/")}`);
  return parts.length ? parts.join(" · ") : "every remediation action";
}

export function summariseEffect(rules: PolicyRules): string {
  const e = rules.effect ?? {};
  const parts: string[] = [];
  if (e.deny) parts.push(`deny${e.deny_reason ? ` (${e.deny_reason})` : ""}`);
  if (e.risk_level) parts.push(`risk → ${e.risk_level}`);
  if (e.auto_approve === true) parts.push("may run unattended (autonomous)");
  if (e.auto_approve === false) parts.push("never unattended");
  if (e.allow_self_approval === true) parts.push("self-approval allowed");
  if (e.allow_self_approval === false) parts.push("four-eyes");
  if (e.approval_ttl_minutes) parts.push(`approval valid ${e.approval_ttl_minutes} min`);
  return parts.length ? parts.join(" · ") : "no effect";
}
