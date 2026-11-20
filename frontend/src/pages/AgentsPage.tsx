import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Bot, CheckCircle2, Cpu, Lock, Plus, Save, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { agentsApi, modelsApi, toolsApi } from "@/api/endpoints";
import {
  formatSuccessRate,
  formFromAgent,
  groupReadTools,
  MAX_INSTRUCTIONS,
  slugPreview,
  toggle,
  updatePayload,
  type AgentForm,
} from "@/components/agents/agents";
import { formatDuration } from "@/components/investigation/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner, StatusDot, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { Agent, AgentExecution, ModelEndpoint, ToolInfo } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatRelative } from "@/utils/format";

const INSTRUCTIONS_HINT = "Added after the built-in safety rules; cannot override them.";

function AgentCard({ agent, selected, onClick }: { agent: Agent; selected: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "w-full rounded-xl border p-3 text-left transition-colors",
        selected ? "border-primary/60 bg-primary/10" : "border-border bg-surface-2/40 hover:border-border-strong",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2">
          <StatusDot tone={!agent.enabled ? "neutral" : agent.implemented ? "success" : "warning"} />
          <span className="truncate text-sm font-medium">{agent.name}</span>
        </span>
        <span className="flex shrink-0 gap-1">
          {agent.custom && <Badge tone="violet">custom</Badge>}
          {agent.implemented ? (
            <Badge tone={agent.enabled ? "success" : "neutral"}>{agent.enabled ? "active" : "disabled"}</Badge>
          ) : (
            <Badge>phase {agent.available_in_phase}</Badge>
          )}
        </span>
      </div>
      <p className="mt-1.5 line-clamp-2 text-xs text-muted">{agent.description}</p>
      {agent.stats.executions_total > 0 && (
        <p className="mt-1.5 font-mono text-[11px] text-subtle">
          {agent.stats.executions_total} runs · {formatSuccessRate(agent.stats.success_rate)} ok
          {agent.stats.last_run_at ? ` · ${formatRelative(agent.stats.last_run_at)}` : ""}
        </p>
      )}
    </button>
  );
}

/** Checkbox list of read tools, grouped by source. */
function ToolPicker({
  tools,
  value,
  onChange,
  disabled,
}: {
  tools: ToolInfo[] | undefined;
  value: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const groups = useMemo(() => groupReadTools(tools ?? []), [tools]);
  if (!tools) return <p className="text-xs text-subtle">Loading tools…</p>;
  const known = new Set(groups.flatMap((g) => g.tools.map((t) => t.name)));
  const stale = value.filter((n) => !known.has(n));
  return (
    <div className="max-h-64 space-y-3 overflow-y-auto rounded-lg border border-border bg-surface-2/40 p-3">
      {groups.map((g) => (
        <div key={g.source}>
          <p className="mb-1 text-[11px] font-semibold tracking-wide text-subtle uppercase">{g.label}</p>
          <div className="grid gap-1 sm:grid-cols-2">
            {g.tools.map((t) => (
              <label key={t.name} className="flex items-start gap-2 text-xs" title={t.description}>
                <input
                  type="checkbox"
                  className="mt-0.5 size-3.5 accent-[var(--color-primary)]"
                  checked={value.includes(t.name)}
                  onChange={() => onChange(toggle(value, t.name))}
                  disabled={disabled}
                />
                <span className="font-mono break-all">{t.name}</span>
              </label>
            ))}
          </div>
        </div>
      ))}
      {stale.length > 0 && (
        <p className="text-xs text-amber-300">
          No longer available (disabled or removed): {stale.join(", ")} — they are ignored when the agent runs.
        </p>
      )}
    </div>
  );
}

function useChatModels(enabled: boolean) {
  const models = useQuery({ queryKey: ["models"], queryFn: modelsApi.list, enabled });
  const chat = (models.data ?? []).filter((m) => (m.purpose ?? "chat") === "chat");
  return { query: models, chat, defaultModel: chat.find((m) => m.is_default) };
}

function ModelSelect({
  id,
  value,
  onChange,
  chat,
  defaultModel,
  fallback,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  chat: ModelEndpoint[];
  defaultModel?: ModelEndpoint;
  fallback?: { id: string; name: string | null };
  disabled?: boolean;
}) {
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled}>
      <option value="">Tenant default{defaultModel ? ` (${defaultModel.name})` : ""}</option>
      {chat
        .filter((m) => m.enabled)
        .map((m) => (
          <option key={m.id} value={m.id}>
            {m.name} — {m.model_name}
          </option>
        ))}
      {fallback && !chat.some((m) => m.id === fallback.id) && <option value={fallback.id}>{fallback.name ?? fallback.id}</option>}
    </Select>
  );
}

function AgentEditor({ agent, onDeleted }: { agent: Agent; onDeleted: () => void }) {
  const canConfigure = useAuthStore((s) => s.hasPermission("agent.configure"));
  const canReadSettings = useAuthStore((s) => s.hasPermission("settings.read"));
  const queryClient = useQueryClient();
  const { chat, defaultModel } = useChatModels(canReadSettings);
  const tools = useQuery({ queryKey: ["tools", "catalogue"], queryFn: () => toolsApi.list(), enabled: agent.uses_tools });
  const [form, setForm] = useState<AgentForm>(() => formFromAgent(agent));
  const [confirmDelete, setConfirmDelete] = useState(false);
  useEffect(() => setForm(formFromAgent(agent)), [agent]);

  const payload = updatePayload(agent, form);
  const dirty = Object.keys(payload).length > 0;
  const save = useMutation({
    mutationFn: () => agentsApi.update(agent.id, payload),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["agents"] }),
  });
  const remove = useMutation({
    mutationFn: () => agentsApi.remove(agent.id),
    onSuccess: () => {
      setConfirmDelete(false);
      onDeleted();
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (dirty) save.mutate();
  };
  const set = (k: keyof AgentForm) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const readOnly = !canConfigure;
  const toolsMissing = agent.custom && form.allowed_tools.length === 0;

  return (
    <div className="space-y-5">
      <div className="panel flex flex-wrap items-center gap-3 px-5 py-4">
        <span className="grid size-10 place-items-center rounded-xl bg-violet-600 shadow-lg shadow-violet-600/30">
          <Bot className="size-5 text-white" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold">{agent.name}</h2>
            <Badge className="font-mono">{agent.agent_type}</Badge>
            {agent.custom && <Badge tone="violet">Custom</Badge>}
            <Badge tone="violet">{agent.model_name ?? (defaultModel ? `default · ${defaultModel.model_name}` : "default model")}</Badge>
          </div>
          <p className="mt-0.5 text-xs text-muted">{agent.description}</p>
        </div>
        {agent.custom && canConfigure && (
          <Button variant="ghost" onClick={() => setConfirmDelete(true)}>
            <Trash2 /> Delete
          </Button>
        )}
      </div>

      {!agent.implemented && (
        <Alert tone="primary">
          This agent ships in phase {agent.available_in_phase}. The Supervisor can already plan work for it; those tasks
          are reported as skipped until the agent is available. You can pre-configure its model now.
        </Alert>
      )}

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Panel title="Configuration">
          <form onSubmit={submit} className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Name" htmlFor="agent-name">
                <Input id="agent-name" value={form.name} maxLength={100} onChange={set("name")} disabled={readOnly} />
              </Field>
              <Field
                label="Model"
                htmlFor="model"
                hint={canReadSettings ? <>Chat models from <Link to="/settings/models" className="text-blue-400 hover:underline">Settings → AI Models</Link>.</> : undefined}
              >
                <ModelSelect
                  id="model"
                  value={form.model_id}
                  onChange={(v) => setForm((f) => ({ ...f, model_id: v }))}
                  chat={chat}
                  defaultModel={defaultModel}
                  fallback={agent.model_id ? { id: agent.model_id, name: agent.model_name } : undefined}
                  disabled={readOnly}
                />
              </Field>
            </div>
            <Field label="Description" htmlFor="agent-desc" hint={agent.custom ? "The Supervisor plans with this text." : undefined}>
              <Textarea id="agent-desc" rows={2} maxLength={1000} value={form.description} onChange={set("description")} disabled={readOnly} />
            </Field>
            <div className="grid grid-cols-2 gap-4">
              <Field label={`Temperature (${Number(form.temperature).toFixed(2)})`} htmlFor="temperature" hint="Low values keep diagnostics deterministic.">
                <input id="temperature" type="range" min="0" max="1" step="0.05" value={form.temperature} onChange={set("temperature")} disabled={readOnly} className="w-full accent-[var(--color-primary)]" />
              </Field>
              <Field label="Max output tokens" htmlFor="max_tokens">
                <Input id="max_tokens" type="number" min={64} max={32768} value={form.max_tokens} onChange={set("max_tokens")} disabled={readOnly} />
              </Field>
              <Field label="Request timeout (s)" htmlFor="timeout">
                <Input id="timeout" type="number" min={5} max={900} value={form.timeout_seconds} onChange={set("timeout_seconds")} disabled={readOnly} />
              </Field>
              <Field label="Retries" htmlFor="retries">
                <Input id="retries" type="number" min={0} max={5} value={form.max_retries} onChange={set("max_retries")} disabled={readOnly} />
              </Field>
            </div>
            {agent.uses_tools && (
              <>
                <Field label="Max tool steps" htmlFor="max_steps" hint="1–15 rounds of tool calls before the agent must conclude.">
                  <Input id="max_steps" type="number" min={1} max={15} value={form.max_steps} onChange={set("max_steps")} disabled={readOnly} />
                </Field>
                <div className="space-y-1.5">
                  <p className="text-xs font-medium text-muted">Allowed tools</p>
                  <ToolPicker
                    tools={tools.data}
                    value={form.allowed_tools}
                    onChange={(next) => setForm((f) => ({ ...f, allowed_tools: next }))}
                    disabled={readOnly}
                  />
                  <p className="text-xs text-subtle">
                    {agent.custom
                      ? "A custom agent needs at least one tool."
                      : "None selected = the agent's built-in tool set."}{" "}
                    Only read-only tools can be given to agents; remediation tools run through approvals.
                  </p>
                </div>
                <Field label={`Operator instructions (${form.instructions.length}/${MAX_INSTRUCTIONS})`} htmlFor="instructions" hint={INSTRUCTIONS_HINT}>
                  <Textarea id="instructions" rows={4} maxLength={MAX_INSTRUCTIONS} value={form.instructions} onChange={set("instructions")} disabled={readOnly} />
                </Field>
              </>
            )}
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))} disabled={readOnly} className="size-4 accent-[var(--color-primary)]" />
              Enabled
              {agent.agent_type === "supervisor" && !form.enabled && (
                <span className="text-xs text-amber-300">— investigations cannot run without the Supervisor</span>
              )}
              {agent.specialist && !form.enabled && agent.agent_type !== "supervisor" && (
                <span className="text-xs text-subtle">— the Supervisor will not plan it</span>
              )}
            </label>
            {save.error && <Alert>{errorMessage(save.error)}</Alert>}
            {save.isSuccess && !save.isPending && !dirty && <Alert tone="success">Saved.</Alert>}
            {canConfigure && (
              <div className="flex justify-end gap-2">
                {dirty && (
                  <Button type="button" variant="ghost" onClick={() => setForm(formFromAgent(agent))}>
                    Reset
                  </Button>
                )}
                <Button type="submit" loading={save.isPending} disabled={!dirty || toolsMissing}>
                  <Save /> Save
                </Button>
              </div>
            )}
          </form>
        </Panel>

        <div className="space-y-5">
          <Panel title="Statistics">
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: "Executions (total)", value: agent.stats.executions_total.toLocaleString(), icon: Activity, tone: "text-blue-400" },
                { label: "Success rate", value: formatSuccessRate(agent.stats.success_rate), icon: CheckCircle2, tone: "text-green-400" },
                { label: "Avg duration", value: formatDuration(agent.stats.avg_duration_ms), icon: Cpu, tone: "text-violet-400" },
                { label: "Last run", value: agent.stats.last_run_at ? formatRelative(agent.stats.last_run_at) : "—", icon: Activity, tone: "text-amber-400" },
                { label: "Runs (24h)", value: agent.stats.executions_24h, icon: Activity, tone: "text-blue-400" },
                { label: "Tokens (24h)", value: agent.stats.tokens_24h.toLocaleString(), icon: Cpu, tone: "text-green-400" },
              ].map((m) => (
                <div key={m.label} className="rounded-lg border border-border bg-surface-2 p-3">
                  <m.icon className={cn("size-4", m.tone)} />
                  <div className="mt-2 font-mono text-lg font-semibold tabular-nums">{m.value}</div>
                  <div className="text-xs text-muted">{m.label}</div>
                </div>
              ))}
            </div>
          </Panel>
          <Panel title={<span className="flex items-center gap-2"><ShieldCheck className="size-4 text-green-400" /> Guardrails</span>}>
            <ul className="space-y-2 text-xs text-muted">
              {[
                "Model output is validated against a strict schema before use.",
                "Requests and prompts never leave your configured endpoints.",
                "Every execution is recorded with model, tokens and duration.",
                agent.uses_tools
                  ? `Tools: ${agent.configuration.allowed_tools.length ? agent.configuration.allowed_tools.join(", ") : "built-in defaults"} (read-only; changes need approval)`
                  : "This agent does not call tools.",
              ].map((t) => (
                <li key={t} className="flex gap-2"><CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-green-400" /> {t}</li>
              ))}
              {readOnly && <li className="flex gap-2"><Lock className="mt-0.5 size-3.5 shrink-0" /> Read-only: requires agent.configure.</li>}
            </ul>
          </Panel>
        </div>
      </div>

      <Dialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete ${agent.name}?`}
        description="The Supervisor stops planning it. Its execution history is kept."
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>Cancel</Button>
            <Button variant="danger" loading={remove.isPending} onClick={() => remove.mutate()}>
              <Trash2 /> Delete
            </Button>
          </>
        }
      >
        {remove.error ? <Alert>{errorMessage(remove.error)}</Alert> : <p className="text-sm text-muted">This cannot be undone.</p>}
      </Dialog>
    </div>
  );
}

const STATUS_TONE: Record<AgentExecution["status"], "success" | "danger" | "neutral"> = {
  success: "success",
  failed: "danger",
  running: "neutral",
  interrupted: "neutral",
};

function ExecutionsPanel({ agents }: { agents: Agent[] }) {
  const [filters, setFilters] = useState<{ agent_type: string; status: string; errors_only: boolean }>({
    agent_type: "",
    status: "",
    errors_only: false,
  });
  const [page, setPage] = useState(1);
  const executions = useQuery({
    queryKey: ["agents", "executions", filters, page],
    queryFn: () =>
      agentsApi.executions({
        page,
        page_size: 15,
        agent_type: filters.agent_type || undefined,
        status: (filters.status || undefined) as AgentExecution["status"] | undefined,
        errors_only: filters.errors_only || undefined,
      }),
  });
  const names = Object.fromEntries(agents.map((a) => [a.agent_type, a.name]));
  const total = executions.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / 15));
  const update = (patch: Partial<typeof filters>) => {
    setFilters((f) => ({ ...f, ...patch }));
    setPage(1);
  };

  return (
    <Panel
      title="Execution history"
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Select aria-label="Agent" value={filters.agent_type} onChange={(e) => update({ agent_type: e.target.value })} className="h-8 w-48 text-xs">
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a.agent_type} value={a.agent_type}>{a.name}</option>
            ))}
          </Select>
          <Select aria-label="Status" value={filters.status} onChange={(e) => update({ status: e.target.value })} className="h-8 w-32 text-xs">
            <option value="">Any status</option>
            <option value="success">Success</option>
            <option value="failed">Failed</option>
            <option value="running">Running</option>
          </Select>
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <input type="checkbox" checked={filters.errors_only} onChange={(e) => update({ errors_only: e.target.checked })} className="size-3.5 accent-[var(--color-primary)]" />
            Errors only
          </label>
        </div>
      }
      bodyClassName="px-0 pb-0"
    >
      {executions.isLoading ? (
        <Spinner />
      ) : executions.error ? (
        <ErrorState message={errorMessage(executions.error)} onRetry={() => executions.refetch()} />
      ) : !executions.data?.items.length ? (
        <EmptyState title="No executions match" />
      ) : (
        <>
          <Table>
            <thead>
              <tr><Th>Agent</Th><Th>Step</Th><Th>Status</Th><Th>Model</Th><Th>Tokens</Th><Th>Duration</Th><Th>When</Th></tr>
            </thead>
            <tbody>
              {executions.data.items.map((x) => (
                <tr key={x.id}>
                  <Td className="text-sm">{names[x.agent_type] ?? x.agent_type}</Td>
                  <Td className="text-sm capitalize">
                    {x.investigation_id ? (
                      <Link to={`/investigations/${x.investigation_id}`} className="hover:text-blue-300">{x.node.replace(/_/g, " ")}</Link>
                    ) : x.node}
                    {x.error && <p className="max-w-xs truncate text-xs text-red-300 normal-case" title={x.error}>{x.error}</p>}
                  </Td>
                  <Td><Badge tone={STATUS_TONE[x.status]}>{x.status}</Badge></Td>
                  <Td className="font-mono text-xs text-muted">{x.model_name ?? "—"}</Td>
                  <Td className="font-mono text-xs text-muted">{(x.prompt_tokens ?? 0) + (x.completion_tokens ?? 0) || "—"}</Td>
                  <Td className="font-mono text-xs text-muted">{formatDuration(x.duration_ms)}</Td>
                  <Td className="text-xs text-subtle">{formatRelative(x.started_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          {pages > 1 && (
            <div className="flex items-center justify-end gap-2 px-4 py-3 text-xs text-muted">
              <Button size="sm" variant="ghost" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</Button>
              <span>Page {page} of {pages}</span>
              <Button size="sm" variant="ghost" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>Next</Button>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

const EMPTY_CREATE = { name: "", description: "", slug: "", model_id: "", instructions: "", max_steps: "6", allowed_tools: [] as string[] };

function CreateAgentDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated: (a: Agent) => void }) {
  const canReadSettings = useAuthStore((s) => s.hasPermission("settings.read"));
  const queryClient = useQueryClient();
  const { chat, defaultModel } = useChatModels(canReadSettings && open);
  const tools = useQuery({ queryKey: ["tools", "catalogue"], queryFn: () => toolsApi.list(), enabled: open });
  const [form, setForm] = useState(EMPTY_CREATE);
  useEffect(() => {
    if (open) setForm(EMPTY_CREATE);
  }, [open]);
  const create = useMutation({
    mutationFn: () =>
      agentsApi.create({
        name: form.name.trim(),
        description: form.description.trim(),
        slug: form.slug.trim() || null,
        model_id: form.model_id || null,
        allowed_tools: form.allowed_tools,
        instructions: form.instructions.trim() || undefined,
        max_steps: Number(form.max_steps) || undefined,
      }),
    onSuccess: (agent) => {
      void queryClient.invalidateQueries({ queryKey: ["agents"] });
      onCreated(agent);
      onOpenChange(false);
    },
  });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const valid = form.name.trim().length > 0 && form.description.trim().length >= 10 && form.allowed_tools.length > 0;

  return (
    <Dialog
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="New custom agent"
      description="A specialist that investigates with only the read-only tools you choose."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button loading={create.isPending} disabled={!valid} onClick={() => create.mutate()}>
            <Plus /> Create agent
          </Button>
        </>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Name" htmlFor="new-name" hint={form.name ? <span className="font-mono">{slugPreview(form.name, form.slug)}</span> : undefined}>
          <Input id="new-name" value={form.name} maxLength={100} onChange={set("name")} placeholder="NetBox Agent" />
        </Field>
        <Field label="Slug (optional)" htmlFor="new-slug" hint="Lowercase letters, digits and underscores.">
          <Input id="new-slug" value={form.slug} maxLength={40} pattern="[a-z][a-z0-9_]*" onChange={set("slug")} placeholder="netbox" />
        </Field>
      </div>
      <Field label="Description" htmlFor="new-desc" hint="At least 10 characters. The Supervisor decides when to plan this agent from this text.">
        <Textarea id="new-desc" rows={2} maxLength={1000} value={form.description} onChange={set("description")} placeholder="Looks up switches, ports and VLANs in NetBox." />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Model" htmlFor="new-model">
          <ModelSelect id="new-model" value={form.model_id} onChange={(v) => setForm((f) => ({ ...f, model_id: v }))} chat={chat} defaultModel={defaultModel} />
        </Field>
        <Field label="Max tool steps" htmlFor="new-steps">
          <Input id="new-steps" type="number" min={1} max={15} value={form.max_steps} onChange={set("max_steps")} />
        </Field>
      </div>
      <div className="space-y-1.5">
        <p className="text-xs font-medium text-muted">Allowed tools (at least one)</p>
        <ToolPicker tools={tools.data} value={form.allowed_tools} onChange={(next) => setForm((f) => ({ ...f, allowed_tools: next }))} />
      </div>
      <Field label={`Operator instructions (${form.instructions.length}/${MAX_INSTRUCTIONS})`} htmlFor="new-instr" hint={INSTRUCTIONS_HINT}>
        <Textarea id="new-instr" rows={3} maxLength={MAX_INSTRUCTIONS} value={form.instructions} onChange={set("instructions")} />
      </Field>
      {create.error && <Alert>{errorMessage(create.error)}</Alert>}
    </Dialog>
  );
}

export function AgentsPage() {
  const canConfigure = useAuthStore((s) => s.hasPermission("agent.configure"));
  const query = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list, refetchInterval: 30_000 });
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const agents = query.data;
  const current = agents.find((a) => a.id === selected) ?? agents[0];
  const runs = agents.reduce((n, a) => n + a.stats.executions_24h, 0);
  const failed = agents.reduce((n, a) => n + a.stats.failures_24h, 0);
  const custom = agents.filter((a) => a.custom).length;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Agents</h1>
        <Badge>{agents.length} agents</Badge>
        <Badge tone="success">{agents.filter((a) => a.implemented && a.enabled).length} active</Badge>
        {custom > 0 && <Badge tone="violet">{custom} custom</Badge>}
        <Badge tone="primary">{runs} runs (24h)</Badge>
        {failed > 0 && <Badge tone="danger">{failed} failed</Badge>}
        {canConfigure && (
          <Button className="ml-auto" onClick={() => setCreating(true)}>
            <Plus /> New custom agent
          </Button>
        )}
      </div>
      <div className="grid gap-5 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <Panel title="Registry" bodyClassName="space-y-2 px-3" className="h-fit">
          {agents.map((a) => (
            <AgentCard key={a.id} agent={a} selected={a.id === current?.id} onClick={() => setSelected(a.id)} />
          ))}
        </Panel>
        {current ? (
          <AgentEditor key={current.id} agent={current} onDeleted={() => setSelected(null)} />
        ) : (
          <EmptyState title="No agents" />
        )}
      </div>
      <ExecutionsPanel agents={agents} />
      <CreateAgentDialog open={creating} onOpenChange={setCreating} onCreated={(a) => setSelected(a.id)} />
    </div>
  );
}
