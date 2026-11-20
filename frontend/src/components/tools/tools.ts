import type { Tone } from "@/components/ui/display";
import type {
  HttpTemplate,
  HttpToolInput,
  JsonSchemaObject,
  RiskLevel,
  ToolDefinition,
  ToolDefinitionStatus,
  ToolInfo,
  ToolParam,
  ToolSource,
} from "@/types/api";

export const STATUS_META: Record<ToolDefinitionStatus, { label: string; tone: Tone }> = {
  new: { label: "Needs review", tone: "primary" },
  active: { label: "Active", tone: "success" },
  schema_changed: { label: "Changed — re-approve", tone: "warning" },
  missing: { label: "Missing on server", tone: "danger" },
};

export const SOURCE_META: Record<ToolSource, { label: string; tone: Tone }> = {
  builtin: { label: "built-in", tone: "neutral" },
  mcp: { label: "MCP", tone: "violet" },
  http: { label: "HTTP", tone: "primary" },
};

export const RISK_TONE: Record<string, Tone> = {
  low: "success",
  medium: "warning",
  high: "danger",
  critical: "danger",
};

export const METHODS: HttpTemplate["method"][] = ["GET", "POST", "PUT", "PATCH", "DELETE"];
export const RISKS: RiskLevel[] = ["low", "medium", "high", "critical"];

export interface KvRow {
  key: string;
  value: string;
}

export function recordToRows(record: Record<string, string> | null | undefined): KvRow[] {
  return Object.entries(record ?? {}).map(([key, value]) => ({ key, value }));
}

export function rowsToRecord(rows: KvRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const key = row.key.trim();
    if (key) out[key] = row.value;
  }
  return out;
}

export function methodAllowsBody(method: string): boolean {
  return method !== "GET" && method !== "DELETE";
}

/** Parse the JSON body template; blank means no body. */
export function parseBody(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  if (!text.trim()) return { ok: true, value: null };
  try {
    return { ok: true, value: JSON.parse(text) as unknown };
  } catch (err) {
    return { ok: false, error: `Body is not valid JSON: ${err instanceof Error ? err.message : String(err)}` };
  }
}

/** Placeholder names ({name}) used anywhere in a template. */
export function templatePlaceholders(template: Pick<HttpTemplate, "path"> & Partial<HttpTemplate>): string[] {
  const found = new Set<string>();
  const walk = (v: unknown) => {
    if (typeof v === "string") {
      for (const m of v.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)) if (m[1]) found.add(m[1]);
    } else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === "object") Object.values(v).forEach(walk);
  };
  walk(template.path);
  walk(template.query);
  walk(template.headers);
  walk(template.body);
  return [...found].sort();
}

export function emptyParam(): ToolParam {
  return { name: "", type: "string", description: "", required: true };
}

/** The JSON schema the backend will build from the parameter list (preview only). */
export function paramsToSchemaPreview(params: ToolParam[]): JsonSchemaObject {
  const properties: NonNullable<JsonSchemaObject["properties"]> = {};
  const required: string[] = [];
  for (const p of params) {
    if (!p.name.trim()) continue;
    const prop: NonNullable<JsonSchemaObject["properties"]>[string] = { type: p.type };
    if (p.description) prop.description = p.description;
    if (p.enum?.length) prop.enum = p.enum;
    if (p.type === "string") {
      prop.maxLength = p.max_length ?? 500;
      if (p.pattern) prop.pattern = p.pattern;
    }
    if (p.type === "integer" || p.type === "number") {
      if (p.minimum != null) prop.minimum = p.minimum;
      if (p.maximum != null) prop.maximum = p.maximum;
    }
    properties[p.name.trim()] = prop;
    if (p.required) required.push(p.name.trim());
  }
  const schema: JsonSchemaObject = { type: "object", properties, additionalProperties: false };
  if (required.length) schema.required = required;
  return schema;
}

export function schemaToParams(schema: JsonSchemaObject | null | undefined): ToolParam[] {
  const required = new Set(schema?.required ?? []);
  return Object.entries(schema?.properties ?? {}).map(([name, prop]) => ({
    name,
    type: (["string", "integer", "number", "boolean"].includes(prop.type ?? "") ? prop.type : "string") as ToolParam["type"],
    description: prop.description ?? "",
    required: required.has(name),
    enum: prop.enum ?? null,
    pattern: prop.pattern ?? null,
    max_length: prop.maxLength ?? null,
    minimum: prop.minimum ?? null,
    maximum: prop.maximum ?? null,
  }));
}

export interface HttpToolForm {
  integration_id: string;
  name: string;
  description: string;
  kind: "read" | "change";
  risk_level: RiskLevel;
  timeout_seconds: string;
  verify_with: string;
  enabled: boolean;
  method: HttpTemplate["method"];
  path: string;
  query: KvRow[];
  headers: KvRow[];
  body: string;
  params: ToolParam[];
}

export function emptyHttpForm(integrationId = ""): HttpToolForm {
  return {
    integration_id: integrationId,
    name: "",
    description: "",
    kind: "read",
    risk_level: "low",
    timeout_seconds: "30",
    verify_with: "",
    enabled: true,
    method: "GET",
    path: "/",
    query: [],
    headers: [],
    body: "",
    params: [],
  };
}

export function definitionToForm(def: ToolDefinition): HttpToolForm {
  const t = def.http_template;
  return {
    integration_id: def.integration_id,
    name: def.name.replace(/^http_/, ""),
    description: def.description,
    kind: def.kind,
    risk_level: def.risk_level,
    timeout_seconds: String(def.timeout_seconds),
    verify_with: def.verify_with ?? "",
    enabled: def.enabled,
    method: t?.method ?? "GET",
    path: t?.path ?? "/",
    query: recordToRows(t?.query),
    headers: recordToRows(t?.headers),
    body: t?.body == null ? "" : JSON.stringify(t.body, null, 2),
    params: schemaToParams(def.input_schema),
  };
}

/** Validate the editor form locally; returns the problems found. */
export function formProblems(form: HttpToolForm): string[] {
  const problems: string[] = [];
  if (!form.integration_id) problems.push("Choose an HTTP API integration");
  if (!/^[a-z][a-z0-9_]{0,49}$/i.test(form.name.trim())) problems.push("Name: letters, digits and underscores");
  if (!form.description.trim()) problems.push("Description is required");
  if (!form.path.startsWith("/")) problems.push("Path must start with '/'");
  if (form.kind === "change" && form.method === "GET") problems.push("A GET request cannot be a change tool");
  const body = parseBody(form.body);
  if (!body.ok) problems.push(body.error);
  else if (body.value !== null && !methodAllowsBody(form.method)) problems.push(`${form.method} requests cannot have a body`);
  const names = form.params.map((p) => p.name.trim()).filter(Boolean);
  if (new Set(names).size !== names.length) problems.push("Parameter names must be unique");
  const known = new Set(names);
  const unknown = templatePlaceholders({
    path: form.path,
    query: rowsToRecord(form.query),
    headers: rowsToRecord(form.headers),
    body: body.ok ? body.value : null,
  }).filter((n) => !known.has(n));
  if (unknown.length) problems.push(`Placeholders without a parameter: ${unknown.join(", ")}`);
  return problems;
}

export function formToTemplate(form: HttpToolForm): HttpToolInput["template"] {
  const body = parseBody(form.body);
  return {
    method: form.method,
    path: form.path.trim(),
    query: rowsToRecord(form.query),
    headers: rowsToRecord(form.headers),
    body: body.ok && methodAllowsBody(form.method) ? body.value : null,
  };
}

export function formToParams(form: HttpToolForm): ToolParam[] {
  return form.params
    .filter((p) => p.name.trim())
    .map((p) => ({
      ...p,
      name: p.name.trim(),
      pattern: p.pattern || null,
      enum: p.enum?.length ? p.enum : null,
    }));
}

export function formToInput(form: HttpToolForm): HttpToolInput {
  return {
    integration_id: form.integration_id,
    name: form.name.trim(),
    description: form.description.trim(),
    parameters: formToParams(form),
    template: formToTemplate(form),
    kind: form.kind,
    risk_level: form.kind === "change" ? form.risk_level : "low",
    timeout_seconds: Number(form.timeout_seconds) || 30,
    verify_with: form.verify_with || null,
    enabled: form.enabled,
  };
}

/** Turn text inputs from a schema-driven form into typed arguments. */
export function coerceArguments(schema: JsonSchemaObject, values: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, prop] of Object.entries(schema.properties ?? {})) {
    const raw = values[name];
    if (raw === undefined || raw === "") continue;
    if (prop.type === "integer" || prop.type === "number") out[name] = Number(raw);
    else if (prop.type === "boolean") out[name] = raw === "true";
    else if (prop.type === "object" || prop.type === "array") {
      try {
        out[name] = JSON.parse(raw) as unknown;
      } catch {
        out[name] = raw;
      }
    } else out[name] = raw;
  }
  return out;
}

export interface CatalogueRow {
  name: string;
  description: string;
  source: ToolSource;
  kind: "read" | "change";
  risk: string;
  permission: string;
  category: string;
  definition: ToolDefinition | null;
  usable: boolean;
}

/** One list: built-in tools, plus every registered tool (enabled or not). */
export function combineRows(catalogue: ToolInfo[], definitions: ToolDefinition[]): CatalogueRow[] {
  const defs = new Map(definitions.map((d) => [d.name, d]));
  const rows: CatalogueRow[] = catalogue
    .filter((t) => (t.source ?? "builtin") === "builtin")
    .map((t) => ({
      name: t.name,
      description: t.description,
      source: "builtin" as const,
      kind: t.kind,
      risk: t.risk_level,
      permission: t.permission,
      category: t.category,
      definition: null,
      usable: true,
    }));
  for (const d of defs.values()) {
    rows.push({
      name: d.name,
      description: d.description,
      source: d.source,
      kind: d.kind,
      risk: d.risk_level,
      permission: d.required_permission,
      category: d.integration_name ?? d.source,
      definition: d,
      usable: d.usable,
    });
  }
  return rows.sort((a, b) => a.source.localeCompare(b.source) || a.name.localeCompare(b.name));
}
