import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box,
  ChevronDown,
  ChevronRight,
  Globe,
  KeyRound,
  Monitor,
  Pencil,
  Play,
  Plug,
  Plus,
  Radar,
  Server,
  Trash2,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { Fragment, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { integrationsApi, type IntegrationInput } from "@/api/endpoints";
import { formatDuration } from "@/components/investigation/shared";
import { INTEGRATION_META } from "@/components/inventory/shared";
import { formatTargets, parseTargets } from "@/components/inventory/targets";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Spinner, StatusDot, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { DiscoveryRun, Integration, IntegrationTestResult, IntegrationType } from "@/types/api";
import { formatDateTime, formatRelative } from "@/utils/format";

const TYPE_ICONS: Record<IntegrationType, LucideIcon> = {
  linux_ssh: Server,
  windows_winrm: Monitor,
  docker: Box,
  http: Globe,
};

const RUN_TONE = {
  completed: "success",
  partial: "warning",
  failed: "danger",
  queued: "neutral",
  running: "primary",
} as const;

const SCHEDULES = [
  { value: "", label: "Manual only" },
  { value: "15", label: "Every 15 minutes" },
  { value: "30", label: "Every 30 minutes" },
  { value: "60", label: "Hourly" },
  { value: "360", label: "Every 6 hours" },
  { value: "1440", label: "Daily" },
];

function usesSsh(type: IntegrationType, mode: string) {
  return type === "linux_ssh" || (type === "docker" && mode !== "tls");
}

// ------------------------------------------------------------------ dialog

function IntegrationDialog({
  integration,
  initialType,
  onClose,
}: {
  integration: Integration | null;
  initialType: IntegrationType;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const cfg = integration?.configuration ?? {};
  const [type, setType] = useState<IntegrationType>(integration?.integration_type ?? initialType);
  const [form, setForm] = useState(() => ({
    name: integration?.name ?? "",
    description: integration?.description ?? "",
    targets: formatTargets(integration?.targets ?? []),
    username: cfg.username ?? "",
    port: cfg.port ? String(cfg.port) : "",
    timeout: String(cfg.timeout_seconds ?? 20),
    docker: cfg.docker ?? "auto",
    mode: cfg.mode ?? "ssh",
    verify_hostname: cfg.verify_hostname ?? true,
    use_https: cfg.use_https ?? false,
    transport: cfg.transport ?? "ntlm",
    verify_tls: cfg.verify_tls ?? true,
    expected_status: (cfg.expected_status ?? []).join(", "),
    slow_ms: String(cfg.slow_ms ?? 2000),
    auth: integration?.credential_fields.includes("private_key") ? "key" : "password",
    password: "",
    private_key: "",
    passphrase: "",
    ca: "",
    cert: "",
    key: "",
    clear_credentials: false,
    schedule: integration?.schedule_minutes ? String(integration.schedule_minutes) : "",
    enabled: integration?.enabled ?? true,
  }));
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const check = (k: keyof typeof form) => (e: { target: { checked: boolean } }) =>
    setForm((f) => ({ ...f, [k]: e.target.checked }));
  const parsed = parseTargets(form.targets, type);
  const ssh = usesSsh(type, form.mode);
  const tls = type === "docker" && form.mode === "tls";
  const stored = integration?.credential_fields ?? [];

  const mutation = useMutation({
    mutationFn: () => {
      const credentials: Record<string, string> = {};
      const put = (k: string, v: string) => v.trim() && (credentials[k] = v);
      if (ssh) {
        if (form.auth === "password") put("password", form.password);
        else {
          put("private_key", form.private_key);
          put("passphrase", form.passphrase);
        }
      } else if (type === "windows_winrm") put("password", form.password);
      else if (tls) {
        put("ca", form.ca);
        put("cert", form.cert);
        put("key", form.key);
      }
      const body: IntegrationInput = {
        name: form.name.trim(),
        integration_type: type,
        description: form.description,
        targets: parsed.targets,
        enabled: form.enabled,
        configuration: {
          username: form.username.trim() || null,
          port: form.port ? Number(form.port) : null,
          timeout_seconds: Number(form.timeout) || 20,
          docker: form.docker as "auto" | "off",
          mode: form.mode as "ssh" | "tls",
          verify_hostname: form.verify_hostname,
          use_https: form.use_https,
          transport: form.transport as "ntlm" | "basic",
          verify_tls: form.verify_tls,
          expected_status: form.expected_status
            .split(/[,\s]+/)
            .filter(Boolean)
            .map(Number),
          slow_ms: Number(form.slow_ms) || 2000,
        },
      };
      if (Object.keys(credentials).length) body.credentials = credentials;
      if (integration && form.clear_credentials) body.clear_credentials = true;
      if (form.schedule) body.schedule_minutes = Number(form.schedule);
      else if (integration?.schedule_minutes) body.clear_schedule = true;
      return integration ? integrationsApi.update(integration.id, body) : integrationsApi.create(body);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["integrations"] });
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!parsed.errors.length) mutation.mutate();
  };
  const keep = (field: string) => (stored.includes(field) ? "Stored (encrypted). Leave blank to keep." : undefined);

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={integration ? `Edit ${integration.name}` : `Add ${INTEGRATION_META[type].label} integration`}
      description="Credentials are encrypted with LOCALOPS_ENCRYPTION_KEY, never shown again and never sent to the AI model."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="integration-form" loading={mutation.isPending} disabled={!!parsed.errors.length}>
            {integration ? "Save" : "Add integration"}
          </Button>
        </>
      }
    >
      <form id="integration-form" onSubmit={submit} className="space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Name" htmlFor="i-name">
            <Input id="i-name" value={form.name} onChange={set("name")} required maxLength={100} placeholder="Production Linux" />
          </Field>
          <Field label="Type" htmlFor="i-type">
            <Select id="i-type" value={type} onChange={(e) => setType(e.target.value as IntegrationType)} disabled={!!integration}>
              {(Object.keys(INTEGRATION_META) as IntegrationType[]).map((t) => (
                <option key={t} value={t}>{INTEGRATION_META[t].label}</option>
              ))}
            </Select>
          </Field>
        </div>

        {type === "docker" && (
          <Field label="Connection" htmlFor="i-mode" hint={form.mode === "tls" ? "dockerd must listen with --tlsverify (usually port 2376)." : "Runs the docker CLI over SSH. The user must be in the docker group (root-equivalent)."}>
            <Select id="i-mode" value={form.mode} onChange={set("mode")}>
              <option value="ssh">SSH (recommended)</option>
              <option value="tls">Engine API over TLS</option>
            </Select>
          </Field>
        )}

        <Field
          label="Targets"
          htmlFor="i-targets"
          hint={type === "http" ? "One URL per line, optionally followed by a display name." : "One per line: host[:port] [display name]. Hostnames or IP addresses; no ranges."}
        >
          <Textarea
            id="i-targets"
            value={form.targets}
            onChange={set("targets")}
            rows={4}
            className="font-mono"
            placeholder={type === "http" ? "https://intranet.corp.local/health Intranet" : "10.0.0.11 app-01\ndb-01.corp.local:2222"}
          />
        </Field>
        {!!parsed.errors.length && <Alert tone="warning">{parsed.errors.join(" · ")}</Alert>}

        {(ssh || type === "windows_winrm") && (
          <div className="grid grid-cols-3 gap-4">
            <div className="col-span-2">
              <Field label="Username" htmlFor="i-user">
                <Input id="i-user" value={form.username} onChange={set("username")} autoComplete="off" placeholder={type === "windows_winrm" ? "CORP\\svc-localops" : "localops"} />
              </Field>
            </div>
            <Field label="Port" htmlFor="i-port">
              <Input id="i-port" type="number" min={1} max={65535} value={form.port} onChange={set("port")} placeholder={ssh ? "22" : form.use_https ? "5986" : "5985"} />
            </Field>
          </div>
        )}

        {ssh && (
          <>
            <Field label="Authentication" htmlFor="i-auth">
              <Select id="i-auth" value={form.auth} onChange={set("auth")}>
                <option value="password">Password</option>
                <option value="key">Private key</option>
              </Select>
            </Field>
            {form.auth === "password" ? (
              <Field label="Password" htmlFor="i-pass" hint={keep("password")}>
                <Input id="i-pass" type="password" value={form.password} onChange={set("password")} autoComplete="new-password" />
              </Field>
            ) : (
              <>
                <Field label="Private key (OpenSSH or PEM)" htmlFor="i-key" hint={keep("private_key")}>
                  <Textarea id="i-key" value={form.private_key} onChange={set("private_key")} rows={4} className="font-mono text-xs" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" />
                </Field>
                <Field label="Key passphrase (optional)" htmlFor="i-pp" hint={keep("passphrase")}>
                  <Input id="i-pp" type="password" value={form.passphrase} onChange={set("passphrase")} autoComplete="new-password" />
                </Field>
              </>
            )}
            {type === "linux_ssh" && (
              <Field label="Docker containers" htmlFor="i-docker">
                <Select id="i-docker" value={form.docker} onChange={set("docker")}>
                  <option value="auto">Collect when the docker CLI is usable</option>
                  <option value="off">Do not collect</option>
                </Select>
              </Field>
            )}
          </>
        )}

        {type === "windows_winrm" && (
          <>
            <Field label="Password" htmlFor="i-wpass" hint={keep("password")}>
              <Input id="i-wpass" type="password" value={form.password} onChange={set("password")} autoComplete="new-password" />
            </Field>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Authentication" htmlFor="i-transport" hint="Basic is only allowed over HTTPS.">
                <Select id="i-transport" value={form.transport} onChange={set("transport")}>
                  <option value="ntlm">NTLM</option>
                  <option value="basic">Basic (HTTPS only)</option>
                </Select>
              </Field>
              <div className="space-y-2 pt-6 text-sm">
                <label className="flex items-center gap-2"><input type="checkbox" checked={form.use_https} onChange={check("use_https")} className="size-4" /> Use HTTPS (5986)</label>
                <label className="flex items-center gap-2"><input type="checkbox" checked={form.verify_tls} onChange={check("verify_tls")} className="size-4" /> Verify certificate</label>
              </div>
            </div>
          </>
        )}

        {tls && (
          <>
            <Field label="Port" htmlFor="i-tport">
              <Input id="i-tport" type="number" value={form.port} onChange={set("port")} placeholder="2376" />
            </Field>
            {(["ca", "cert", "key"] as const).map((k) => (
              <Field key={k} label={{ ca: "CA certificate", cert: "Client certificate", key: "Client key" }[k]} htmlFor={`i-${k}`} hint={keep(k)}>
                <Textarea id={`i-${k}`} value={form[k]} onChange={set(k)} rows={3} className="font-mono text-xs" placeholder="-----BEGIN ..." />
              </Field>
            ))}
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.verify_hostname} onChange={check("verify_hostname")} className="size-4" /> Verify the server hostname</label>
          </>
        )}

        {type === "http" && (
          <div className="grid grid-cols-2 gap-4">
            <Field label="Expected status codes" htmlFor="i-status" hint="Blank means any 2xx or 3xx.">
              <Input id="i-status" value={form.expected_status} onChange={set("expected_status")} placeholder="200, 204" />
            </Field>
            <Field label="Slow above (ms)" htmlFor="i-slow">
              <Input id="i-slow" type="number" min={50} value={form.slow_ms} onChange={set("slow_ms")} />
            </Field>
            <label className="col-span-2 flex items-center gap-2 text-sm"><input type="checkbox" checked={form.verify_tls} onChange={check("verify_tls")} className="size-4" /> Verify TLS certificates</label>
          </div>
        )}

        <div className="grid grid-cols-2 gap-4">
          <Field label="Discovery schedule" htmlFor="i-sched">
            <Select id="i-sched" value={form.schedule} onChange={set("schedule")}>
              {SCHEDULES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
            </Select>
          </Field>
          <Field label="Timeout per target (s)" htmlFor="i-timeout">
            <Input id="i-timeout" type="number" min={3} max={300} value={form.timeout} onChange={set("timeout")} />
          </Field>
        </div>
        <Field label="Description" htmlFor="i-desc">
          <Input id="i-desc" value={form.description} onChange={set("description")} maxLength={1000} />
        </Field>
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" checked={form.enabled} onChange={check("enabled")} className="size-4" /> Enabled</label>
          {integration?.has_credentials && (
            <label className="flex items-center gap-2"><input type="checkbox" checked={form.clear_credentials} onChange={check("clear_credentials")} className="size-4" /> Remove stored credentials</label>
          )}
        </div>
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ runs & keys

function RunSummary({ run }: { run: DiscoveryRun }) {
  const s = run.stats;
  return (
    <span className="text-xs text-muted">
      {s.succeeded ?? 0}/{s.targets ?? 0} targets · +{s.added ?? 0} new · {s.updated ?? 0} changed
      {s.stale ? ` · ${s.stale} stale` : ""}
      {s.relationships_added ? ` · ${s.relationships_added} links` : ""}
    </span>
  );
}

function RunsTable({ integrationId }: { integrationId: string }) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["integrations", integrationId, "runs"],
    queryFn: () => integrationsApi.runs(integrationId),
    refetchInterval: (q) => (q.state.data?.items.some((r) => r.status === "queued" || r.status === "running") ? 2000 : false),
  });
  const [open, setOpen] = useState<string | null>(null);
  // Each time the run list settles (nothing queued or running), the integration's
  // counters and the inventory may have changed.
  const active = query.data?.items.some((r) => r.status === "queued" || r.status === "running") ?? false;
  const updatedAt = query.dataUpdatedAt;
  useEffect(() => {
    if (!updatedAt || active) return;
    void queryClient.invalidateQueries({ queryKey: ["integrations"], exact: true });
    void queryClient.invalidateQueries({ queryKey: ["resources"] });
  }, [updatedAt, active, queryClient]);
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} />;
  if (!query.data.items.length) return <p className="py-3 text-xs text-subtle">No discovery runs yet.</p>;
  return (
    <Table>
      <thead>
        <tr><Th>Started</Th><Th>Trigger</Th><Th>Status</Th><Th>Result</Th><Th /></tr>
      </thead>
      <tbody>
        {query.data.items.map((run) => (
          <Fragment key={run.id}>
            <tr>
              <Td className="whitespace-nowrap text-xs">{formatDateTime(run.started_at)}</Td>
              <Td className="text-xs capitalize">{run.trigger}</Td>
              <Td><Badge tone={RUN_TONE[run.status]}>{run.status}</Badge></Td>
              <Td><RunSummary run={run} /></Td>
              <Td className="text-right">
                {!!run.target_results.length && (
                  <Button variant="ghost" size="sm" onClick={() => setOpen(open === run.id ? null : run.id)}>
                    {open === run.id ? <ChevronDown /> : <ChevronRight />} Targets
                  </Button>
                )}
              </Td>
            </tr>
            {open === run.id && (
              <tr>
                <Td colSpan={5} className="bg-surface-2/60">
                  <ul className="space-y-1.5 text-xs">
                    {run.target_results.map((t) => (
                      <li key={t.target} className="flex flex-wrap items-center gap-2">
                        <StatusDot tone={t.ok ? "success" : "danger"} />
                        <span className="font-mono">{t.target}</span>
                        <span className="text-muted">{t.ok ? `${t.resources} resources in ${formatDuration(t.duration_ms)}` : t.error}</span>
                        {t.host_key_pinned_now && <Badge tone="primary">host key pinned</Badge>}
                        {t.error_code === "host_key_changed" && <Badge tone="danger">host key changed</Badge>}
                      </li>
                    ))}
                  </ul>
                </Td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </Table>
  );
}

function HostKeys({ integration, canManage }: { integration: Integration; canManage: boolean }) {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["integrations", integration.id, "keys"], queryFn: () => integrationsApi.hostKeys(integration.id) });
  const reset = useMutation({
    mutationFn: (keyId: string) => integrationsApi.resetHostKey(integration.id, keyId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["integrations", integration.id, "keys"] }),
  });
  if (!query.data?.length) return <p className="py-3 text-xs text-subtle">No host keys pinned yet. Keys are pinned on first successful connection.</p>;
  return (
    <ul className="space-y-2 text-xs">
      {query.data.map((k) => (
        <li key={k.id} className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-surface-2 px-3 py-2">
          <KeyRound className="size-3.5 text-amber-300" />
          <span className="font-mono">{k.host}:{k.port}</span>
          <span className="font-mono text-muted">{k.key_type} {k.fingerprint}</span>
          <span className="text-subtle">pinned {formatRelative(k.first_seen_at)}</span>
          {canManage && (
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              loading={reset.isPending}
              onClick={() => window.confirm(`Forget the pinned key for ${k.host}:${k.port}? The next connection will trust whatever key the host presents. Only do this after verifying the host's new key out of band.`) && reset.mutate(k.id)}
            >
              Reset pin
            </Button>
          )}
        </li>
      ))}
      {reset.error && <Alert>{errorMessage(reset.error)}</Alert>}
    </ul>
  );
}

// ------------------------------------------------------------------ card

function IntegrationCard({ integration, canManage, onEdit }: { integration: Integration; canManage: boolean; onEdit: () => void }) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<"runs" | "keys" | null>(null);
  const [result, setResult] = useState<IntegrationTestResult | null>(null);
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["integrations"] });
    void queryClient.invalidateQueries({ queryKey: ["resources"] });
  };
  const test = useMutation({ mutationFn: () => integrationsApi.test(integration.id), onSuccess: (r) => { setResult(r); refresh(); } });
  const discover = useMutation({ mutationFn: () => integrationsApi.discover(integration.id), onSuccess: () => { setTab("runs"); refresh(); } });
  const toggle = useMutation({ mutationFn: () => integrationsApi.update(integration.id, { enabled: !integration.enabled }), onSuccess: refresh });
  const remove = useMutation({ mutationFn: () => integrationsApi.remove(integration.id), onSuccess: refresh });
  const Icon = TYPE_ICONS[integration.integration_type];
  const ssh = usesSsh(integration.integration_type, integration.configuration.mode ?? "ssh");
  const error = test.error ?? discover.error ?? toggle.error ?? remove.error;

  return (
    <section className="panel p-5">
      <div className="flex flex-wrap items-start gap-4">
        <span className="grid size-10 place-items-center rounded-xl border border-border bg-surface-2">
          <Icon className="size-5 text-blue-400" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{integration.name}</h3>
            <Badge>{INTEGRATION_META[integration.integration_type].label}{integration.integration_type === "docker" ? ` · ${integration.configuration.mode ?? "ssh"}` : ""}</Badge>
            {!integration.enabled && <Badge tone="warning">disabled</Badge>}
            {integration.last_status && <Badge tone={RUN_TONE[integration.last_status]}>last run {integration.last_status}</Badge>}
          </div>
          <p className="mt-0.5 truncate text-xs text-muted">
            {integration.description || integration.targets.map((t) => t.name || t.host).slice(0, 6).join(", ") || "No targets yet"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canManage && (
            <>
              <Button variant="secondary" size="sm" onClick={() => { setResult(null); test.mutate(); }} loading={test.isPending} disabled={!integration.targets.length}>
                <Zap /> Test
              </Button>
              <Button size="sm" onClick={() => discover.mutate()} loading={discover.isPending} disabled={!integration.enabled || !integration.targets.length}>
                <Play /> Discover now
              </Button>
              <Button variant="ghost" size="sm" onClick={() => toggle.mutate()}>{integration.enabled ? "Disable" : "Enable"}</Button>
              <Button variant="ghost" size="icon" aria-label="Edit" onClick={onEdit}><Pencil /></Button>
              <Button variant="ghost" size="icon" aria-label="Delete" onClick={() => window.confirm(`Delete integration "${integration.name}"? Discovered resources stay in the inventory.`) && remove.mutate()}>
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      </div>

      <dl className="mt-5 grid grid-cols-2 gap-4 border-t border-border pt-4 text-sm md:grid-cols-5">
        <div><dt className="text-xs text-muted">Targets</dt><dd className="mt-0.5">{integration.targets.length}</dd></div>
        <div>
          <dt className="text-xs text-muted">Resources</dt>
          <dd className="mt-0.5"><Link className="text-blue-300 hover:underline" to={`/infrastructure?integration_id=${integration.id}`}>{integration.resource_count}</Link></dd>
        </div>
        <div><dt className="text-xs text-muted">Schedule</dt><dd className="mt-0.5">{SCHEDULES.find((s) => s.value === String(integration.schedule_minutes ?? ""))?.label ?? `Every ${integration.schedule_minutes} min`}</dd></div>
        <div><dt className="text-xs text-muted">Last run</dt><dd className="mt-0.5">{formatRelative(integration.last_run_at)}</dd></div>
        <div>
          <dt className="text-xs text-muted">Credentials</dt>
          <dd className="mt-0.5 text-xs">{integration.has_credentials ? integration.credential_fields.join(", ").replace(/_/g, " ") : "none"}{integration.configuration.username ? ` · ${integration.configuration.username}` : ""}</dd>
        </div>
      </dl>

      {integration.last_error && integration.last_status !== "completed" && !result && (
        <div className="mt-3"><Alert tone="warning">{integration.last_error}</Alert></div>
      )}
      {error && <div className="mt-3"><Alert>{errorMessage(error)}</Alert></div>}
      {result && (
        <div className="mt-3">
          <Alert tone={result.ok ? "success" : "danger"}>
            <span className="font-mono">{result.target}</span>: {result.message}
            {typeof result.details.host_key_fingerprint === "string" && (
              <span className="mt-1 block text-xs">
                Host key {result.details.host_key_pinned_now ? "pinned now" : "matches pin"}: <span className="font-mono">{result.details.host_key_fingerprint}</span>
              </span>
            )}
            {typeof result.details.docker === "string" && <span className="mt-1 block text-xs">Docker: {result.details.docker}</span>}
          </Alert>
        </div>
      )}

      <div className="mt-4 flex gap-1 border-b border-border text-xs">
        {(["runs", ...(ssh ? (["keys"] as const) : [])] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(tab === t ? null : t)}
            className={`-mb-px border-b-2 px-3 py-2 ${tab === t ? "border-primary text-fg" : "border-transparent text-muted hover:text-fg"}`}
          >
            {t === "runs" ? "Discovery runs" : "Pinned host keys"}
          </button>
        ))}
      </div>
      {tab === "runs" && <div className="pt-2"><RunsTable integrationId={integration.id} /></div>}
      {tab === "keys" && <div className="pt-3"><HostKeys integration={integration} canManage={canManage} /></div>}
    </section>
  );
}

// ------------------------------------------------------------------ page

export function IntegrationsPage() {
  const canManage = useAuthStore((s) => s.hasPermission("integration.configure"));
  const query = useQuery({ queryKey: ["integrations"], queryFn: integrationsApi.list });
  const [dialog, setDialog] = useState<{ integration: Integration | null; type: IntegrationType } | null>(null);
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const integrations = query.data;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Tools &amp; Integrations</h1>
          <p className="text-sm text-muted">Read-only connections LocalOps uses to discover infrastructure. Only fixed, read-only commands run on targets.</p>
        </div>
        {canManage && <Button onClick={() => setDialog({ integration: null, type: "linux_ssh" })}><Plus /> Add integration</Button>}
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {(Object.keys(INTEGRATION_META) as IntegrationType[]).map((type) => {
          const Icon = TYPE_ICONS[type];
          const mine = integrations.filter((i) => i.integration_type === type);
          return (
            <button
              key={type}
              type="button"
              disabled={!canManage}
              onClick={() => setDialog({ integration: null, type })}
              className="panel flex items-start gap-3 p-4 text-left transition-colors hover:border-border-strong disabled:cursor-default"
            >
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary/15 text-blue-300"><Icon className="size-4" /></span>
              <span className="min-w-0">
                <span className="block text-sm font-medium">{INTEGRATION_META[type].label}</span>
                <span className="mt-0.5 block text-xs text-muted">{INTEGRATION_META[type].description}</span>
                <span className="mt-2 block text-xs text-subtle">{mine.length ? `${mine.length} configured · ${mine.reduce((n, i) => n + i.resource_count, 0)} resources` : "Not configured"}</span>
              </span>
            </button>
          );
        })}
      </div>
      <p className="flex items-center gap-2 text-xs text-subtle">
        <Radar className="size-3.5" /> Network devices (SNMP), monitoring systems and MCP tool servers arrive in later phases.
      </p>

      {!integrations.length ? (
        <div className="panel">
          <EmptyState icon={<Plug className="size-8" />} title="No integrations yet" description="Add a Linux, Windows, Docker or HTTP integration, test the connection, then run discovery to populate the inventory." />
        </div>
      ) : (
        integrations.map((i) => (
          <IntegrationCard key={i.id} integration={i} canManage={canManage} onEdit={() => setDialog({ integration: i, type: i.integration_type })} />
        ))
      )}
      {dialog && (
        <IntegrationDialog key={dialog.integration?.id ?? dialog.type} integration={dialog.integration} initialType={dialog.type} onClose={() => setDialog(null)} />
      )}
    </div>
  );
}
