import { Building2, KeyRound, Shield, Users } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { cn } from "@/utils/cn";

const SECTIONS = [
  { to: "account", label: "My Account", icon: KeyRound },
  { to: "users", label: "Users", icon: Users, permission: "user.read" },
  { to: "roles", label: "Roles & Permissions", icon: Shield, permission: "role.read" },
  { to: "tenants", label: "Tenants", icon: Building2, permission: "tenant.manage" },
];

export function SettingsLayout() {
  const has = useAuthStore((s) => s.hasPermission);
  return (
    <div className="flex flex-col gap-6 lg:flex-row">
      <nav className="panel h-fit w-full shrink-0 p-2 lg:w-56" aria-label="Settings">
        {SECTIONS.filter((s) => !s.permission || has(s.permission)).map((s) => (
          <NavLink
            key={s.to}
            to={s.to}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm",
                isActive ? "bg-surface-3 text-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
              )
            }
          >
            <s.icon className="size-4" /> {s.label}
          </NavLink>
        ))}
        <p className="px-3 pt-3 pb-1 text-[11px] text-subtle">
          AI models, agents, policies and integrations arrive in later phases.
        </p>
      </nav>
      <div className="min-w-0 flex-1">
        <Outlet />
      </div>
    </div>
  );
}
