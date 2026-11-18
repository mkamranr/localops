import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { tenantsApi } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, ErrorState, Pagination, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Input } from "@/components/ui/form";
import { PASSWORD_MIN } from "@/pages/settings/AccountPage";
import { formatDateTime } from "@/utils/format";

const PAGE_SIZE = 20;
const EMPTY = { name: "", slug: "", username: "admin", email: "", password: "" };

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

function CreateTenantDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState(EMPTY);
  const [slugEdited, setSlugEdited] = useState(false);
  const mutation = useMutation({
    mutationFn: () =>
      tenantsApi.create({
        name: form.name,
        slug: form.slug,
        admin: { username: form.username, email: form.email, password: form.password },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tenants"] });
      setForm(EMPTY);
      setSlugEdited(false);
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title="Create tenant"
      description="Creates an isolated tenant with built-in roles and its first Tenant Administrator."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="create-tenant" loading={mutation.isPending}>Create tenant</Button>
        </>
      }
    >
      <form id="create-tenant" onSubmit={submit} className="space-y-4">
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
        <Field label="Name" htmlFor="t-name">
          <Input
            id="t-name"
            value={form.name}
            required
            onChange={(e) =>
              setForm((f) => ({ ...f, name: e.target.value, slug: slugEdited ? f.slug : slugify(e.target.value) }))
            }
          />
        </Field>
        <Field label="Slug" htmlFor="t-slug" hint="Used at sign-in. Lowercase letters, digits and dashes.">
          <Input
            id="t-slug"
            className="font-mono"
            value={form.slug}
            required
            pattern="[a-z0-9][a-z0-9-]{1,62}"
            onChange={(e) => {
              setSlugEdited(true);
              setForm((f) => ({ ...f, slug: e.target.value }));
            }}
          />
        </Field>
        <div className="rounded-lg border border-border p-3">
          <p className="mb-3 text-xs font-semibold text-muted uppercase">First administrator</p>
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Username" htmlFor="t-user">
                <Input id="t-user" value={form.username} required onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))} />
              </Field>
              <Field label="Email" htmlFor="t-email">
                <Input id="t-email" type="email" value={form.email} required onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))} />
              </Field>
            </div>
            <Field label="Initial password" htmlFor="t-pass" hint={`At least ${PASSWORD_MIN} characters; must be changed at first sign-in.`}>
              <Input id="t-pass" type="password" autoComplete="new-password" minLength={PASSWORD_MIN} value={form.password} required onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))} />
            </Field>
          </div>
        </div>
      </form>
    </Dialog>
  );
}

export function TenantsPage() {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [creating, setCreating] = useState(false);
  const tenants = useQuery({
    queryKey: ["tenants", page],
    queryFn: () => tenantsApi.list(page, PAGE_SIZE),
    placeholderData: keepPreviousData,
  });
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => tenantsApi.setEnabled(id, enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tenants"] }),
  });

  return (
    <Panel
      title="Tenants"
      bodyClassName="px-0 pb-0"
      actions={
        <Button size="sm" onClick={() => setCreating(true)}>
          <Plus /> New tenant
        </Button>
      }
    >
      {toggle.error && (
        <div className="px-5 pb-3">
          <Alert>{errorMessage(toggle.error)}</Alert>
        </div>
      )}
      {tenants.isLoading ? (
        <Spinner />
      ) : tenants.error ? (
        <ErrorState message={errorMessage(tenants.error)} onRetry={() => tenants.refetch()} />
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th>Name</Th>
                <Th>Slug</Th>
                <Th>Status</Th>
                <Th>Created</Th>
                <Th className="w-32" />
              </tr>
            </thead>
            <tbody>
              {tenants.data?.items.map((t) => (
                <tr key={t.id} className="hover:bg-surface-2/60">
                  <Td className="font-medium">{t.name}</Td>
                  <Td className="font-mono text-xs text-muted">{t.slug}</Td>
                  <Td>
                    <Badge tone={t.status === "active" ? "success" : "warning"}>{t.status}</Badge>
                  </Td>
                  <Td className="text-muted">{formatDateTime(t.created_at)}</Td>
                  <Td className="text-right">
                    {t.slug !== "system" && (
                      <Button
                        variant="secondary"
                        size="sm"
                        loading={toggle.isPending && toggle.variables?.id === t.id}
                        onClick={() => {
                          const enable = t.status !== "active";
                          if (enable || window.confirm(`Disable tenant ${t.name}? Its users will be signed out.`)) {
                            toggle.mutate({ id: t.id, enabled: enable });
                          }
                        }}
                      >
                        {t.status === "active" ? "Disable" : "Enable"}
                      </Button>
                    )}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pagination page={page} pageSize={PAGE_SIZE} total={tenants.data?.total ?? 0} onPage={setPage} />
        </>
      )}
      <CreateTenantDialog open={creating} onClose={() => setCreating(false)} />
    </Panel>
  );
}
