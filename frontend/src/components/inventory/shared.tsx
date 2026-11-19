import {
  AppWindow,
  Box,
  Container,
  Database,
  Globe,
  HardDrive,
  Monitor,
  Router,
  Server,
  type LucideIcon,
} from "lucide-react";
import { Badge, StatusDot, type Tone } from "@/components/ui/display";
import type { HealthStatus, IntegrationType, RelationshipType, ResourceType } from "@/types/api";

export const HEALTH_META: Record<
  HealthStatus,
  { label: string; tone: Tone; dot: "success" | "warning" | "danger" | "neutral"; color: string }
> = {
  healthy: { label: "Healthy", tone: "success", dot: "success", color: "#22c55e" },
  degraded: { label: "Degraded", tone: "warning", dot: "warning", color: "#f59e0b" },
  unhealthy: { label: "Unhealthy", tone: "danger", dot: "danger", color: "#ef4444" },
  unknown: { label: "Unknown", tone: "neutral", dot: "neutral", color: "#5d6c8c" },
};

export const TYPE_META: Record<ResourceType, { label: string; icon: LucideIcon }> = {
  linux_server: { label: "Linux server", icon: Server },
  windows_server: { label: "Windows server", icon: Monitor },
  docker_host: { label: "Docker host", icon: Box },
  docker_container: { label: "Container", icon: Container },
  http_service: { label: "HTTP service", icon: Globe },
  application: { label: "Application", icon: AppWindow },
  database: { label: "Database", icon: Database },
  network_device: { label: "Network device", icon: Router },
  other: { label: "Other", icon: HardDrive },
};

export const RESOURCE_TYPES = Object.keys(TYPE_META) as ResourceType[];

export const RELATIONSHIP_META: Record<RelationshipType, { label: string; hint: string }> = {
  DEPENDS_ON: { label: "depends on", hint: "Source needs the target to work" },
  RUNS_ON: { label: "runs on", hint: "Source is hosted by the target" },
  CONNECTS_TO: { label: "connects to", hint: "Source opens connections to the target" },
  STORES_DATA_IN: { label: "stores data in", hint: "Source keeps its data in the target" },
  HOSTS: { label: "hosts", hint: "Source hosts the target" },
  EXPOSES: { label: "exposes", hint: "Source publishes the target" },
  MONITORED_BY: { label: "monitored by", hint: "Not a dependency" },
  MANAGED_BY: { label: "managed by", hint: "Not a dependency" },
};

export const INTEGRATION_META: Record<IntegrationType, { label: string; description: string }> = {
  linux_ssh: {
    label: "Linux (SSH)",
    description: "Hosts, services, ports, disks and connections; Docker containers when available.",
  },
  windows_winrm: {
    label: "Windows (WinRM)",
    description: "Windows servers over WinRM: OS, services, disks, listening ports.",
  },
  docker: {
    label: "Docker",
    description: "Docker hosts and containers, via SSH (default) or the TLS Engine API.",
  },
  http: {
    label: "HTTP checks",
    description: "Status, latency and certificate expiry of web endpoints.",
  },
};

export function HealthBadge({ status, stale }: { status: HealthStatus; stale?: boolean }) {
  const meta = HEALTH_META[status] ?? HEALTH_META.unknown;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Badge tone={meta.tone}>
        <StatusDot tone={meta.dot} /> {meta.label}
      </Badge>
      {stale && <Badge title="Not found in the latest discovery">stale</Badge>}
    </span>
  );
}

export function TypeIcon({ type, className }: { type: ResourceType; className?: string }) {
  const Icon = TYPE_META[type]?.icon ?? HardDrive;
  return <Icon className={className ?? "size-4 text-muted"} />;
}

export function typeLabel(type: string): string {
  return TYPE_META[type as ResourceType]?.label ?? type;
}

/** Tiny SVG line chart; `values` may contain nulls (gaps are skipped). */
export function Sparkline({
  values,
  max,
  color = "#3b82f6",
  width = 220,
  height = 44,
}: {
  values: (number | null)[];
  max?: number;
  color?: string;
  width?: number;
  height?: number;
}) {
  const points = values
    .map((v, i) => (v == null ? null : ([i, v] as const)))
    .filter((p): p is readonly [number, number] => p !== null);
  if (points.length < 2) {
    return <div className="flex h-11 items-center text-xs text-subtle">Not enough samples yet</div>;
  }
  const top = max ?? Math.max(...points.map(([, v]) => v), 1);
  const step = width / Math.max(values.length - 1, 1);
  const path = points
    .map(([i, v], n) => `${n ? "L" : "M"}${(i * step).toFixed(1)},${(height - (v / top) * (height - 4) - 2).toFixed(1)}`)
    .join(" ");
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="h-11" role="img" aria-label="trend">
      <path d={path} fill="none" stroke={color} strokeWidth={1.75} vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

export function percentTone(value: number | null | undefined): string {
  if (value == null) return "text-muted";
  if (value >= 90) return "text-red-300";
  if (value >= 75) return "text-amber-300";
  return "text-green-300";
}
