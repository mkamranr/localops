import { useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { authApi } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { Alert, Badge, Panel } from "@/components/ui/display";
import { Field, Input } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";

export const PASSWORD_MIN = 12;

export function AccountPage() {
  const user = useAuthStore((s) => s.user);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [saving, setSaving] = useState(false);

  if (!user) return null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setDone(false);
    if (next !== confirm) return setError("New passwords do not match");
    if (next.length < PASSWORD_MIN) return setError(`Password must be at least ${PASSWORD_MIN} characters`);
    setSaving(true);
    try {
      await authApi.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="grid gap-6 xl:grid-cols-2">
      <Panel title="Profile">
        <dl className="grid grid-cols-[8rem_1fr] gap-y-3 text-sm">
          <dt className="text-muted">Username</dt>
          <dd className="font-mono">{user.username}</dd>
          <dt className="text-muted">Display name</dt>
          <dd>{user.display_name}</dd>
          <dt className="text-muted">Email</dt>
          <dd>{user.email}</dd>
          <dt className="text-muted">Tenant</dt>
          <dd>
            {user.tenant.name} <span className="font-mono text-xs text-subtle">({user.tenant.slug})</span>
          </dd>
          <dt className="text-muted">Roles</dt>
          <dd className="flex flex-wrap gap-1.5">
            {user.roles.length ? user.roles.map((r) => <Badge key={r} tone="primary">{r}</Badge>) : "—"}
          </dd>
          <dt className="text-muted">Permissions</dt>
          <dd className="text-muted">{user.permissions.length} granted</dd>
        </dl>
      </Panel>
      <Panel title="Change password">
        <form onSubmit={submit} className="space-y-4">
          {user.must_change_password && (
            <Alert tone="warning">You are using an initial password. Please set a new one.</Alert>
          )}
          {error && <Alert>{error}</Alert>}
          {done && <Alert tone="success">Password changed. Other sessions were signed out.</Alert>}
          <Field label="Current password" htmlFor="current">
            <Input id="current" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
          </Field>
          <Field label="New password" htmlFor="new" hint={`At least ${PASSWORD_MIN} characters.`}>
            <Input id="new" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required />
          </Field>
          <Field label="Confirm new password" htmlFor="confirm">
            <Input id="confirm" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
          </Field>
          <Button type="submit" loading={saving}>
            Update password
          </Button>
        </form>
      </Panel>
    </div>
  );
}
