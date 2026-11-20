import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { errorMessage } from "@/api/client";
import { integrationsApi, toolDefinitionsApi, toolsApi } from "@/api/endpoints";
import {
  HttpToolDialog,
  invalidateTools,
  SourceBadge,
  ToolReviewDialog,
  ToolStatusBadge,
  ToolTestDialog,
} from "@/components/tools/ToolDialogs";
import { combineRows, RISK_TONE } from "@/components/tools/tools";
import { Button } from "@/components/ui/button";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Input, Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { ToolDefinition } from "@/types/api";

export function ToolsPage() {
  const queryClient = useQueryClient();
  const canManage = useAuthStore((s) => s.hasPermission("tool.manage"));
  const catalogue = useQuery({ queryKey: ["tools"], queryFn: () => toolsApi.list() });
  const definitions = useQuery({ queryKey: ["tool-definitions", {}], queryFn: () => toolDefinitionsApi.list() });
  const integrations = useQuery({ queryKey: ["integrations"], queryFn: integrationsApi.list });
  const [filter, setFilter] = useState({ q: "", source: "", kind: "" });
  const [editor, setEditor] = useState<{ tool: ToolDefinition | null } | null>(null);
  const [review, setReview] = useState<ToolDefinition | null>(null);
  const [testing, setTesting] = useState<ToolDefinition | null>(null);
  const remove = useMutation({
    mutationFn: (id: string) => toolDefinitionsApi.remove(id),
    onSuccess: () => invalidateTools(queryClient),
  });

  const rows = useMemo(
    () => combineRows(catalogue.data ?? [], definitions.data ?? []),
    [catalogue.data, definitions.data],
  );
  const readTools = (catalogue.data ?? []).filter((t) => t.kind === "read").map((t) => t.name);
  const httpIntegrations = (integrations.data ?? []).filter((i) => i.integration_type === "http_api");
  const q = filter.q.trim().toLowerCase();
  const shown = rows.filter(
    (r) =>
      (!filter.source || r.source === filter.source) &&
      (!filter.kind || r.kind === filter.kind) &&
      (!q || r.name.includes(q) || r.description.toLowerCase().includes(q)),
  );

  if (catalogue.isLoading || definitions.isLoading) return <Spinner />;
  if (catalogue.error || definitions.error)
    return <ErrorState message={errorMessage(catalogue.error ?? definitions.error)} onRetry={() => { void catalogue.refetch(); void definitions.refetch(); }} />;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Tools</h1>
          <p className="text-sm text-muted">
            Built-in diagnostic and remediation tools, MCP server tools and custom HTTP tools. Read tools can be given to
            agents; change tools run only through policy and approval.
          </p>
        </div>
        {canManage && (
          <Button onClick={() => setEditor({ tool: null })}>
            <Plus /> New HTTP tool
          </Button>
        )}
      </div>

      <Panel
        title={`Catalogue (${shown.length})`}
        actions={
          <div className="flex flex-wrap gap-2">
            <Input aria-label="Search tools" placeholder="Search…" value={filter.q} onChange={(e) => setFilter({ ...filter, q: e.target.value })} className="w-48" />
            <Select aria-label="Source" value={filter.source} onChange={(e) => setFilter({ ...filter, source: e.target.value })} className="w-36">
              <option value="">All sources</option>
              <option value="builtin">Built-in</option>
              <option value="mcp">MCP</option>
              <option value="http">HTTP</option>
            </Select>
            <Select aria-label="Kind" value={filter.kind} onChange={(e) => setFilter({ ...filter, kind: e.target.value })} className="w-32">
              <option value="">All kinds</option>
              <option value="read">Read</option>
              <option value="change">Change</option>
            </Select>
          </div>
        }
      >
        {remove.error && <div className="mb-3"><Alert>{errorMessage(remove.error)}</Alert></div>}
        {!shown.length ? (
          <EmptyState title="No tools match" description="Adjust the filters." />
        ) : (
          <Table>
            <thead>
              <tr><Th>Tool</Th><Th>Source</Th><Th>Kind</Th><Th>Risk</Th><Th>Permission</Th><Th>Category / integration</Th><Th>Status</Th><Th /></tr>
            </thead>
            <tbody>
              {shown.map((r) => {
                const d = r.definition;
                return (
                  <tr key={r.name} className={d && !d.usable ? "opacity-70" : undefined}>
                    <Td>
                      <div className="font-mono text-xs">{r.name}</div>
                      <div className="max-w-md truncate text-xs text-muted" title={r.description}>{r.description}</div>
                    </Td>
                    <Td><SourceBadge source={r.source} /></Td>
                    <Td><Badge tone={r.kind === "change" ? "warning" : "neutral"}>{r.kind}</Badge></Td>
                    <Td><Badge tone={RISK_TONE[r.risk] ?? "neutral"}>{r.risk}</Badge></Td>
                    <Td className="font-mono text-xs">{r.permission}</Td>
                    <Td className="text-xs">{r.category}</Td>
                    <Td>
                      {d ? (
                        <div className="flex flex-wrap gap-1">
                          <ToolStatusBadge status={d.status} />
                          {!d.enabled && <Badge tone="neutral">disabled</Badge>}
                        </div>
                      ) : (
                        <Badge tone="success">available</Badge>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap text-right">
                      {d && (
                        <>
                          {canManage && d.enabled && d.kind === "read" && d.usable && (
                            <Button variant="ghost" size="sm" onClick={() => setTesting(d)}>Test</Button>
                          )}
                          {d.source === "http" && canManage ? (
                            <Button variant="ghost" size="icon" aria-label={`Edit ${d.name}`} onClick={() => setEditor({ tool: d })}><Pencil /></Button>
                          ) : (
                            <Button variant="ghost" size="sm" onClick={() => setReview(d)}>{canManage ? "Review" : "View"}</Button>
                          )}
                          {canManage && (
                            <Button
                              variant="ghost"
                              size="icon"
                              aria-label={`Delete ${d.name}`}
                              onClick={() =>
                                window.confirm(
                                  d.source === "mcp"
                                    ? `Delete ${d.name}? The next sync adds it again (disabled).`
                                    : `Delete ${d.name}? Agents and policies referring to it stop using it.`,
                                ) && remove.mutate(d.id)
                              }
                            >
                              <Trash2 />
                            </Button>
                          )}
                        </>
                      )}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        )}
      </Panel>

      {editor && (
        <HttpToolDialog
          key={editor.tool?.id ?? "new"}
          tool={editor.tool}
          integrations={httpIntegrations}
          readTools={readTools}
          onClose={() => setEditor(null)}
        />
      )}
      {review && <ToolReviewDialog key={review.id} tool={review} readTools={readTools} canManage={canManage} onClose={() => setReview(null)} />}
      {testing && <ToolTestDialog tool={testing} onClose={() => setTesting(null)} />}
    </div>
  );
}
