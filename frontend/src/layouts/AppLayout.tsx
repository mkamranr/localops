import { useQuery } from "@tanstack/react-query";
import { Activity, LogOut, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { approvalsApi, authApi, dashboardApi } from "@/api/endpoints";
import { Alert, Badge, StatusDot } from "@/components/ui/display";
import { NAVIGATION, visibleNavigation } from "@/layouts/navigation";
import { useAuthStore } from "@/stores/authStore";
import { cn } from "@/utils/cn";

function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(t);
  }, []);
  return (
    <div className="hidden text-right leading-tight md:block">
      <div className="font-mono text-lg font-semibold tabular-nums">
        {now.toLocaleTimeString(undefined, { hour12: false })}
      </div>
      <div className="text-[11px] text-subtle">
        {now.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short", year: "numeric" })}
      </div>
    </div>
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? (parts.at(-1)?.[0] ?? "") : (parts[0]?.[1] ?? ""))).toUpperCase();
}

function currentTitle(pathname: string): string {
  if (pathname === "/") return "Dashboard";
  const all = NAVIGATION.flatMap((g) => g.items);
  const match = all.find((i) => i.path !== "/" && pathname.startsWith(i.path));
  return match?.label ?? "LocalOps";
}

export function AppLayout() {
  const user = useAuthStore((s) => s.user);
  const hasPermission = useAuthStore((s) => s.hasPermission);
  const navigate = useNavigate();
  const { pathname } = useLocation();

  const health = useQuery({
    queryKey: ["dashboard", "summary"],
    queryFn: dashboardApi.summary,
    enabled: hasPermission("dashboard.read"),
    refetchInterval: 30_000,
  });
  const approvals = useQuery({
    queryKey: ["approvals", "summary"],
    queryFn: approvalsApi.summary,
    enabled: hasPermission("approval.read"),
    refetchInterval: 20_000,
  });
  const pendingApprovals = approvals.data?.pending ?? 0;
  const services = health.data?.services ?? [];
  const healthy = services.filter((s) => s.status === "ok").length;
  const systemTone = !services.length ? "neutral" : healthy === services.length ? "success" : "danger";

  if (!user) return null;
  const groups = visibleNavigation(hasPermission);

  const logout = async () => {
    await authApi.logout();
    navigate("/login", { replace: true });
  };

  return (
    <div className="flex h-full">
      <aside className="flex w-64 shrink-0 flex-col border-r border-border bg-surface">
        <Link to="/" className="flex items-center gap-3 px-5 py-5">
          <img src="/logo-mark.png" alt="" className="size-9" />
          <div className="leading-tight">
            <div className="font-semibold tracking-tight">LocalOps</div>
            <div className="text-[11px] text-subtle">AI Infrastructure Operations</div>
          </div>
        </Link>
        <nav className="flex-1 space-y-5 overflow-y-auto px-3 pb-4" aria-label="Main">
          {groups.map((group) => (
            <div key={group.label}>
              <div className="px-3 pb-1.5 text-[11px] font-semibold tracking-wider text-subtle uppercase">
                {group.label}
              </div>
              <ul className="space-y-0.5">
                {group.items.map((item) => (
                  <li key={item.path}>
                    <NavLink
                      to={item.path}
                      end={item.path === "/"}
                      className={({ isActive }) =>
                        cn(
                          "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                          isActive
                            ? "bg-primary text-white shadow-lg shadow-primary/20"
                            : "text-muted hover:bg-surface-2 hover:text-fg",
                        )
                      }
                    >
                      <item.icon className="size-4 shrink-0" />
                      <span className="flex-1 truncate">{item.label}</span>
                      {item.path === "/approvals" && pendingApprovals > 0 && (
                        <span
                          className="rounded-full bg-amber-500 px-1.5 text-[10px] font-semibold text-black"
                          aria-label={`${pendingApprovals} pending approvals`}
                        >
                          {pendingApprovals}
                        </span>
                      )}
                      {item.phase && (
                        <span className="rounded bg-surface-3 px-1.5 text-[10px] text-subtle">
                          P{item.phase}
                        </span>
                      )}
                    </NavLink>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
        <div className="space-y-2 border-t border-border p-3">
          <div className="flex items-center gap-3 rounded-lg bg-surface-2 px-3 py-2.5">
            <div className="grid size-8 place-items-center rounded-full bg-primary/20 text-xs font-semibold text-blue-300 uppercase">
              {initials(user.display_name || user.username)}
            </div>
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate text-sm font-medium">{user.display_name || user.username}</div>
              <div className="truncate text-[11px] text-subtle">{user.roles[0] ?? "No role"}</div>
            </div>
            <button
              onClick={logout}
              className="rounded-md p-1.5 text-muted hover:bg-surface-3 hover:text-fg"
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut className="size-4" />
            </button>
          </div>
          <div className="flex items-center gap-2 px-3 text-[11px] text-subtle">
            <StatusDot tone={systemTone} />
            {services.length
              ? healthy === services.length
                ? "All systems operational"
                : `${services.length - healthy} service(s) degraded`
              : "System status"}
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-border bg-surface/80 px-6 backdrop-blur">
          <h1 className="text-lg font-semibold tracking-tight">{currentTitle(pathname)}</h1>
          <div className="flex items-center gap-3">
            <Badge tone="primary" title="Current tenant">
              <ShieldCheck className="size-3.5" /> {user.tenant.name}
            </Badge>
            {services.length > 0 && (
              <Badge tone={healthy === services.length ? "success" : "danger"} title="Platform services">
                <Activity className="size-3.5" /> Services {healthy}/{services.length}
              </Badge>
            )}
            <Clock />
          </div>
        </header>
        <main className="bg-grid flex-1 overflow-y-auto p-6">
          {user.must_change_password && !pathname.startsWith("/settings/account") && (
            <div className="mb-5">
              <Alert tone="warning">
                Your password must be changed before continuing to use LocalOps.{" "}
                <Link to="/settings/account" className="font-medium underline">
                  Change password
                </Link>
              </Alert>
            </div>
          )}
          <Outlet />
        </main>
      </div>
    </div>
  );
}
