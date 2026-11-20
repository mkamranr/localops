import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { errorMessage } from "@/api/client";
import { toolDefinitionsApi, toolsApi } from "@/api/endpoints";
import { invalidateTools, ToolReviewDialog, ToolStatusBadge, ToolTestDialog } from "@/components/tools/ToolDialogs";
import { RISK_TONE } from "@/components/tools/tools";
import { Button } from "@/components/ui/button";
import { Alert, Badge, ErrorState, Spinner, Table, Td, Th } from "@/components/ui/display";
import type { Integration, ToolDefinition, ToolSyncResult } from "@/types/api";
import { formatRelative } from "@/utils/format";

/** Tools tab of an MCP / HTTP API integration. */
export function IntegrationTools({ integration, canManage }: { integration: Integration; canManage: boolean }) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["tool-definitions", { integration_id: integration.id }],
    queryFn: () => toolDefinitionsApi.list({ integration_id: integration.id }),
  });
  const catalogue = useQuery({ queryKey: ["tools"], queryFn: () => toolsApi.list() });
  const readTools = (catalogue.data ?? []).filter((t) => t.kind === "read").map((t) => t.name);
  const [review, setReview] = useState<ToolDefinition | null>(null);
  const [testing, setTesting] = useState<ToolDefinition | null>(null);
  const [synced, setSynced] = useState<ToolSyncResult | null>(null);
  const sync = useMutation({
    mutationFn: () => toolDefinitionsApi.sync(integration.id),
    onSuccess: (r) => {
      setSynced(r);
      invalidateTools(queryClient);
    },
  });
  const toggle = useMutation({
    mutationFn: (t: ToolDefinition) => toolDefinitionsApi.update(t.id, { enabled: !t.enabled }),
    onSuccess: () => invalidateTools(queryClient),
  });
  const error = sync.error ?? toggle.error;

  return (
    <div className="space-y-3 pt-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-xs text-muted">
          {integration.integration_type === "mcp"
            ? "Tools offered by the server arrive disabled. Review each one, classify it as read or change, then enable it."
            : "Custom request templates for this API are managed under Settings → Tools."}
        </p>
        {canManage && integration.integration_type === "mcp" && (
          <Button size="sm" variant="secondary" className="ml-auto" loading={sync.isPending} disabled={!integration.enabled} onClick={() => sync.mutate()}>
            <RefreshCw /> Sync tools
          </Button>
        )}
      </div>
      {synced && (
        <Alert tone="success">
          {synced.server.name ? `${synced.server.name}${synced.server.version ? ` ${synced.server.version}` : ""}: ` : ""}
          {synced.total} tool(s) — {synced.added.length} new, {synced.changed.length} changed, {synced.missing.length} missing, {synced.unchanged} unchanged.
          {!!synced.changed.length && " Changed tools were disabled until re-approved."}
        </Alert>
      )}
      {error && <Alert>{errorMessage(error)}</Alert>}
      {query.isLoading ? (
        <Spinner />
      ) : query.error || !query.data ? (
        <ErrorState message={errorMessage(query.error)} />
      ) : !query.data.length ? (
        <p className="py-3 text-xs text-subtle">
          {integration.integration_type === "mcp" ? "No tools yet. Sync to fetch the server's tool list." : "No HTTP tools use this integration yet."}
        </p>
      ) : (
        <Table>
          <thead>
            <tr><Th>Tool</Th><Th>Status</Th><Th>Kind</Th><Th>Risk</Th><Th>Synced</Th><Th>Enabled</Th><Th /></tr>
          </thead>
          <tbody>
            {query.data.map((t) => (
              <tr key={t.id}>
                <Td>
                  <div className="font-mono text-xs">{t.name}</div>
                  <div className="max-w-md truncate text-xs text-muted">{t.description}</div>
                </Td>
                <Td><ToolStatusBadge status={t.status} /></Td>
                <Td><Badge tone={t.kind === "change" ? "warning" : "neutral"}>{t.kind}</Badge></Td>
                <Td><Badge tone={RISK_TONE[t.risk_level] ?? "neutral"}>{t.risk_level}</Badge></Td>
                <Td className="whitespace-nowrap text-xs">{formatRelative(t.last_synced_at ?? t.created_at)}</Td>
                <Td>
                  <input
                    type="checkbox"
                    className="size-4"
                    aria-label={`Enable ${t.name}`}
                    checked={t.enabled}
                    disabled={!canManage || toggle.isPending || t.status === "missing" || t.status === "schema_changed"}
                    onChange={() => toggle.mutate(t)}
                  />
                </Td>
                <Td className="whitespace-nowrap text-right">
                  {t.enabled && t.kind === "read" && canManage && (
                    <Button variant="ghost" size="sm" onClick={() => setTesting(t)}>Test</Button>
                  )}
                  <Button variant={t.status === "new" || t.status === "schema_changed" ? "secondary" : "ghost"} size="sm" onClick={() => setReview(t)}>
                    {canManage ? "Review" : "View"}
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {review && <ToolReviewDialog key={review.id} tool={review} readTools={readTools} canManage={canManage} onClose={() => setReview(null)} />}
      {testing && <ToolTestDialog tool={testing} onClose={() => setTesting(null)} />}
    </div>
  );
}
