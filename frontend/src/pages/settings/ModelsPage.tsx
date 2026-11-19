import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Cpu, Pencil, Plus, Server, Star, Trash2, Wifi, Zap } from "lucide-react";
import { useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { modelsApi, type ModelInput } from "@/api/endpoints";
import { formatDuration } from "@/components/investigation/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Spinner, StatusDot } from "@/components/ui/display";
import { Field, Input, Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { ModelEndpoint, ModelTestResult, Provider } from "@/types/api";
import { formatRelative } from "@/utils/format";

const PROVIDERS: { value: Provider; label: string; url: string }[] = [
  { value: "ollama", label: "Ollama", url: "http://ollama:11434" },
  { value: "vllm", label: "vLLM", url: "http://vllm:8000/v1" },
  { value: "llamacpp", label: "llama.cpp server", url: "http://llamacpp:8080/v1" },
  { value: "openai_compatible", label: "OpenAI-compatible", url: "http://llm-gateway:8000/v1" },
];

const STATUS = {
  healthy: { tone: "success" as const, label: "Connected" },
  degraded: { tone: "warning" as const, label: "Degraded" },
  unhealthy: { tone: "danger" as const, label: "Unreachable" },
};

function SummaryCard({ label, value, icon: Icon, tone }: { label: string; value: string | number; icon: typeof Cpu; tone: string }) {
  return (
    <div className="panel flex items-center justify-between p-5">
      <div>
        <div className="text-xs text-muted">{label}</div>
        <div className="mt-1 truncate text-lg font-semibold">{value}</div>
      </div>
      <span className={`grid size-10 place-items-center rounded-xl ${tone}`}>
        <Icon className="size-5" />
      </span>
    </div>
  );
}

function ModelDialog({ model, open, onOpenChange }: { model: ModelEndpoint | null; open: boolean; onOpenChange: (o: boolean) => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState(() => ({
    name: model?.name ?? "",
    provider: model?.provider ?? ("ollama" as Provider),
    base_url: model?.base_url ?? "http://ollama:11434",
    model_name: model?.model_name ?? "",
    description: model?.description ?? "",
    timeout_seconds: String(model?.configuration.timeout_seconds ?? 120),
    max_retries: String(model?.configuration.max_retries ?? 2),
    structured_output: model?.configuration.structured_output ?? "json_object",
    thinking: model?.configuration.thinking ?? "auto",
    api_key: "",
    clear_api_key: false,
    is_default: model?.is_default ?? false,
  }));
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const mutation = useMutation({
    mutationFn: () => {
      const body: ModelInput = {
        name: form.name.trim(),
        provider: form.provider,
        base_url: form.base_url.trim(),
        model_name: form.model_name.trim(),
        description: form.description,
        configuration: {
          timeout_seconds: Number(form.timeout_seconds),
          max_retries: Number(form.max_retries),
          structured_output: form.structured_output,
          thinking: form.thinking,
        },
        is_default: form.is_default,
      };
      if (form.api_key) body.api_key = form.api_key;
      if (model && form.clear_api_key) body.clear_api_key = true;
      return model ? modelsApi.update(model.id, body) : modelsApi.create(body);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["models"] });
      onOpenChange(false);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={model ? `Edit ${model.name}` : "Add model endpoint"}
      description="Any OpenAI-compatible server inside your network: Ollama, vLLM, llama.cpp…"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="model-form" loading={mutation.isPending}>{model ? "Save" : "Add model"}</Button>
        </>
      }
    >
      <form id="model-form" onSubmit={submit} className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Name" htmlFor="m-name">
            <Input id="m-name" value={form.name} onChange={set("name")} required maxLength={100} placeholder="Local Ollama" />
          </Field>
          <Field label="Provider" htmlFor="m-provider">
            <Select
              id="m-provider"
              value={form.provider}
              onChange={(e) => {
                const p = PROVIDERS.find((x) => x.value === e.target.value)!;
                setForm((f) => ({ ...f, provider: p.value, base_url: model ? f.base_url : p.url }));
              }}
            >
              {PROVIDERS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </Select>
          </Field>
        </div>
        <Field label="Base URL" htmlFor="m-url" hint="Reachable from the LocalOps worker. /v1 is added automatically.">
          <Input id="m-url" value={form.base_url} onChange={set("base_url")} required className="font-mono" />
        </Field>
        <Field label="Model" htmlFor="m-model" hint="As listed by the server, e.g. qwen2.5:14b-instruct or meta-llama/Llama-3.1-8B-Instruct">
          <Input id="m-model" value={form.model_name} onChange={set("model_name")} required className="font-mono" />
        </Field>
        <div className="grid grid-cols-3 gap-4">
          <Field label="Timeout (s)" htmlFor="m-timeout">
            <Input id="m-timeout" type="number" min={5} max={900} value={form.timeout_seconds} onChange={set("timeout_seconds")} />
          </Field>
          <Field label="Retries" htmlFor="m-retries">
            <Input id="m-retries" type="number" min={0} max={5} value={form.max_retries} onChange={set("max_retries")} />
          </Field>
          <Field label="JSON mode" htmlFor="m-json">
            <Select id="m-json" value={form.structured_output} onChange={set("structured_output")}>
              <option value="json_object">json_object</option>
              <option value="json_schema">json_schema</option>
              <option value="none">prompt only</option>
            </Select>
          </Field>
        </div>
        <Field
          label="Reasoning (thinking)"
          htmlFor="m-thinking"
          hint="Thinking models write their reasoning before the answer. 'Disabled' asks the server to switch it off (vLLM/SGLang/llama.cpp chat-template flag, Ollama think=false, Qwen3 /no_think); servers that ignore it still work."
        >
          <Select id="m-thinking" value={form.thinking} onChange={set("thinking")}>
            <option value="auto">Auto: allow room for reasoning</option>
            <option value="enabled">Always thinks: larger token budget</option>
            <option value="disabled">Disable thinking where supported</option>
          </Select>
        </Field>
        <Field label="API key (optional)" htmlFor="m-key" hint={model?.has_api_key ? "A key is stored (encrypted). Leave blank to keep it." : "Stored encrypted with LOCALOPS_ENCRYPTION_KEY."}>
          <Input id="m-key" type="password" value={form.api_key} onChange={set("api_key")} autoComplete="new-password" />
        </Field>
        {model?.has_api_key && (
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.clear_api_key} onChange={(e) => setForm((f) => ({ ...f, clear_api_key: e.target.checked }))} className="size-4" />
            Remove stored API key
          </label>
        )}
        <Field label="Description" htmlFor="m-desc">
          <Input id="m-desc" value={form.description} onChange={set("description")} maxLength={1000} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.is_default} onChange={(e) => setForm((f) => ({ ...f, is_default: e.target.checked }))} className="size-4" />
          Use as the default model for agents
        </label>
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}

function ModelCard({ model, canManage, onEdit }: { model: ModelEndpoint; canManage: boolean; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const [result, setResult] = useState<ModelTestResult | null>(null);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["models"] });
  const test = useMutation({ mutationFn: () => modelsApi.test(model.id), onSuccess: (r) => { setResult(r); refresh(); } });
  const update = useMutation({ mutationFn: (body: Partial<ModelInput>) => modelsApi.update(model.id, body), onSuccess: refresh });
  const remove = useMutation({ mutationFn: () => modelsApi.remove(model.id), onSuccess: refresh });
  const status = model.last_status ? STATUS[model.last_status] : null;
  const provider = PROVIDERS.find((p) => p.value === model.provider)?.label ?? model.provider;

  return (
    <section className="panel p-5">
      <div className="flex flex-wrap items-start gap-4">
        <span className="grid size-10 place-items-center rounded-xl border border-border bg-surface-2">
          <Server className="size-5 text-blue-400" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{model.name}</h3>
            <Badge className="uppercase">{provider}</Badge>
            {model.is_default && <Badge tone="primary"><Star className="size-3" /> default</Badge>}
            {!model.enabled && <Badge>disabled</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-muted">{model.description || model.base_url}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {status ? (
            <span className="flex items-center gap-1.5 text-xs"><StatusDot tone={status.tone} /> {status.label}</span>
          ) : (
            <span className="text-xs text-subtle">Not tested</span>
          )}
          <Button variant="secondary" size="sm" onClick={() => test.mutate()} loading={test.isPending} disabled={!canManage}>
            <Zap /> Test
          </Button>
          {canManage && (
            <>
              {!model.is_default && model.enabled && (
                <Button variant="ghost" size="sm" onClick={() => update.mutate({ is_default: true })}>Make default</Button>
              )}
              <Button variant="ghost" size="sm" onClick={() => update.mutate({ enabled: !model.enabled })}>
                {model.enabled ? "Disable" : "Enable"}
              </Button>
              <Button variant="ghost" size="icon" aria-label="Edit" onClick={onEdit}><Pencil /></Button>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Delete"
                onClick={() => window.confirm(`Delete model endpoint "${model.name}"? Agents using it fall back to the default.`) && remove.mutate()}
              >
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      </div>

      <dl className="mt-5 grid grid-cols-2 gap-4 border-t border-border pt-4 text-sm md:grid-cols-4">
        <div><dt className="text-xs text-muted">Connection</dt><dd className="mt-0.5">{status?.label ?? "Unknown"}</dd></div>
        <div><dt className="text-xs text-muted">Last health check</dt><dd className="mt-0.5">{formatRelative(model.last_checked_at)}</dd></div>
        <div><dt className="text-xs text-muted">Response time</dt><dd className="mt-0.5 font-mono">{model.last_latency_ms != null ? formatDuration(model.last_latency_ms) : "N/A"}</dd></div>
        <div><dt className="text-xs text-muted">Model</dt><dd className="mt-0.5 truncate font-mono text-xs">{model.model_name}</dd></div>
      </dl>
      <p className="mt-2 font-mono text-[11px] text-subtle">{model.base_url}{model.has_api_key ? " · API key stored" : ""}</p>

      {model.last_error && !result && <div className="mt-3"><Alert tone="warning">{model.last_error}</Alert></div>}
      {(test.error || update.error || remove.error) && <div className="mt-3"><Alert>{errorMessage(test.error ?? update.error ?? remove.error)}</Alert></div>}
      {result && (
        <div className="mt-4 rounded-lg border border-border bg-surface-2 p-3 text-sm">
          {result.ok ? (
            <div className="space-y-1.5">
              <p className="text-green-300">
                Reachable in {formatDuration(result.latency_ms)} ·{" "}
                {result.model_available === false ? <span className="text-amber-300">model not listed by the server</span> : "model available"}
              </p>
              {result.reply && <p className="text-xs text-muted">Probe reply: <span className="font-mono text-fg">{result.reply}</span></p>}
              {!!result.models.length && (
                <p className="text-xs text-muted">Server models: <span className="font-mono">{result.models.slice(0, 12).join(", ")}{result.models.length > 12 ? "…" : ""}</span></p>
              )}
            </div>
          ) : (
            <p className="text-red-300">{result.error}</p>
          )}
        </div>
      )}
    </section>
  );
}

export function ModelsPage() {
  const canManage = useAuthStore((s) => s.hasPermission("settings.manage"));
  const query = useQuery({ queryKey: ["models"], queryFn: modelsApi.list });
  const [editing, setEditing] = useState<ModelEndpoint | null>(null);
  const [open, setOpen] = useState(false);
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const models = query.data;
  const def = models.find((m) => m.is_default);
  const healthy = models.filter((m) => m.last_status === "healthy");
  const latencies = healthy.map((m) => m.last_latency_ms ?? 0).filter(Boolean);
  const avg = latencies.length ? latencies.reduce((a, b) => a + b, 0) / latencies.length : null;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">AI Models</h1>
          <p className="text-sm text-muted">Self-hosted model endpoints used by LocalOps agents. Nothing is sent outside your network.</p>
        </div>
        {canManage && (
          <Button onClick={() => { setEditing(null); setOpen(true); }}><Plus /> Add model</Button>
        )}
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <SummaryCard label="Default model" value={def?.name ?? "None"} icon={Cpu} tone="bg-emerald-600/20 text-emerald-300" />
        <SummaryCard label="Connected" value={`${healthy.length} / ${models.length}`} icon={Wifi} tone="bg-blue-600/20 text-blue-300" />
        <SummaryCard label="Avg response" value={avg != null ? formatDuration(avg) : "—"} icon={Activity} tone="bg-violet-600/20 text-violet-300" />
        <SummaryCard label="Enabled endpoints" value={models.filter((m) => m.enabled).length} icon={Server} tone="bg-amber-600/20 text-amber-300" />
      </div>
      {!models.length ? (
        <div className="panel">
          <EmptyState title="No model endpoints" description="Add an Ollama, vLLM or other OpenAI-compatible endpoint so agents can run." />
        </div>
      ) : (
        models.map((m) => <ModelCard key={m.id} model={m} canManage={canManage} onEdit={() => { setEditing(m); setOpen(true); }} />)
      )}
      {open && <ModelDialog key={editing?.id ?? "new"} model={editing} open={open} onOpenChange={setOpen} />}
    </div>
  );
}
