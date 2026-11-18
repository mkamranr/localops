import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Search, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { rolesApi, usersApi } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import {
  Alert,
  Badge,
  EmptyState,
  ErrorState,
  Pagination,
  Panel,
  Spinner,
  Table,
  Td,
  Th,
} from "@/components/ui/display";
import { Field, Input, Select } from "@/components/ui/form";
import { PASSWORD_MIN } from "@/pages/settings/AccountPage";
import { useAuthStore } from "@/stores/authStore";
import type { Role, User } from "@/types/api";
import { formatRelative } from "@/utils/format";

const PAGE_SIZE = 20;

function RoleChecklist({
  roles,
  selected,
  onChange,
}: {
  roles: Role[];
  selected: string[];
  onChange: (ids: string[]) => void;
}) {
  return (
    <div className="grid max-h-56 gap-1 overflow-y-auto rounded-lg border border-border-strong bg-bg/60 p-2">
      {roles.map((r) => (
        <label key={r.id} className="flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 hover:bg-surface-2">
          <input
            type="checkbox"
            className="mt-0.5 accent-[var(--color-primary)]"
            checked={selected.includes(r.id)}
            onChange={(e) =>
              onChange(e.target.checked ? [...selected, r.id] : selected.filter((id) => id !== r.id))
            }
          />
          <span>
            <span className="block text-sm">{r.name}</span>
            <span className="block text-xs text-subtle">{r.description}</span>
          </span>
        </label>
      ))}
    </div>
  );
}

function CreateUserDialog({ open, onClose, roles }: { open: boolean; onClose: () => void; roles: Role[] }) {
  const qc = useQueryClient();
  const canAssign = useAuthStore((s) => s.hasPermission("role.assign"));
  const [form, setForm] = useState({ username: "", email: "", display_name: "", password: "" });
  const [roleIds, setRoleIds] = useState<string[]>([]);
  const mutation = useMutation({
    mutationFn: () =>
      usersApi.create({
        ...form,
        display_name: form.display_name || undefined,
        role_ids: roleIds,
        must_change_password: true,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      setForm({ username: "", email: "", display_name: "", password: "" });
      setRoleIds([]);
      onClose();
    },
  });
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const submit = (e: FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title="Create user"
      description="The user must change this initial password at first sign-in."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="create-user" loading={mutation.isPending}>Create user</Button>
        </>
      }
    >
      <form id="create-user" onSubmit={submit} className="space-y-4">
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Username" htmlFor="u-username">
            <Input id="u-username" value={form.username} onChange={set("username")} required minLength={3} />
          </Field>
          <Field label="Display name" htmlFor="u-display">
            <Input id="u-display" value={form.display_name} onChange={set("display_name")} />
          </Field>
        </div>
        <Field label="Email" htmlFor="u-email">
          <Input id="u-email" type="email" value={form.email} onChange={set("email")} required />
        </Field>
        <Field label="Initial password" htmlFor="u-password" hint={`At least ${PASSWORD_MIN} characters.`}>
          <Input id="u-password" type="password" autoComplete="new-password" value={form.password} onChange={set("password")} required minLength={PASSWORD_MIN} />
        </Field>
        {canAssign && (
          <div className="space-y-1.5">
            <span className="text-xs font-medium text-muted">Roles</span>
            <RoleChecklist roles={roles} selected={roleIds} onChange={setRoleIds} />
          </div>
        )}
      </form>
    </Dialog>
  );
}

function EditUserDialog({ user, onClose, roles }: { user: User | null; onClose: () => void; roles: Role[] }) {
  const qc = useQueryClient();
  const me = useAuthStore((s) => s.user);
  const has = useAuthStore((s) => s.hasPermission);
  const isSelf = user?.id === me?.id;
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [status, setStatus] = useState(user?.status ?? "active");
  const [password, setPassword] = useState("");
  const [roleIds, setRoleIds] = useState<string[]>(user?.roles.map((r) => r.id) ?? []);

  const mutation = useMutation({
    mutationFn: async () => {
      if (!user) return;
      const body: Record<string, string> = {};
      if (displayName !== user.display_name) body.display_name = displayName;
      if (email !== user.email) body.email = email;
      if (status !== user.status) body.status = status;
      if (password) body.password = password;
      if (Object.keys(body).length) await usersApi.update(user.id, body);
      const original = user.roles.map((r) => r.id).sort().join();
      if (has("role.assign") && !isSelf && [...roleIds].sort().join() !== original) {
        await usersApi.setRoles(user.id, roleIds);
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["users"] });
      onClose();
    },
  });

  return (
    <Dialog
      open={!!user}
      onOpenChange={(o) => !o && onClose()}
      title={`Edit ${user?.username ?? "user"}`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="edit-user" loading={mutation.isPending}>Save changes</Button>
        </>
      }
    >
      <form
        id="edit-user"
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          mutation.mutate();
        }}
      >
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
        <Field label="Display name" htmlFor="e-display">
          <Input id="e-display" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </Field>
        <Field label="Email" htmlFor="e-email">
          <Input id="e-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        {!isSelf && (
          <>
            <Field label="Status" htmlFor="e-status">
              <Select id="e-status" value={status} onChange={(e) => setStatus(e.target.value as User["status"])}>
                <option value="active">Active</option>
                <option value="disabled">Disabled</option>
              </Select>
            </Field>
            <Field label="Reset password" htmlFor="e-password" hint="Leave blank to keep. The user must change it at next sign-in and all their sessions end.">
              <Input id="e-password" type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} minLength={PASSWORD_MIN} />
            </Field>
            {has("role.assign") && (
              <div className="space-y-1.5">
                <span className="text-xs font-medium text-muted">Roles</span>
                <RoleChecklist roles={roles} selected={roleIds} onChange={setRoleIds} />
              </div>
            )}
          </>
        )}
        {isSelf && <Alert tone="primary">You can't change your own status or roles.</Alert>}
      </form>
    </Dialog>
  );
}

export function UsersPage() {
  const qc = useQueryClient();
  const has = useAuthStore((s) => s.hasPermission);
  const me = useAuthStore((s) => s.user);
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);

  const users = useQuery({
    queryKey: ["users", page, q, status],
    queryFn: () => usersApi.list({ page, page_size: PAGE_SIZE, q: q || undefined, status: status || undefined }),
    placeholderData: keepPreviousData,
  });
  const roles = useQuery({ queryKey: ["roles"], queryFn: rolesApi.list, enabled: has("role.read") });
  const remove = useMutation({
    mutationFn: (id: string) => usersApi.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });

  return (
    <Panel
      title="Users"
      bodyClassName="px-0 pb-0"
      actions={
        has("user.create") && (
          <Button size="sm" onClick={() => setCreating(true)}>
            <Plus /> New user
          </Button>
        )
      }
    >
      <div className="flex flex-wrap gap-3 px-5 pb-4">
        <div className="relative w-72">
          <Search className="absolute top-2.5 left-3 size-4 text-subtle" />
          <Input
            className="pl-9"
            placeholder="Search name, username or email"
            aria-label="Search users"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(1);
            }}
          />
        </div>
        <div className="w-48">
          <Select aria-label="Filter by status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
            <option value="">Active & disabled</option>
            <option value="active">Active</option>
            <option value="disabled">Disabled</option>
            <option value="deleted">Deleted</option>
          </Select>
        </div>
      </div>
      {remove.error && (
        <div className="px-5 pb-3">
          <Alert>{errorMessage(remove.error)}</Alert>
        </div>
      )}
      {users.isLoading ? (
        <Spinner />
      ) : users.error ? (
        <ErrorState message={errorMessage(users.error)} onRetry={() => users.refetch()} />
      ) : !users.data?.items.length ? (
        <EmptyState title="No users found" />
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th>User</Th>
                <Th>Email</Th>
                <Th>Roles</Th>
                <Th>Status</Th>
                <Th>Last sign-in</Th>
                <Th className="w-24" />
              </tr>
            </thead>
            <tbody>
              {users.data.items.map((u) => (
                <tr key={u.id} className="hover:bg-surface-2/60">
                  <Td>
                    <div className="font-medium">{u.display_name}</div>
                    <div className="font-mono text-xs text-subtle">{u.username}</div>
                  </Td>
                  <Td className="text-muted">{u.email}</Td>
                  <Td>
                    <div className="flex flex-wrap gap-1">
                      {u.roles.length ? u.roles.map((r) => <Badge key={r.id} tone="primary">{r.name}</Badge>) : <span className="text-xs text-subtle">none</span>}
                    </div>
                  </Td>
                  <Td>
                    <Badge tone={u.status === "active" ? "success" : u.status === "disabled" ? "warning" : "neutral"}>
                      {u.status}
                    </Badge>
                    {u.must_change_password && u.status === "active" && (
                      <Badge tone="neutral" className="ml-1" title="Must change password">pw reset</Badge>
                    )}
                  </Td>
                  <Td className="text-muted">{formatRelative(u.last_login_at)}</Td>
                  <Td>
                    {u.status !== "deleted" && (
                      <div className="flex justify-end gap-1">
                        {has("user.update") && (
                          <Button variant="ghost" size="icon" aria-label={`Edit ${u.username}`} onClick={() => setEditing(u)}>
                            <Pencil />
                          </Button>
                        )}
                        {has("user.delete") && u.id !== me?.id && (
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`Delete ${u.username}`}
                            onClick={() => {
                              if (window.confirm(`Delete user ${u.username}? This cannot be undone.`)) remove.mutate(u.id);
                            }}
                          >
                            <Trash2 className="text-red-400" />
                          </Button>
                        )}
                      </div>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pagination page={page} pageSize={PAGE_SIZE} total={users.data.total} onPage={setPage} />
        </>
      )}
      <CreateUserDialog open={creating} onClose={() => setCreating(false)} roles={roles.data ?? []} />
      {editing && <EditUserDialog key={editing.id} user={editing} onClose={() => setEditing(null)} roles={roles.data ?? []} />}
    </Panel>
  );
}
