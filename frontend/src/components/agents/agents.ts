import type { Agent, AgentConfig, ToolInfo, ToolSource } from "@/types/api";

/** Editable agent fields as form strings (inputs hold text). */
export interface AgentForm {
  name: string;
  description: string;
  model_id: string;
  temperature: string;
  max_tokens: string;
  timeout_seconds: string;
  max_retries: string;
  enabled: boolean;
  max_steps: string;
  allowed_tools: string[];
  instructions: string;
}

export const MAX_INSTRUCTIONS = 2000;

export function formFromAgent(agent: Agent): AgentForm {
  const c = agent.configuration;
  return {
    name: agent.name,
    description: agent.description,
    model_id: agent.model_id ?? "",
    temperature: String(c.temperature),
    max_tokens: String(c.max_tokens),
    timeout_seconds: String(c.timeout_seconds),
    max_retries: String(c.max_retries),
    enabled: agent.enabled,
    max_steps: String(c.max_steps ?? 6),
    allowed_tools: [...(c.allowed_tools ?? [])],
    instructions: c.instructions ?? "",
  };
}

export interface AgentUpdatePayload {
  name?: string;
  description?: string;
  model_id?: string | null;
  enabled?: boolean;
  configuration?: Partial<AgentConfig>;
}

const sameList = (a: string[], b: string[]) => a.length === b.length && a.every((x, i) => x === b[i]);

/** Only the fields that changed; tool settings only for agents that use tools. */
export function updatePayload(agent: Agent, form: AgentForm): AgentUpdatePayload {
  const out: AgentUpdatePayload = {};
  const c = agent.configuration;
  if (form.name.trim() && form.name.trim() !== agent.name) out.name = form.name.trim();
  if (form.description !== agent.description) out.description = form.description;
  if ((form.model_id || null) !== agent.model_id) out.model_id = form.model_id || null;
  if (form.enabled !== agent.enabled) out.enabled = form.enabled;
  const config: Partial<AgentConfig> = {};
  const num = (key: "temperature" | "max_tokens" | "timeout_seconds" | "max_retries") => {
    const v = Number(form[key]);
    if (form[key] !== "" && Number.isFinite(v) && v !== c[key]) config[key] = v;
  };
  num("temperature");
  num("max_tokens");
  num("timeout_seconds");
  num("max_retries");
  if (agent.uses_tools) {
    const steps = Number(form.max_steps);
    if (form.max_steps !== "" && Number.isFinite(steps) && steps !== c.max_steps) config.max_steps = steps;
    if (!sameList(form.allowed_tools, c.allowed_tools ?? [])) config.allowed_tools = [...form.allowed_tools];
    if (form.instructions.trim() !== (c.instructions ?? "").trim()) config.instructions = form.instructions.trim();
  }
  if (Object.keys(config).length) out.configuration = config;
  return out;
}

export const SOURCE_LABELS: Record<ToolSource, string> = {
  builtin: "Built-in",
  mcp: "MCP servers",
  http: "Custom HTTP",
};

/** Read tools grouped by source (built-in first); change tools never appear. */
export function groupReadTools(tools: ToolInfo[]): { source: ToolSource; label: string; tools: ToolInfo[] }[] {
  const order: ToolSource[] = ["builtin", "mcp", "http"];
  return order
    .map((source) => ({
      source,
      label: SOURCE_LABELS[source],
      tools: tools
        .filter((t) => t.kind === "read" && (t.source ?? "builtin") === source)
        .sort((a, b) => a.name.localeCompare(b.name)),
    }))
    .filter((g) => g.tools.length > 0);
}

/** Mirrors the backend: lowercase, underscores, no "_agent" suffix, ≤40 chars. */
export function slugPreview(name: string, slug?: string): string {
  if (slug && slug.trim()) return `custom_${slug.trim()}`;
  let s = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  s = s.replace(/_agent$/, "").replace(/^custom_/, "");
  if (!s || !/^[a-z]/.test(s)) s = `agent_${s}`.replace(/_+$/, "");
  return `custom_${s.slice(0, 40).replace(/_+$/, "")}`;
}

export function formatSuccessRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "—";
  return `${Math.round(rate * 1000) / 10}%`;
}

export function toggle(list: string[], name: string): string[] {
  return list.includes(name) ? list.filter((n) => n !== name) : [...list, name];
}
