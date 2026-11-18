import {
  Bot,
  BookOpen,
  ClipboardCheck,
  FileBarChart,
  LayoutDashboard,
  Network,
  Plug,
  ScrollText,
  Search,
  Server,
  Settings,
  ShieldAlert,
  Sparkles,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  label: string;
  path: string;
  icon: LucideIcon;
  /** Permission required to see the item; omitted means any signed-in user. */
  permission?: string;
  /** Roadmap phase delivering the screen; undefined means available now. */
  phase?: number;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

// Spec §12.1: 13 sidebar entries, grouped for scanning.
export const NAVIGATION: NavGroup[] = [
  {
    label: "Overview",
    items: [
      { label: "Dashboard", path: "/", icon: LayoutDashboard, permission: "dashboard.read" },
      { label: "AI Assistant", path: "/assistant", icon: Sparkles, permission: "incident.investigate", phase: 2 },
    ],
  },
  {
    label: "Operations",
    items: [
      { label: "Investigations", path: "/investigations", icon: Search, permission: "incident.read", phase: 2 },
      { label: "Incidents", path: "/incidents", icon: ShieldAlert, permission: "incident.read", phase: 4 },
      { label: "Approvals", path: "/approvals", icon: ClipboardCheck, permission: "approval.read", phase: 5 },
    ],
  },
  {
    label: "Infrastructure",
    items: [
      { label: "Infrastructure", path: "/infrastructure", icon: Server, permission: "resource.read", phase: 3 },
      { label: "Dependencies", path: "/dependencies", icon: Network, permission: "resource.read", phase: 3 },
    ],
  },
  {
    label: "Intelligence",
    items: [
      { label: "Knowledge Base", path: "/knowledge", icon: BookOpen, permission: "knowledge.read", phase: 6 },
      { label: "Agents", path: "/agents", icon: Bot, permission: "agent.read", phase: 2 },
      { label: "Tools & Integrations", path: "/integrations", icon: Plug, permission: "integration.read", phase: 6 },
    ],
  },
  {
    label: "Governance",
    items: [
      { label: "Reports", path: "/reports", icon: FileBarChart, permission: "incident.read", phase: 4 },
      { label: "Audit Logs", path: "/audit", icon: ScrollText, permission: "audit.read" },
      { label: "Settings", path: "/settings", icon: Settings },
    ],
  },
];

export function visibleNavigation(has: (permission: string) => boolean): NavGroup[] {
  return NAVIGATION.map((g) => ({
    ...g,
    items: g.items.filter((i) => !i.permission || has(i.permission)),
  })).filter((g) => g.items.length > 0);
}

export const COMING_SOON = NAVIGATION.flatMap((g) => g.items).filter((i) => i.phase);
