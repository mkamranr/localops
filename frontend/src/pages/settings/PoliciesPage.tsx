import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Scale, Trash2, Wand2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { policiesApi, resourcesApi, type PolicyPreview } from "@/api/endpoints";
import {
  EMPTY_FORM,
  formToInput,
  policyToForm,
  summariseEffect,
  summariseMatch,
  type PolicyForm,
  type TriState,
} from "@/components/remediation/policy";
import { RISK_META } from "@/components/remediation/remediation";
import { RiskBadge } from "@/components/remediation/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { ExecutionMode, Policy, RiskLevel, ToolInfo } from "@/types/api";

const RISKS: RiskLevel[] = ["low", "medium", "high", "critical"];

function TriSelect({ id, value, onChange, yes, no }: { id: string; value: TriState; onChange: (v: TriState) => void; yes: string; no: string }) {
  return (
    <Select id={id} value={value} onChange={(e) => onChange(e.target.value as TriState)}>
      <option value="inherit">No change</option>
      <option value="yes">{yes}</option>
      <option value="no">{no}</option>
    </Select>
  );
}

function PolicyEditor({
  open,
  policy,
  actions,
  onClose,
}: {
  open: boolean;
  policy: Policy | null;
  actions: ToolInfo[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState<PolicyForm>(policy ? policyToForm(policy) : EMPTY_FORM);
  const set = <K extends keyof PolicyForm>(key: K, value: PolicyForm[K]) => setForm((f) => ({ ...f, [key]: value }));
  const save = useMutation({
    mutationFn: () => (policy ? policiesApi.update(policy.id, formToInput(form)) : policiesApi.create(formToInput(form))),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["policies"] });
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    save.mutate();
  };
  const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  return (
    <Dialog wide open={open} onOpenChange={(o) => !o && onClose()} title={policy ? `Edit policy: ${policy.name}` : "New remediation policy"} description="Rules apply in ascending priority; later matching rules override earlier ones. Critical actions are never auto-approved or self-approved, whatever a rule says.">
      <form onSubmit={submit} className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-[1fr_8rem]">
          <Field label="Name" htmlFor="p-name">
            <Input id="p-name" value={form.name} onChange={(e) => set("name", e.target.value)} required minLength={3} maxLength={100} />
          </Field>
          <Field label="Priority" htmlFor="p-priority">
            <Input id="p-priority" type="number" min={0} max={10000} value={form.priority} onChange={(e) => set("priority", Number(e.target.value))} />
          </Field>
        </div>
        <Field label="Description" htmlFor="p-desc">
          <Textarea id="p-desc" rows={2} value={form.description} onChange={(e) => set("description", e.target.value)} maxLength={1000} />
        </Field>

        <fieldset className="space-y-3 rounded-lg border border-border p-4">
          <legend className="px-1 text-xs font-medium tracking-wide text-muted uppercase">When (empty matches anything)</legend>
          <div>
            <div className="mb-1.5 text-xs font-medium text-muted">Actions</div>
            <div className="grid max-h-40 grid-cols-1 gap-1 overflow-y-auto sm:grid-cols-2">
              {actions.map((a) => (
                <label key={a.name} className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={form.tools.includes(a.name)} onChange={() => set("tools", toggle(form.tools, a.name))} />
                  <span className="font-mono">{a.name}</span>
                  <span className="text-subtle">({a.risk_level})</span>
                </label>
              ))}
            </div>
          </div>
          <div>
            <div className="mb-1.5 text-xs font-medium text-muted">Risk decided so far</div>
            <div className="flex flex-wrap gap-3">
              {RISKS.map((r) => (
                <label key={r} className="flex items-center gap-1.5 text-xs">
                  <input type="checkbox" checked={form.risks.includes(r)} onChange={() => set("risks", toggle(form.risks, r))} /> {r}
                </label>
              ))}
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Environments" htmlFor="p-env" hint="Comma-separated; *nonprod* = set and not production">
              <Input id="p-env" value={form.environments} onChange={(e) => set("environments", e.target.value)} placeholder="production, staging" />
            </Field>
            <Field label="Resource types" htmlFor="p-types">
              <Input id="p-types" value={form.resourceTypes} onChange={(e) => set("resourceTypes", e.target.value)} placeholder="docker_container" />
            </Field>
            <Field label="Tags (any of)" htmlFor="p-tags">
              <Input id="p-tags" value={form.tags} onChange={(e) => set("tags", e.target.value)} placeholder="database, pci" />
            </Field>
            <Field label="Groups" htmlFor="p-groups">
              <Input id="p-groups" value={form.groups} onChange={(e) => set("groups", e.target.value)} placeholder="payments" />
            </Field>
          </div>
        </fieldset>

        <fieldset className="space-y-3 rounded-lg border border-border p-4">
          <legend className="px-1 text-xs font-medium tracking-wide text-muted uppercase">Then</legend>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Risk level" htmlFor="p-risk">
              <Select id="p-risk" value={form.riskLevel} onChange={(e) => set("riskLevel", e.target.value as RiskLevel | "")}>
                <option value="">No change</option>
                {RISKS.map((r) => <option key={r} value={r}>{RISK_META[r].label}</option>)}
              </Select>
            </Field>
            <Field label="Approval valid for (minutes)" htmlFor="p-ttl">
              <Input id="p-ttl" type="number" min={5} max={10080} value={form.ttl} onChange={(e) => set("ttl", e.target.value)} placeholder="default" />
            </Field>
            <Field label="Unattended in autonomous mode" htmlFor="p-auto">
              <TriSelect id="p-auto" value={form.autoApprove} onChange={(v) => set("autoApprove", v)} yes="Allowed" no="Never" />
            </Field>
            <Field label="Requester may approve" htmlFor="p-self">
              <TriSelect id="p-self" value={form.selfApproval} onChange={(v) => set("selfApproval", v)} yes="Allowed" no="Second person required" />
            </Field>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.deny} onChange={(e) => set("deny", e.target.checked)} /> Block matching actions entirely
          </label>
          {form.deny && <Input value={form.denyReason} onChange={(e) => set("denyReason", e.target.value)} placeholder="Reason shown to users" maxLength={300} aria-label="Deny reason" />}
        </fieldset>

        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} /> Enabled
        </label>
        {save.error && <Alert>{errorMessage(save.error)}</Alert>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" loading={save.isPending}>Save policy</Button>
        </div>
      </form>
    </Dialog>
  );
}

function PolicyPreviewPanel({ actions }: { actions: ToolInfo[] }) {
  const [tool, setTool] = useState("");
  const [q, setQ] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [mode, setMode] = useState<ExecutionMode>("assisted");
  const spec = actions.find((a) => a.name === tool);
  const resources = useQuery({
    queryKey: ["resources", "policy-preview", q],
    queryFn: () => resourcesApi.list({ page: 1, page_size: 20, q: q || undefined }),
    enabled: !!tool,
  });
  const options = (resources.data?.items ?? []).filter((r) => !spec || spec.resource_types.includes(r.resource_type));
  const preview = useMutation({ mutationFn: () => policiesApi.preview({ tool, resource_id: resourceId, execution_mode: mode }) });
  const d: PolicyPreview | undefined = preview.data;
  return (
    <Panel title={<span className="flex items-center gap-2"><Wand2 className="size-4 text-violet-400" /> Try the policies</span>}>
      <div className="grid gap-3 md:grid-cols-4">
        <Select value={tool} onChange={(e) => { setTool(e.target.value); setResourceId(""); preview.reset(); }} aria-label="Action">
          <option value="">Choose an action…</option>
          {actions.map((a) => <option key={a.name} value={a.name}>{a.name}</option>)}
        </Select>
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter resources…" aria-label="Filter resources" disabled={!tool} />
        <Select value={resourceId} onChange={(e) => { setResourceId(e.target.value); preview.reset(); }} aria-label="Resource" disabled={!tool}>
          <option value="">Choose a resource…</option>
          {options.map((r) => <option key={r.id} value={r.id}>{r.name} ({r.environment ?? "no environment"})</option>)}
        </Select>
        <div className="flex gap-2">
          <Select value={mode} onChange={(e) => setMode(e.target.value as ExecutionMode)} aria-label="Mode">
            <option value="manual">manual</option>
            <option value="assisted">assisted</option>
            <option value="autonomous">autonomous</option>
          </Select>
          <Button variant="secondary" onClick={() => preview.mutate()} disabled={!tool || !resourceId} loading={preview.isPending}>Evaluate</Button>
        </div>
      </div>
      {preview.error && <div className="mt-3"><Alert>{errorMessage(preview.error)}</Alert></div>}
      {d && (
        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          <RiskBadge risk={d.risk_level} />
          {d.denied ? (
            <Badge tone="danger">Blocked: {d.denied_reason}</Badge>
          ) : d.requires_approval ? (
            <Badge tone="warning">Needs approval{d.allow_self_approval ? " (self-approval allowed)" : " by a second person"}</Badge>
          ) : (
            <Badge tone="success">Runs unattended</Badge>
          )}
          <span className="text-xs text-muted">rules: {d.matched_rules.length ? d.matched_rules.join(", ") : "none"} · approval valid {d.approval_ttl_minutes} min</span>
          {d.notes.map((n) => <span key={n} className="text-xs text-amber-200/80">{n}</span>)}
        </div>
      )}
    </Panel>
  );
}

export function PoliciesPage() {
  const queryClient = useQueryClient();
  const canManage = useAuthStore((s) => s.hasPermission("settings.manage"));
  const [editing, setEditing] = useState<Policy | "new" | null>(null);
  const policies = useQuery({ queryKey: ["policies"], queryFn: policiesApi.list });
  const actions = useQuery({ queryKey: ["policies", "actions"], queryFn: policiesApi.actions });
  const remove = useMutation({
    mutationFn: (id: string) => policiesApi.remove(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["policies"] }),
  });
  const toggle = useMutation({
    mutationFn: (p: Policy) => policiesApi.update(p.id, { enabled: !p.enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["policies"] }),
  });
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Remediation policies</h2>
          <p className="text-sm text-muted">
            Decide the risk of each remediation action, whether it needs a second approver, and which low-risk actions may
            run unattended in autonomous mode.
          </p>
        </div>
        {canManage && (
          <Button onClick={() => setEditing("new")}>
            <Plus /> New policy
          </Button>
        )}
      </div>
      <Alert tone="primary">
        Fixed safeguards: critical actions (such as reboots) always need a second person; outside autonomous mode every
        action needs a human approval; an approval is bound to the exact action and expires.
      </Alert>
      <Panel bodyClassName="px-0 pb-0">
        {policies.isLoading ? (
          <Spinner />
        ) : policies.error ? (
          <ErrorState message={errorMessage(policies.error)} onRetry={() => policies.refetch()} />
        ) : !policies.data?.length ? (
          <EmptyState icon={<Scale className="size-8" />} title="No policies" description="Without policies each action uses its default risk and always needs a second approver." />
        ) : (
          <Table>
            <thead>
              <tr><Th>Priority</Th><Th>Policy</Th><Th>When</Th><Th>Then</Th><Th>Status</Th>{canManage && <Th className="text-right">Actions</Th>}</tr>
            </thead>
            <tbody>
              {policies.data.map((p) => (
                <tr key={p.id}>
                  <Td className="font-mono text-xs">{p.priority}</Td>
                  <Td className="max-w-xs">
                    <div className="text-sm font-medium">{p.name}{p.builtin && <Badge className="ml-2">default</Badge>}</div>
                    {p.description && <div className="text-xs text-muted">{p.description}</div>}
                  </Td>
                  <Td className="max-w-xs text-xs text-fg/85">{summariseMatch(p.rules)}</Td>
                  <Td className="max-w-xs text-xs text-fg/85">{summariseEffect(p.rules)}</Td>
                  <Td>
                    {canManage ? (
                      <button type="button" onClick={() => toggle.mutate(p)} aria-label={p.enabled ? "Disable" : "Enable"}>
                        <Badge tone={p.enabled ? "success" : "neutral"}>{p.enabled ? "enabled" : "disabled"}</Badge>
                      </button>
                    ) : (
                      <Badge tone={p.enabled ? "success" : "neutral"}>{p.enabled ? "enabled" : "disabled"}</Badge>
                    )}
                  </Td>
                  {canManage && (
                    <Td className="text-right whitespace-nowrap">
                      <Button size="icon" variant="ghost" aria-label={`Edit ${p.name}`} onClick={() => setEditing(p)}><Pencil /></Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={`Delete ${p.name}`}
                        onClick={() => window.confirm(`Delete the policy "${p.name}"?`) && remove.mutate(p.id)}
                      >
                        <Trash2 />
                      </Button>
                    </Td>
                  )}
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>
      {(remove.error || toggle.error) && <Alert>{errorMessage(remove.error ?? toggle.error)}</Alert>}
      <PolicyPreviewPanel actions={actions.data ?? []} />
      {editing && (
        <PolicyEditor
          key={editing === "new" ? "new" : editing.id}
          open
          policy={editing === "new" ? null : editing}
          actions={actions.data ?? []}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
