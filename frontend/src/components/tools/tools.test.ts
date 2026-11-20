import { describe, expect, it } from "vitest";
import {
  coerceArguments,
  combineRows,
  definitionToForm,
  emptyHttpForm,
  formProblems,
  formToInput,
  paramsToSchemaPreview,
  parseBody,
  recordToRows,
  rowsToRecord,
  schemaToParams,
  STATUS_META,
  templatePlaceholders,
} from "@/components/tools/tools";
import type { ToolDefinition, ToolInfo } from "@/types/api";

const definition: ToolDefinition = {
  id: "d1",
  integration_id: "i1",
  integration_name: "Inventory API",
  source: "http",
  name: "http_device_info",
  remote_name: "GET /devices/{device}",
  title: null,
  description: "Device info",
  remote_description: null,
  input_schema: {
    type: "object",
    properties: { device: { type: "string", maxLength: 40, pattern: "^[a-z0-9-]+$" }, limit: { type: "integer", minimum: 1 } },
    required: ["device"],
  },
  output_schema: null,
  http_template: { method: "POST", path: "/devices/{device}", query: { limit: "{limit}" }, headers: {}, body: { a: 1 }, response_max_bytes: 200000 },
  kind: "read",
  risk_level: "low",
  required_permission: "tool.execute.readonly",
  timeout_seconds: 30,
  verify_with: null,
  enabled: false,
  status: "new",
  schema_hash: "h",
  approved_schema_hash: null,
  approved_by: null,
  last_synced_at: null,
  created_by: "admin",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: null,
  usable: false,
};

describe("tool helpers", () => {
  it("maps statuses to review labels", () => {
    expect(STATUS_META.new.label).toBe("Needs review");
    expect(STATUS_META.schema_changed.tone).toBe("warning");
  });

  it("round-trips key/value rows and drops blank keys", () => {
    expect(rowsToRecord([{ key: " a ", value: "1" }, { key: "", value: "x" }])).toEqual({ a: "1" });
    expect(recordToRows({ a: "1" })).toEqual([{ key: "a", value: "1" }]);
  });

  it("parses body JSON and reports errors", () => {
    expect(parseBody("")).toEqual({ ok: true, value: null });
    expect(parseBody('{"a": 1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(parseBody("{nope").ok).toBe(false);
  });

  it("finds placeholders across the template", () => {
    expect(
      templatePlaceholders({ path: "/d/{device}", query: { l: "{limit}" }, headers: { X: "{token_id}" }, body: { n: ["{name}"] } }),
    ).toEqual(["device", "limit", "name", "token_id"]);
  });

  it("builds the schema preview from parameters", () => {
    const schema = paramsToSchemaPreview([
      { name: "device", type: "string", description: "Name", required: true, pattern: "^[a-z]+$" },
      { name: "n", type: "integer", description: "", required: false, minimum: 1, maximum: 5 },
      { name: "", type: "string", description: "", required: true },
    ]);
    expect(schema.required).toEqual(["device"]);
    expect(schema.properties?.device).toEqual({ type: "string", description: "Name", maxLength: 500, pattern: "^[a-z]+$" });
    expect(schema.properties?.n).toEqual({ type: "integer", minimum: 1, maximum: 5 });
    expect(schema.additionalProperties).toBe(false);
  });

  it("restores the editor form from a definition", () => {
    const form = definitionToForm(definition);
    expect(form.name).toBe("device_info");
    expect(form.method).toBe("POST");
    expect(form.query).toEqual([{ key: "limit", value: "{limit}" }]);
    expect(JSON.parse(form.body)).toEqual({ a: 1 });
    expect(form.params.map((p) => [p.name, p.required])).toEqual([["device", true], ["limit", false]]);
    expect(schemaToParams(definition.input_schema)[0]?.pattern).toBe("^[a-z0-9-]+$");
  });

  it("validates the form before sending", () => {
    const form = { ...emptyHttpForm("i1"), name: "device_info", description: "d", path: "/devices/{device}" };
    expect(formProblems(form)).toEqual(["Placeholders without a parameter: device"]);
    form.params = [{ name: "device", type: "string", description: "", required: true }];
    expect(formProblems(form)).toEqual([]);
    expect(formProblems({ ...form, kind: "change" })).toContain("A GET request cannot be a change tool");
    expect(formProblems({ ...form, body: '{"x": 1}' })).toContain("GET requests cannot have a body");
    expect(formProblems({ ...form, path: "devices" })).toContain("Path must start with '/'");
  });

  it("produces the API payload", () => {
    const form = {
      ...emptyHttpForm("i1"),
      name: " device_info ",
      description: "d",
      method: "POST" as const,
      path: "/x/{device}",
      body: '{"device": "{device}"}',
      headers: [{ key: "X-Trace", value: "1" }],
      params: [{ name: "device", type: "string" as const, description: "", required: true, pattern: "" }],
    };
    const input = formToInput(form);
    expect(input.name).toBe("device_info");
    expect(input.template).toEqual({ method: "POST", path: "/x/{device}", query: {}, headers: { "X-Trace": "1" }, body: { device: "{device}" } });
    expect(input.parameters[0]?.pattern).toBeNull();
    expect(input.risk_level).toBe("low");
  });

  it("coerces test arguments by schema type", () => {
    expect(coerceArguments(definition.input_schema, { device: "sw-01", limit: "3", extra: "x" })).toEqual({ device: "sw-01", limit: 3 });
  });

  it("combines built-in tools with registered definitions", () => {
    const builtin = { name: "linux_cpu", description: "", category: "linux", kind: "read", source: "builtin", permission: "p", verify_with: [], risk_level: "low", requires_approval: false, remote: true, timeout_seconds: 10, resource_types: [], input_schema: {} } as ToolInfo;
    const registered = { ...builtin, name: "http_device_info", source: "http" } as ToolInfo;
    const rows = combineRows([builtin, registered], [definition]);
    expect(rows.map((r) => [r.name, r.source, r.usable])).toEqual([
      ["linux_cpu", "builtin", true],
      ["http_device_info", "http", false],
    ]);
  });
});
