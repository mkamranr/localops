import { Construction } from "lucide-react";
import { Badge, Panel } from "@/components/ui/display";
import type { NavItem } from "@/layouts/navigation";

const DESCRIPTIONS: Record<string, string> = {
  "/assistant": "Natural-language investigations with live agent activity, evidence and RCA results.",
  "/investigations": "History and live status of AI investigations with filters by status, resource and user.",
  "/incidents": "Centralised incident management with timelines, RCA, remediation tracking and related incidents.",
  "/approvals": "Review and decide on remediation actions with exact, sanitised operation details.",
  "/infrastructure": "Discovered resources with health, environment labels, metrics, logs and services.",
  "/dependencies": "Interactive dependency graph with upstream/downstream exploration and blast radius.",
  "/knowledge": "Runbooks, documents and historical incidents with semantic search.",
  "/agents": "Configure agents, model assignments, allowed tools and view execution history.",
  "/integrations": "Linux, Windows, Docker, network, HTTP and MCP server integrations.",
  "/reports": "RCA and operational reports.",
};

export function ComingSoonPage({ item }: { item: NavItem }) {
  return (
    <Panel>
      <div className="flex flex-col items-center gap-4 py-16 text-center">
        <div className="grid size-14 place-items-center rounded-2xl bg-primary/15">
          <item.icon className="size-7 text-blue-400" />
        </div>
        <div className="space-y-2">
          <h2 className="text-xl font-semibold">{item.label}</h2>
          <p className="max-w-md text-sm text-muted">{DESCRIPTIONS[item.path]}</p>
        </div>
        <Badge tone="violet">
          <Construction className="size-3.5" /> Planned for Phase {item.phase}
        </Badge>
      </div>
    </Panel>
  );
}
