import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  ClipboardCheck,
  Database,
  KeyRound,
  Search,
  Server,
  ShieldAlert,
  Users,
  type LucideIcon,
} from "lucide-react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { agentsApi, dashboardApi } from "@/api/endpoints";
import { Badge, EmptyState, ErrorState, Panel, Spinner, StatusDot } from "@/components/ui/display";
import { useAuthStore } from "@/stores/authStore";
import type { AuditLog, DashboardSummary } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatRelative } from "@/utils/format";

const iconTones = {
  violet: "bg-violet-600 shadow-violet-600/30",
  blue: "bg-blue-600 shadow-blue-600/30",
  green: "bg-emerald-600 shadow-emerald-600/30",
  red: "bg-red-600 shadow-red-600/30",
  amber: "bg-amber-600 shadow-amber-600/30",
  sky: "bg-sky-600 shadow-sky-600/30",
};

function StatCard({
  icon: Icon,
  tone,
  value,
  label,
  sub,
  to,
  phase,
}: {
  icon: LucideIcon;
  tone: keyof typeof iconTones;
  value: string | number;
  label: string;
  sub: string;
  to?: string;
  phase?: number;
}) {
  const body = (
    <div className="panel group h-full p-5 transition-colors hover:border-border-strong">
      <div className="flex items-start justify-between">
        <span className={cn("grid size-10 place-items-center rounded-xl shadow-lg", iconTones[tone])}>
          <Icon className="size-5 text-white" />
        </span>
        {phase && <Badge>Phase {phase}</Badge>}
      </div>
      <div className="mt-4 font-mono text-3xl font-semibold tabular-nums">{value}</div>
      <div className="mt-1 text-sm text-fg">{label}</div>
      <div className="text-xs text-subtle">{sub}</div>
    </div>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

function Ring({ value, total, label, color }: { value: number; total: number; label: string; color: string }) {
  const pct = total ? Math.round((value / total) * 100) : 0;
  const r = 42;
  const c = 2 * Math.PI * r;
  return (
    <div className="flex flex-col items-center gap-2">
      <div className="relative size-28">
        <svg viewBox="0 0 100 100" className="size-full -rotate-90">
          <circle cx="50" cy="50" r={r} fill="none" stroke="var(--color-surface-3)" strokeWidth="8" />
          <circle
            cx="50"
            cy="50"
            r={r}
            fill="none"
            stroke={color}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={c}
            strokeDashoffset={c - (c * pct) / 100}
            className="transition-[stroke-dashoffset] duration-700"
          />
        </svg>
        <div className="absolute inset-0 grid place-items-center text-center">
          <div>
            <div className="font-mono text-xl font-semibold">{pct}%</div>
            <div className="text-[10px] text-subtle">{label}</div>
          </div>
        </div>
      </div>
      <div className="text-xs text-muted">
        {value} / {total}
      </div>
    </div>
  );
}

function ServicesPanel({ data }: { data: DashboardSummary }) {
  const max = Math.max(50, ...data.services.map((s) => s.latency_ms ?? 0));
  return (
    <Panel title="Platform Services" actions={<Badge tone="neutral">live</Badge>}>
      <ul className="space-y-3">
        {data.services.map((s) => (
          <li key={s.name} className="space-y-1.5">
            <div className="flex items-center justify-between text-sm">
              <span className="flex items-center gap-2 capitalize">
                <StatusDot tone={s.status === "ok" ? "success" : "danger"} />
                {s.name}
              </span>
              <span className="font-mono text-xs text-muted">
                {s.status === "ok" ? `${s.latency_ms ?? "–"} ms` : (s.detail ?? "unreachable")}
              </span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-surface-3">
              <div
                className={cn("h-full rounded-full", s.status === "ok" ? "bg-success" : "bg-danger")}
                style={{
                  width: s.status === "ok" ? `${Math.max(4, ((s.latency_ms ?? 0) / max) * 100)}%` : "100%",
                }}
              />
            </div>
          </li>
        ))}
      </ul>
    </Panel>
  );
}

function ActivityPanel() {
  const query = useQuery({
    queryKey: ["dashboard", "activity"],
    queryFn: () => dashboardApi.activity(8),
    refetchInterval: 30_000,
  });
  return (
    <Panel
      title="Recent Activity"
      actions={
        <Link to="/audit" className="text-xs text-blue-400 hover:underline">
          View audit log →
        </Link>
      }
    >
      {query.isLoading ? (
        <Spinner />
      ) : query.error ? (
        <ErrorState message={errorMessage(query.error)} />
      ) : !query.data?.length ? (
        <EmptyState title="No activity yet" />
      ) : (
        <ul className="divide-y divide-border/60">
          {query.data.map((e: AuditLog) => (
            <li key={e.id} className="flex items-center gap-3 py-2.5 text-sm">
              <StatusDot tone={e.outcome === "success" ? "success" : "danger"} />
              <span className="font-mono text-xs text-blue-300">{e.action}</span>
              <span className="min-w-0 flex-1 truncate text-muted">{e.username ?? "system"}</span>
              <span className="text-xs text-subtle">{formatRelative(e.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function AgentActivityPanel({ data }: { data: DashboardSummary }) {
  const canRead = useAuthStore((s) => s.hasPermission("agent.read"));
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: agentsApi.list,
    enabled: canRead,
    refetchInterval: 30_000,
  });
  const active = (agents.data ?? []).filter((a) => a.stats.executions_24h > 0);
  const success = data.agent_executions_24h
    ? Math.round(((data.agent_executions_24h - data.agent_failures_24h) / data.agent_executions_24h) * 100)
    : null;
  return (
    <Panel
      title="Agent Activity"
      actions={
        canRead ? (
          <Link to="/agents" className="text-xs text-blue-400 hover:underline">
            Agents →
          </Link>
        ) : undefined
      }
    >
      <div className="grid grid-cols-3 gap-2 text-center">
        <div className="rounded-lg border border-border bg-surface-2 p-2">
          <div className="font-mono text-xl font-semibold tabular-nums">{data.agent_executions_24h}</div>
          <div className="text-[11px] text-muted">runs (24h)</div>
        </div>
        <div className="rounded-lg border border-border bg-surface-2 p-2">
          <div className={cn("font-mono text-xl font-semibold tabular-nums", success != null && success < 90 && "text-amber-300")}>
            {success != null ? `${success}%` : "—"}
          </div>
          <div className="text-[11px] text-muted">success</div>
        </div>
        <div className="rounded-lg border border-border bg-surface-2 p-2">
          <div className="font-mono text-xl font-semibold tabular-nums">{data.agent_failures_24h}</div>
          <div className="text-[11px] text-muted">failures</div>
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2 text-xs text-muted">
        <Bot className="size-3.5" /> Default model:{" "}
        {data.default_model ? (
          <span className="font-mono text-fg">{data.default_model}</span>
        ) : (
          <Link to="/settings/models" className="text-amber-300 hover:underline">
            not configured
          </Link>
        )}
      </div>
      {canRead && (
        <ul className="mt-3 divide-y divide-border/60">
          {active.length === 0 ? (
            <li className="py-3 text-xs text-subtle">No agent runs in the last 24 hours.</li>
          ) : (
            active.map((a) => (
              <li key={a.id} className="flex items-center gap-3 py-2 text-sm">
                <StatusDot tone={a.stats.failures_24h ? "warning" : "success"} />
                <span className="min-w-0 flex-1 truncate">{a.name}</span>
                <span className="font-mono text-xs text-muted">{a.stats.executions_24h} runs</span>
                <span className="text-xs text-subtle">{formatRelative(a.stats.last_run_at)}</span>
              </li>
            ))
          )}
        </ul>
      )}
    </Panel>
  );
}

export function DashboardPage() {
  const canAudit = useAuthStore((s) => s.hasPermission("audit.read"));
  const query = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: dashboardApi.summary,
    refetchInterval: 30_000,
  });

  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data)
    return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const d = query.data;
  const healthyServices = d.services.filter((s) => s.status === "ok").length;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={Server} tone="violet" value={`${d.resources.online}/${d.resources.total}`} label="Infrastructure resources" sub="online / total" />
        <StatCard icon={ShieldAlert} tone="red" value={d.incidents.open_total} label="Active incidents" sub={`critical ${d.incidents.critical} · high ${d.incidents.high}`} />
        <StatCard icon={Search} tone="blue" value={d.active_investigations} label="Active investigations" sub={`${d.investigations_24h} started in the last 24h`} to="/investigations" />
        <StatCard icon={ClipboardCheck} tone="amber" value={d.pending_approvals} label="Pending approvals" sub="awaiting decision" phase={5} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <Panel title="Platform Health" className="xl:col-span-1">
          <div className="flex justify-around py-2">
            <Ring value={healthyServices} total={d.services.length} label="services" color="var(--color-success)" />
            <Ring value={d.users.active} total={d.users.total} label="active users" color="var(--color-primary)" />
          </div>
        </Panel>
        <Panel title="Access (24h)" className="xl:col-span-1">
          <div className="grid grid-cols-2 gap-3">
            {[
              { label: "Successful logins", value: d.activity.logins_24h, icon: KeyRound, tone: "text-green-400" },
              { label: "Failed logins", value: d.activity.failed_logins_24h, icon: KeyRound, tone: "text-red-400" },
              { label: "Audit events", value: d.activity.events_24h, icon: Database, tone: "text-blue-400" },
              { label: "Users", value: `${d.users.active}/${d.users.total}`, icon: Users, tone: "text-violet-400" },
            ].map((m) => (
              <div key={m.label} className="rounded-lg border border-border bg-surface-2 p-3">
                <m.icon className={cn("size-4", m.tone)} />
                <div className="mt-2 font-mono text-2xl font-semibold tabular-nums">{m.value}</div>
                <div className="text-xs text-muted">{m.label}</div>
              </div>
            ))}
          </div>
        </Panel>
        <ServicesPanel data={d} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2">
          {canAudit ? (
            <ActivityPanel />
          ) : (
            <Panel title="Recent Activity">
              <EmptyState title="Audit access required" description="Ask an administrator for the audit.read permission." />
            </Panel>
          )}
        </div>
        <AgentActivityPanel data={d} />
      </div>
    </div>
  );
}
