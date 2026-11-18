import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, Bot, CheckCircle2, Cpu, Lock, Save, ShieldCheck } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { agentsApi, modelsApi } from "@/api/endpoints";
import { formatDuration } from "@/components/investigation/shared";
import { Button } from "@/components/ui/button";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner, StatusDot, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { Agent } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatRelative } from "@/utils/format";

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
        {agent.implemented ? (
          <Badge tone={agent.enabled ? "success" : "neutral"}>{agent.enabled ? "active" : "disabled"}</Badge>
        ) : (
          <Badge>phase {agent.available_in_phase}</Badge>
        )}
      </div>
      <p className="mt-1.5 line-clamp-2 text-xs text-muted">{agent.description}</p>
      {agent.stats.executions_24h > 0 && (
        <p className="mt-1.5 font-mono text-[11px] text-subtle">
          {agent.stats.executions_24h} runs · {agent.stats.failures_24h} failed (24h)
        </p>
      )}
    </button>
  );
}

function AgentEditor({ agent }: { agent: Agent }) {
  const canConfigure = useAuthStore((s) => s.hasPermission("agent.configure"));
  const canReadSettings = useAuthStore((s) => s.hasPermission("settings.read"));
  const queryClient = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: modelsApi.list, enabled: canReadSettings });
  const [form, setForm] = useState({
    model_id: agent.model_id ?? "",
    temperature: String(agent.configuration.temperature),
    max_tokens: String(agent.configuration.max_tokens),
    timeout_seconds: String(agent.configuration.timeout_seconds),
    max_retries: String(agent.configuration.max_retries),
    enabled: agent.enabled,
  });
  useEffect(() => {
    setForm({
      model_id: agent.model_id ?? "",
      temperature: String(agent.configuration.temperature),
      max_tokens: String(agent.configuration.max_tokens),
      timeout_seconds: String(agent.configuration.timeout_seconds),
      max_retries: String(agent.configuration.max_retries),
      enabled: agent.enabled,
    });
  }, [agent]);

  const save = useMutation({
    mutationFn: () =>
      agentsApi.update(agent.id, {
        model_id: form.model_id || null,
        enabled: form.enabled,
        configuration: {
          temperature: Number(form.temperature),
          max_tokens: Number(form.max_tokens),
          timeout_seconds: Number(form.timeout_seconds),
          max_retries: Number(form.max_retries),
        },
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["agents"] }),
  });
  const executions = useQuery({
    queryKey: ["agents", "executions", agent.agent_type],
    queryFn: () => agentsApi.executions({ page: 1, page_size: 8, agent_type: agent.agent_type }),
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const defaultModel = models.data?.find((m) => m.is_default);
  const readOnly = !canConfigure;

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
            <Badge tone="violet">{agent.model_name ?? (defaultModel ? `default · ${defaultModel.model_name}` : "default model")}</Badge>
          </div>
          <p className="mt-0.5 text-xs text-muted">{agent.description}</p>
        </div>
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
            <Field label="Model" htmlFor="model" hint={canReadSettings ? <>Manage endpoints in <Link to="/settings/models" className="text-blue-400 hover:underline">Settings → AI Models</Link>.</> : undefined}>
              <Select id="model" value={form.model_id} onChange={set("model_id")} disabled={readOnly}>
                <option value="">Tenant default{defaultModel ? ` (${defaultModel.name})` : ""}</option>
                {models.data?.filter((m) => m.enabled).map((m) => (
                  <option key={m.id} value={m.id}>{m.name} — {m.model_name}</option>
                ))}
                {agent.model_id && !models.data && <option value={agent.model_id}>{agent.model_name}</option>}
              </Select>
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
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))} disabled={readOnly} className="size-4 accent-[var(--color-primary)]" />
              Enabled
              {agent.agent_type === "supervisor" && !form.enabled && (
                <span className="text-xs text-amber-300">— investigations cannot run without the Supervisor</span>
              )}
            </label>
            {save.error && <Alert>{errorMessage(save.error)}</Alert>}
            {save.isSuccess && !save.isPending && <Alert tone="success">Saved.</Alert>}
            {canConfigure && (
              <div className="flex justify-end">
                <Button type="submit" loading={save.isPending}><Save /> Save</Button>
              </div>
            )}
          </form>
        </Panel>

        <div className="space-y-5">
          <Panel title="Last 24 hours">
            <div className="grid grid-cols-2 gap-3">
              {[
                { label: "Executions", value: agent.stats.executions_24h, icon: Activity, tone: "text-blue-400" },
                { label: "Failures", value: agent.stats.failures_24h, icon: Activity, tone: "text-red-400" },
                { label: "Avg duration", value: formatDuration(agent.stats.avg_duration_ms), icon: Cpu, tone: "text-violet-400" },
                { label: "Tokens", value: agent.stats.tokens_24h.toLocaleString(), icon: Cpu, tone: "text-green-400" },
              ].map((m) => (
                <div key={m.label} className="rounded-lg border border-border bg-surface-2 p-3">
                  <m.icon className={cn("size-4", m.tone)} />
                  <div className="mt-2 font-mono text-xl font-semibold tabular-nums">{m.value}</div>
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
                `Tools: ${agent.configuration.allowed_tools.length ? agent.configuration.allowed_tools.join(", ") : "none (tool access arrives with the tool registry)"}`,
              ].map((t) => (
                <li key={t} className="flex gap-2"><CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-green-400" /> {t}</li>
              ))}
              {readOnly && <li className="flex gap-2"><Lock className="mt-0.5 size-3.5 shrink-0" /> Read-only: requires agent.configure.</li>}
            </ul>
          </Panel>
        </div>
      </div>

      <Panel title="Recent executions" bodyClassName="px-0 pb-0">
        {executions.isLoading ? (
          <Spinner />
        ) : !executions.data?.items.length ? (
          <EmptyState title="No executions yet" />
        ) : (
          <Table>
            <thead>
              <tr><Th>Step</Th><Th>Status</Th><Th>Model</Th><Th>Tokens</Th><Th>Duration</Th><Th>When</Th></tr>
            </thead>
            <tbody>
              {executions.data.items.map((x) => (
                <tr key={x.id}>
                  <Td className="text-sm capitalize">
                    {x.investigation_id ? (
                      <Link to={`/investigations/${x.investigation_id}`} className="hover:text-blue-300">{x.node.replace(/_/g, " ")}</Link>
                    ) : x.node}
                    {x.error && <p className="max-w-xs truncate text-xs text-red-300 normal-case">{x.error}</p>}
                  </Td>
                  <Td><Badge tone={x.status === "success" ? "success" : x.status === "failed" ? "danger" : "neutral"}>{x.status}</Badge></Td>
                  <Td className="font-mono text-xs text-muted">{x.model_name ?? "—"}</Td>
                  <Td className="font-mono text-xs text-muted">{(x.prompt_tokens ?? 0) + (x.completion_tokens ?? 0) || "—"}</Td>
                  <Td className="font-mono text-xs text-muted">{formatDuration(x.duration_ms)}</Td>
                  <Td className="text-xs text-subtle">{formatRelative(x.started_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>
    </div>
  );
}

export function AgentsPage() {
  const query = useQuery({ queryKey: ["agents"], queryFn: agentsApi.list, refetchInterval: 30_000 });
  const [selected, setSelected] = useState<string | null>(null);
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const agents = query.data;
  const current = agents.find((a) => a.id === selected) ?? agents[0];
  const runs = agents.reduce((n, a) => n + a.stats.executions_24h, 0);
  const failed = agents.reduce((n, a) => n + a.stats.failures_24h, 0);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold">Agents</h1>
        <Badge>{agents.length} agents</Badge>
        <Badge tone="success">{agents.filter((a) => a.implemented).length} available</Badge>
        <Badge tone="primary">{runs} runs (24h)</Badge>
        {failed > 0 && <Badge tone="danger">{failed} failed</Badge>}
      </div>
      <div className="grid gap-5 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <Panel title="Registry" bodyClassName="space-y-2 px-3" className="h-fit">
          {agents.map((a) => (
            <AgentCard key={a.id} agent={a} selected={a.id === current?.id} onClick={() => setSelected(a.id)} />
          ))}
        </Panel>
        {current ? <AgentEditor key={current.id} agent={current} /> : <EmptyState title="No agents" />}
      </div>
    </div>
  );
}
