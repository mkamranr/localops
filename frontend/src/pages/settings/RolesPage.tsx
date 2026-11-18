import { useQuery } from "@tanstack/react-query";
import { Lock, Users } from "lucide-react";
import { errorMessage } from "@/api/client";
import { rolesApi } from "@/api/endpoints";
import { Badge, ErrorState, Panel, Spinner } from "@/components/ui/display";

function groupPermissions(perms: string[]): Record<string, string[]> {
  const groups: Record<string, string[]> = {};
  for (const p of perms) {
    const area = p.split(".")[0] ?? p;
    (groups[area] ??= []).push(p.slice(area.length + 1));
  }
  return groups;
}

export function RolesPage() {
  const roles = useQuery({ queryKey: ["roles"], queryFn: rolesApi.list });

  if (roles.isLoading) return <Spinner />;
  if (roles.error) return <ErrorState message={errorMessage(roles.error)} onRetry={() => roles.refetch()} />;

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">
        Built-in roles are managed by LocalOps. Assign them to users from the Users page. Custom roles
        are planned for a later release.
      </p>
      <div className="grid gap-4 xl:grid-cols-2">
        {roles.data?.map((role) => (
          <Panel
            key={role.id}
            title={role.name}
            actions={
              <div className="flex items-center gap-2">
                {role.is_system && (
                  <Badge>
                    <Lock className="size-3" /> built-in
                  </Badge>
                )}
                <Badge tone="primary">
                  <Users className="size-3" /> {role.user_count}
                </Badge>
              </div>
            }
          >
            <p className="mb-3 text-xs text-muted">{role.description}</p>
            <div className="space-y-1.5">
              {Object.entries(groupPermissions(role.permissions)).map(([area, actions]) => (
                <div key={area} className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="w-24 font-mono text-subtle">{area}</span>
                  {actions.map((a) => (
                    <span key={a} className="rounded border border-border-strong bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-muted">
                      {a}
                    </span>
                  ))}
                </div>
              ))}
            </div>
          </Panel>
        ))}
      </div>
    </div>
  );
}
