import { describe, expect, it } from "vitest";
import {
  formatSuccessRate,
  formFromAgent,
  groupReadTools,
  slugPreview,
  toggle,
  updatePayload,
} from "@/components/agents/agents";
import type { Agent, ToolInfo } from "@/types/api";

const agent = (over: Partial<Agent> = {}): Agent => ({
  id: "a1",
  agent_type: "network_diagnostics",
  name: "Network Diagnostics Agent",
  description: "Checks DNS",
  enabled: true,
  model_id: null,
  model_name: null,
  configuration: {
    temperature: 0.1,
    max_tokens: 1024,
    timeout_seconds: 120,
    max_retries: 2,
    allowed_tools: [],
    max_steps: 6,
    instructions: "",
  },
  capabilities: [],
  available_in_phase: 4,
  implemented: true,
  specialist: true,
  custom: false,
  uses_tools: true,
  stats: {
    executions_24h: 0,
    failures_24h: 0,
    avg_duration_ms: null,
    tokens_24h: 0,
    last_run_at: null,
    executions_total: 0,
    success_rate: null,
  },
  updated_at: null,
  ...over,
});

const tool = (name: string, kind: "read" | "change", source: ToolInfo["source"]): ToolInfo =>
  ({ name, kind, source }) as ToolInfo;

describe("agent config payload", () => {
  it("sends nothing when unchanged", () => {
    const a = agent();
    expect(updatePayload(a, formFromAgent(a))).toEqual({});
  });

  it("sends only changed fields", () => {
    const a = agent();
    const form = { ...formFromAgent(a), max_steps: "10", allowed_tools: ["dns_lookup"], instructions: " Be brief " };
    expect(updatePayload(a, form)).toEqual({
      configuration: { max_steps: 10, allowed_tools: ["dns_lookup"], instructions: "Be brief" },
    });
    expect(updatePayload(a, { ...formFromAgent(a), model_id: "m1", enabled: false })).toEqual({
      model_id: "m1",
      enabled: false,
    });
  });

  it("omits tool settings for agents that do not use tools", () => {
    const a = agent({ uses_tools: false });
    expect(updatePayload(a, { ...formFromAgent(a), max_steps: "3", instructions: "x" })).toEqual({});
  });
});

describe("tool grouping", () => {
  it("groups read tools by source and drops change tools", () => {
    const groups = groupReadTools([
      tool("mcp_b", "read", "mcp"),
      tool("dns_lookup", "read", "builtin"),
      tool("linux_reboot", "change", "builtin"),
      tool("http_x", "read", "http"),
      tool("mcp_a", "read", "mcp"),
    ]);
    expect(groups.map((g) => g.source)).toEqual(["builtin", "mcp", "http"]);
    expect(groups[1]?.tools.map((t) => t.name)).toEqual(["mcp_a", "mcp_b"]);
    expect(groups.flatMap((g) => g.tools).some((t) => t.kind === "change")).toBe(false);
  });
});

describe("helpers", () => {
  it("previews custom agent slugs like the backend", () => {
    expect(slugPreview("Netbox Agent")).toBe("custom_netbox");
    expect(slugPreview("DB  Health!")).toBe("custom_db_health");
    expect(slugPreview("42 checks")).toBe("custom_agent_42_checks");
    expect(slugPreview("x", "my_slug")).toBe("custom_my_slug");
  });

  it("formats success rates", () => {
    expect(formatSuccessRate(null)).toBe("—");
    expect(formatSuccessRate(1)).toBe("100%");
    expect(formatSuccessRate(0.8333)).toBe("83.3%");
  });

  it("toggles list entries", () => {
    expect(toggle(["a"], "b")).toEqual(["a", "b"]);
    expect(toggle(["a", "b"], "a")).toEqual(["b"]);
  });
});
