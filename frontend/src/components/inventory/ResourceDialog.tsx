import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { errorMessage } from "@/api/client";
import { resourcesApi } from "@/api/endpoints";
import { RESOURCE_TYPES, TYPE_META } from "@/components/inventory/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import type { Resource, ResourceInput, ResourceType } from "@/types/api";

const split = (text: string) =>
  text
    .split(/[,\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);

/**
 * Create a manual resource, or edit one. For discovered resources only the
 * descriptive fields are editable; facts come from discovery.
 */
export function ResourceDialog({
  resource,
  onClose,
  onSaved,
}: {
  resource: Resource | null;
  onClose: () => void;
  onSaved?: (r: Resource) => void;
}) {
  const queryClient = useQueryClient();
  const discovered = resource?.source === "discovered";
  const [form, setForm] = useState(() => ({
    name: resource?.name ?? "",
    resource_type: resource?.resource_type ?? ("application" as ResourceType),
    hostname: resource?.hostname ?? "",
    ips: (resource?.ip_addresses ?? []).join(", "),
    os: resource?.os ?? "",
    environment: resource?.environment ?? "",
    group_name: resource?.group_name ?? "",
    owner: resource?.owner ?? "",
    tags: (resource?.tags ?? []).join(", "),
    description: resource?.description ?? "",
  }));
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const mutation = useMutation({
    mutationFn: () => {
      const descriptive: Partial<ResourceInput> = {
        environment: form.environment.trim() || null,
        group_name: form.group_name.trim() || null,
        owner: form.owner.trim() || null,
        tags: split(form.tags),
        description: form.description,
      };
      if (discovered && resource) return resourcesApi.update(resource.id, descriptive);
      const body: ResourceInput = {
        ...descriptive,
        name: form.name.trim(),
        resource_type: form.resource_type,
        hostname: form.hostname.trim() || null,
        ip_addresses: split(form.ips),
        os: form.os.trim() || null,
      };
      return resource ? resourcesApi.update(resource.id, body) : resourcesApi.create(body);
    },
    onSuccess: (r) => {
      void queryClient.invalidateQueries({ queryKey: ["resources"] });
      onSaved?.(r);
      onClose();
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={resource ? `Edit ${resource.name}` : "Add resource"}
      description={
        discovered
          ? "Name, type, addresses and facts come from discovery. You can set the environment, group, owner, tags and description."
          : "Register a resource that discovery does not cover, such as an application, database or appliance."
      }
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" form="resource-form" loading={mutation.isPending}>
            {resource ? "Save" : "Add resource"}
          </Button>
        </>
      }
    >
      <form id="resource-form" onSubmit={submit} className="space-y-4">
        {!discovered && (
          <>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Name" htmlFor="r-name">
                <Input id="r-name" value={form.name} onChange={set("name")} required maxLength={200} placeholder="billing-api" />
              </Field>
              <Field label="Type" htmlFor="r-type">
                <Select id="r-type" value={form.resource_type} onChange={set("resource_type")}>
                  {RESOURCE_TYPES.map((t) => <option key={t} value={t}>{TYPE_META[t].label}</option>)}
                </Select>
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Hostname" htmlFor="r-host">
                <Input id="r-host" value={form.hostname} onChange={set("hostname")} placeholder="billing.corp.local" />
              </Field>
              <Field label="IP addresses" htmlFor="r-ips" hint="Comma separated">
                <Input id="r-ips" value={form.ips} onChange={set("ips")} placeholder="10.0.0.20" />
              </Field>
            </div>
            <Field label="Operating system / version" htmlFor="r-os">
              <Input id="r-os" value={form.os} onChange={set("os")} />
            </Field>
          </>
        )}
        <div className="grid grid-cols-3 gap-4">
          <Field label="Environment" htmlFor="r-env">
            <Input id="r-env" value={form.environment} onChange={set("environment")} maxLength={50} placeholder="production" />
          </Field>
          <Field label="Group" htmlFor="r-group">
            <Input id="r-group" value={form.group_name} onChange={set("group_name")} maxLength={100} placeholder="billing" />
          </Field>
          <Field label="Owner" htmlFor="r-owner">
            <Input id="r-owner" value={form.owner} onChange={set("owner")} maxLength={100} placeholder="platform-team" />
          </Field>
        </div>
        <Field label="Tags" htmlFor="r-tags" hint="Comma separated">
          <Input id="r-tags" value={form.tags} onChange={set("tags")} placeholder="critical, pci" />
        </Field>
        <Field label="Description" htmlFor="r-desc">
          <Textarea id="r-desc" value={form.description} onChange={set("description")} rows={3} maxLength={2000} />
        </Field>
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}
