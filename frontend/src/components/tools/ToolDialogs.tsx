import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { toolDefinitionsApi } from "@/api/endpoints";
import {
  coerceArguments,
  definitionToForm,
  emptyHttpForm,
  emptyParam,
  formProblems,
  formToInput,
  formToParams,
  formToTemplate,
  METHODS,
  methodAllowsBody,
  paramsToSchemaPreview,
  RISKS,
  SOURCE_META,
  STATUS_META,
  type HttpToolForm,
  type KvRow,
} from "@/components/tools/tools";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import type {
  Integration,
  RiskLevel,
  ToolDefinition,
  ToolDefinitionStatus,
  ToolParam,
  ToolRunResult,
  ToolSource,
} from "@/types/api";

export function invalidateTools(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: ["tool-definitions"] });
  void queryClient.invalidateQueries({ queryKey: ["tools"] });
}

export function ToolStatusBadge({ status }: { status: ToolDefinitionStatus }) {
  const meta = STATUS_META[status];
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}

export function SourceBadge({ source }: { source: ToolSource }) {
  const meta = SOURCE_META[source] ?? SOURCE_META.builtin;
  return <Badge tone={meta.tone}>{meta.label}</Badge>;
}

const check = "flex items-center gap-2 text-sm";

// ------------------------------------------------------------------ review (MCP and HTTP)

export function ToolReviewDialog({
  tool,
  readTools,
  canManage,
  onClose,
}: {
  tool: ToolDefinition;
  readTools: string[];
  canManage: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    description: tool.description,
    kind: tool.kind,
    risk_level: tool.risk_level,
    timeout: String(tool.timeout_seconds),
    verify_with: tool.verify_with ?? "",
    enabled: tool.enabled,
  });
  const changed = tool.status === "schema_changed";
  const save = useMutation({
    mutationFn: (approve: boolean) =>
      toolDefinitionsApi.update(tool.id, {
        description: form.description.trim() || undefined,
        kind: form.kind,
        risk_level: form.risk_level,
        timeout_seconds: Number(form.timeout) || undefined,
        ...(form.verify_with ? { verify_with: form.verify_with } : { clear_verify_with: !!tool.verify_with }),
        enabled: form.enabled,
        approve_schema: approve || undefined,
      }),
    onSuccess: () => {
      invalidateTools(queryClient);
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate(false);
  };
  return (
    <Dialog
      open
      wide
      onOpenChange={(o) => !o && onClose()}
      title={`Review ${tool.name}`}
      description="Agents only see the reviewed description. Remote text and schemas are untrusted until approved."
      footer={
        canManage ? (
          <>
            <Button variant="ghost" onClick={onClose}>Cancel</Button>
            {changed && (
              <Button variant="secondary" loading={save.isPending} onClick={() => save.mutate(true)}>
                Approve changes
              </Button>
            )}
            <Button type="submit" form="tool-review" loading={save.isPending}>Save</Button>
          </>
        ) : (
          <Button variant="ghost" onClick={onClose}>Close</Button>
        )
      }
    >
      <form id="tool-review" onSubmit={submit} className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <ToolStatusBadge status={tool.status} />
          <SourceBadge source={tool.source} />
          {tool.remote_name && <span className="font-mono text-muted">remote: {tool.remote_name}</span>}
          {tool.approved_by && <span className="text-subtle">approved by {tool.approved_by}</span>}
        </div>
        {changed && (
          <Alert tone="warning">
            The server changed this tool&apos;s schema or description since it was approved. It stays disabled until you
            review the new definition below and approve it.
          </Alert>
        )}
        {tool.remote_description != null && tool.source === "mcp" && (
          <Field label="Description from the server (untrusted)" htmlFor="t-remote">
            <pre id="t-remote" className="max-h-32 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface-2 p-3 text-xs">
              {tool.remote_description || "(none)"}
            </pre>
          </Field>
        )}
        <Field label="Description shown to agents" htmlFor="t-desc">
          <Textarea id="t-desc" rows={3} maxLength={1000} value={form.description} disabled={!canManage} onChange={(e) => setForm({ ...form, description: e.target.value })} />
        </Field>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Kind" htmlFor="t-kind" hint={form.kind === "change" ? "Runs only through approval." : "Agents may call it."}>
            <Select id="t-kind" value={form.kind} disabled={!canManage} onChange={(e) => setForm({ ...form, kind: e.target.value as "read" | "change" })}>
              <option value="read">Read</option>
              <option value="change">Change</option>
            </Select>
          </Field>
          <Field label="Risk" htmlFor="t-risk">
            <Select id="t-risk" value={form.risk_level} disabled={!canManage} onChange={(e) => setForm({ ...form, risk_level: e.target.value as RiskLevel })}>
              {RISKS.map((r) => <option key={r} value={r}>{r}</option>)}
            </Select>
          </Field>
          <Field label="Timeout (s)" htmlFor="t-timeout">
            <Input id="t-timeout" type="number" min={1} max={600} value={form.timeout} disabled={!canManage} onChange={(e) => setForm({ ...form, timeout: e.target.value })} />
          </Field>
          <Field label="Verify with" htmlFor="t-verify">
            <Select id="t-verify" value={form.verify_with} disabled={!canManage || form.kind !== "change"} onChange={(e) => setForm({ ...form, verify_with: e.target.value })}>
              <option value="">None</option>
              {readTools.filter((n) => n !== tool.name).map((n) => <option key={n} value={n}>{n}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Input schema" htmlFor="t-schema">
          <pre id="t-schema" className="max-h-60 overflow-auto rounded-lg border border-border bg-surface-2 p-3 font-mono text-xs">
            {JSON.stringify(tool.input_schema, null, 2)}
          </pre>
        </Field>
        <label className={check}>
          <input type="checkbox" className="size-4" checked={form.enabled} disabled={!canManage || tool.status === "missing"} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
          Enabled {tool.status === "new" && form.enabled && <span className="text-xs text-subtle">(enabling approves the definition shown)</span>}
        </label>
        {save.error && <Alert>{errorMessage(save.error)}</Alert>}
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ test run

export function ToolTestDialog({ tool, onClose }: { tool: ToolDefinition; onClose: () => void }) {
  const props = Object.entries(tool.input_schema.properties ?? {});
  const required = new Set(tool.input_schema.required ?? []);
  const [values, setValues] = useState<Record<string, string>>({});
  const [result, setResult] = useState<ToolRunResult | null>(null);
  const run = useMutation({
    mutationFn: () => toolDefinitionsApi.test(tool.id, coerceArguments(tool.input_schema, values)),
    onSuccess: setResult,
  });
  return (
    <Dialog
      open
      wide
      onOpenChange={(o) => !o && onClose()}
      title={`Test ${tool.name}`}
      description="Runs the tool once against its server. The run is recorded and audited."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Close</Button>
          <Button type="submit" form="tool-test" loading={run.isPending}>Run</Button>
        </>
      }
    >
      <form
        id="tool-test"
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          setResult(null);
          run.mutate();
        }}
      >
        {!props.length && <p className="text-sm text-muted">This tool takes no arguments.</p>}
        {props.map(([name, prop]) => (
          <Field key={name} label={`${name}${required.has(name) ? " *" : ""}`} htmlFor={`arg-${name}`} hint={prop.description}>
            {prop.enum ? (
              <Select id={`arg-${name}`} value={values[name] ?? ""} onChange={(e) => setValues({ ...values, [name]: e.target.value })}>
                <option value="">—</option>
                {prop.enum.map((v) => <option key={String(v)} value={String(v)}>{String(v)}</option>)}
              </Select>
            ) : prop.type === "boolean" ? (
              <Select id={`arg-${name}`} value={values[name] ?? ""} onChange={(e) => setValues({ ...values, [name]: e.target.value })}>
                <option value="">—</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </Select>
            ) : (
              <Input
                id={`arg-${name}`}
                type={prop.type === "integer" || prop.type === "number" ? "number" : "text"}
                value={values[name] ?? ""}
                onChange={(e) => setValues({ ...values, [name]: e.target.value })}
              />
            )}
          </Field>
        ))}
        {run.error && <Alert>{errorMessage(run.error)}</Alert>}
        {result && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-sm">
              <Badge tone={result.status === "success" ? "success" : "danger"}>{result.status}</Badge>
              <span>{result.summary}</span>
              <span className="ml-auto text-xs text-subtle">{result.duration_ms} ms</span>
            </div>
            {result.error && <Alert>{result.error}</Alert>}
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface-2 p-3 font-mono text-xs">
              {result.output || "(no output)"}
            </pre>
          </div>
        )}
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ HTTP tool editor

function KvEditor({ label, rows, onChange, placeholder }: { label: string; rows: KvRow[]; onChange: (rows: KvRow[]) => void; placeholder: string }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted">{label}</span>
        <Button type="button" variant="ghost" size="sm" onClick={() => onChange([...rows, { key: "", value: "" }])}>
          <Plus /> Add
        </Button>
      </div>
      {rows.map((row, i) => (
        <div key={i} className="flex gap-2">
          <Input aria-label={`${label} name`} value={row.key} placeholder="name" onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)))} />
          <Input aria-label={`${label} value`} value={row.value} placeholder={placeholder} className="font-mono" onChange={(e) => onChange(rows.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)))} />
          <Button type="button" variant="ghost" size="icon" aria-label="Remove" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            <Trash2 />
          </Button>
        </div>
      ))}
    </div>
  );
}

function ParamRow({ param, onChange, onRemove }: { param: ToolParam; onChange: (p: ToolParam) => void; onRemove: () => void }) {
  const num = (v: string) => (v === "" ? null : Number(v));
  return (
    <div className="grid grid-cols-12 gap-2 rounded-lg border border-border bg-surface-2/50 p-2">
      <Input className="col-span-3 font-mono" aria-label="Parameter name" placeholder="name" value={param.name} onChange={(e) => onChange({ ...param, name: e.target.value })} />
      <Select className="col-span-2" aria-label="Type" value={param.type} onChange={(e) => onChange({ ...param, type: e.target.value as ToolParam["type"] })}>
        {(["string", "integer", "number", "boolean"] as const).map((t) => <option key={t} value={t}>{t}</option>)}
      </Select>
      <Input className="col-span-6" aria-label="Description" placeholder="description" value={param.description} onChange={(e) => onChange({ ...param, description: e.target.value })} />
      <Button type="button" variant="ghost" size="icon" className="col-span-1" aria-label="Remove parameter" onClick={onRemove}>
        <Trash2 />
      </Button>
      <label className="col-span-2 flex items-center gap-2 text-xs">
        <input type="checkbox" className="size-4" checked={param.required} onChange={(e) => onChange({ ...param, required: e.target.checked })} /> required
      </label>
      {param.type === "string" ? (
        <>
          <Input className="col-span-7 font-mono" aria-label="Pattern" placeholder="pattern (regex), e.g. ^[a-z0-9-]+$" value={param.pattern ?? ""} onChange={(e) => onChange({ ...param, pattern: e.target.value || null })} />
          <Input className="col-span-3" type="number" min={1} aria-label="Max length" placeholder="max length" value={param.max_length ?? ""} onChange={(e) => onChange({ ...param, max_length: num(e.target.value) })} />
        </>
      ) : param.type !== "boolean" ? (
        <>
          <Input className="col-span-5" type="number" aria-label="Minimum" placeholder="minimum" value={param.minimum ?? ""} onChange={(e) => onChange({ ...param, minimum: num(e.target.value) })} />
          <Input className="col-span-5" type="number" aria-label="Maximum" placeholder="maximum" value={param.maximum ?? ""} onChange={(e) => onChange({ ...param, maximum: num(e.target.value) })} />
        </>
      ) : null}
    </div>
  );
}

export function HttpToolDialog({
  tool,
  integrations,
  readTools,
  onClose,
}: {
  tool: ToolDefinition | null;
  integrations: Integration[];
  readTools: string[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<HttpToolForm>(() => (tool ? definitionToForm(tool) : emptyHttpForm(integrations[0]?.id ?? "")));
  const [showSchema, setShowSchema] = useState(false);
  const set = <K extends keyof HttpToolForm>(k: K, v: HttpToolForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const problems = formProblems(form);
  const save = useMutation({
    mutationFn: async () => {
      if (!tool) return toolDefinitionsApi.createHttp(formToInput(form));
      await toolDefinitionsApi.updateHttp(tool.id, {
        description: form.description.trim(),
        parameters: formToParams(form),
        template: formToTemplate(form),
      });
      return toolDefinitionsApi.update(tool.id, {
        kind: form.kind,
        risk_level: form.kind === "change" ? form.risk_level : "low",
        timeout_seconds: Number(form.timeout_seconds) || 30,
        ...(form.verify_with ? { verify_with: form.verify_with } : { clear_verify_with: !!tool.verify_with }),
        enabled: form.enabled,
      });
    },
    onSuccess: () => {
      invalidateTools(queryClient);
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!problems.length) save.mutate();
  };

  return (
    <Dialog
      open
      wide
      onOpenChange={(o) => !o && onClose()}
      title={tool ? `Edit ${tool.name}` : "New HTTP tool"}
      description="A fixed request template. Arguments are validated against the parameters, path values are encoded as single segments, and requests never leave the integration's base URL."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="http-tool" loading={save.isPending} disabled={!!problems.length}>
            {tool ? "Save" : "Create tool"}
          </Button>
        </>
      }
    >
      <form id="http-tool" onSubmit={submit} className="space-y-4">
        {!integrations.length && <Alert tone="warning">Add an HTTP API integration under Tools &amp; Integrations first.</Alert>}
        <div className="grid grid-cols-2 gap-4">
          <Field label="Integration" htmlFor="h-int">
            <Select id="h-int" value={form.integration_id} disabled={!!tool} onChange={(e) => set("integration_id", e.target.value)}>
              <option value="">Choose…</option>
              {integrations.map((i) => (
                <option key={i.id} value={i.id}>{i.name} ({i.configuration.url})</option>
              ))}
            </Select>
          </Field>
          <Field label="Name" htmlFor="h-name" hint="The tool is called http_<name>.">
            <Input id="h-name" value={form.name} disabled={!!tool} maxLength={50} onChange={(e) => set("name", e.target.value)} placeholder="device_info" />
          </Field>
        </div>
        <Field label="Description (shown to agents)" htmlFor="h-desc">
          <Textarea id="h-desc" rows={2} maxLength={1000} value={form.description} onChange={(e) => set("description", e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Kind" htmlFor="h-kind">
            <Select id="h-kind" value={form.kind} onChange={(e) => set("kind", e.target.value as "read" | "change")}>
              <option value="read">Read</option>
              <option value="change">Change (approval)</option>
            </Select>
          </Field>
          <Field label="Risk" htmlFor="h-risk">
            <Select id="h-risk" value={form.risk_level} disabled={form.kind !== "change"} onChange={(e) => set("risk_level", e.target.value as RiskLevel)}>
              {RISKS.map((r) => <option key={r} value={r}>{r}</option>)}
            </Select>
          </Field>
          <Field label="Timeout (s)" htmlFor="h-timeout">
            <Input id="h-timeout" type="number" min={1} max={600} value={form.timeout_seconds} onChange={(e) => set("timeout_seconds", e.target.value)} />
          </Field>
          <Field label="Verify with" htmlFor="h-verify">
            <Select id="h-verify" value={form.verify_with} disabled={form.kind !== "change"} onChange={(e) => set("verify_with", e.target.value)}>
              <option value="">None</option>
              {readTools.filter((n) => n !== tool?.name).map((n) => <option key={n} value={n}>{n}</option>)}
            </Select>
          </Field>
        </div>

        <div className="grid grid-cols-4 gap-4">
          <Field label="Method" htmlFor="h-method">
            <Select id="h-method" value={form.method} onChange={(e) => set("method", e.target.value as HttpToolForm["method"])}>
              {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
            </Select>
          </Field>
          <div className="col-span-3">
            <Field label="Path" htmlFor="h-path" hint="Relative to the integration URL. Use {param} placeholders, e.g. /devices/{device}.">
              <Input id="h-path" className="font-mono" value={form.path} onChange={(e) => set("path", e.target.value)} />
            </Field>
          </div>
        </div>
        <KvEditor label="Query parameters" rows={form.query} onChange={(r) => set("query", r)} placeholder="{param} or fixed value" />
        <KvEditor label="Headers" rows={form.headers} onChange={(r) => set("headers", r)} placeholder="{param} or fixed value" />
        {methodAllowsBody(form.method) && (
          <Field label="JSON body template (optional)" htmlFor="h-body" hint='A string that is exactly "{param}" becomes the typed argument value.'>
            <Textarea id="h-body" rows={4} className="font-mono text-xs" value={form.body} onChange={(e) => set("body", e.target.value)} placeholder='{"device": "{device}", "enabled": true}' />
          </Field>
        )}

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted">Parameters</span>
            <div className="flex gap-1">
              <Button type="button" variant="ghost" size="sm" onClick={() => setShowSchema(!showSchema)}>{showSchema ? "Hide" : "Show"} schema</Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => set("params", [...form.params, emptyParam()])}><Plus /> Add parameter</Button>
            </div>
          </div>
          {form.params.map((p, i) => (
            <ParamRow
              key={i}
              param={p}
              onChange={(np) => set("params", form.params.map((q, j) => (j === i ? np : q)))}
              onRemove={() => set("params", form.params.filter((_, j) => j !== i))}
            />
          ))}
          {showSchema && (
            <pre className="max-h-48 overflow-auto rounded-lg border border-border bg-surface-2 p-3 font-mono text-xs">
              {JSON.stringify(paramsToSchemaPreview(form.params), null, 2)}
            </pre>
          )}
        </div>
        <label className={check}>
          <input type="checkbox" className="size-4" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} /> Enabled
        </label>
        {!!problems.length && <Alert tone="warning">{problems.join(" · ")}</Alert>}
        {save.error && <Alert>{errorMessage(save.error)}</Alert>}
      </form>
    </Dialog>
  );
}
