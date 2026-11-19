import { useQuery } from "@tanstack/react-query";
import { ClipboardCheck, ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { approvalsApi, executionsApi, investigationsApi } from "@/api/endpoints";
import { liveRemediation } from "@/components/remediation/remediation";
import { ApprovalStatusBadge, ExecutionsTable, RemediationPanel, RiskBadge } from "@/components/remediation/shared";
import { Alert, EmptyState, ErrorState, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { useAuthStore } from "@/stores/authStore";
import type { Approval, IncidentDetail } from "@/types/api";
import { formatRelative } from "@/utils/format";

function ApprovalsTable({ rows }: { rows: Approval[] }) {
  if (!rows.length) return <EmptyState icon={<ClipboardCheck className="size-8" />} title="No approval requests" />;
  return (
    <Table>
      <thead>
        <tr><Th>Action</Th><Th>Risk</Th><Th>Status</Th><Th>Requested by</Th><Th>Decided by</Th><Th>When</Th></tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={a.id} className="hover:bg-surface-2/60">
            <Td className="max-w-md">
              <Link to={`/approvals/${a.id}`} className="text-sm text-blue-300 hover:underline">{a.description}</Link>
            </Td>
            <Td><RiskBadge risk={a.risk_level} /></Td>
            <Td><ApprovalStatusBadge status={a.status} auto={a.auto_approved} /></Td>
            <Td className="text-xs">{a.requested_by_username ?? "—"}</Td>
            <Td className="text-xs">{a.auto_approved ? "policy" : (a.approved_by_username ?? "—")}</Td>
            <Td className="text-xs text-muted">{formatRelative(a.created_at)}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

/** Incident → Remediation tab: the latest plan, its approvals and executions. */
export function IncidentRemediation({ inc }: { inc: IncidentDetail }) {
  const canRead = useAuthStore((s) => s.hasPermission("approval.read"));
  const latest = inc.investigations.find((i) => i.status === "completed") ?? inc.investigations[0];
  const inv = useQuery({ queryKey: ["investigation", latest?.id], queryFn: () => investigationsApi.get(latest!.id), enabled: !!latest });
  const approvals = useQuery({
    queryKey: ["approvals", { incident_id: inc.id }],
    queryFn: () => approvalsApi.list({ page: 1, page_size: 50, incident_id: inc.id }),
    enabled: canRead,
  });
  const executions = useQuery({
    queryKey: ["executions", { incident_id: inc.id }],
    queryFn: () => executionsApi.list({ page: 1, page_size: 50, incident_id: inc.id }),
    enabled: canRead,
  });
  const plan = inv.data?.status === "completed" ? liveRemediation(inv.data.result.remediation, []) : null;
  const advisory = inc.root_cause?.recommended_remediation ?? [];
  return (
    <div className="space-y-5">
      {plan ? (
        <RemediationPanel data={plan} investigationId={inv.data?.id} />
      ) : inv.data && inv.data.status !== "completed" ? (
        <Alert tone="primary">
          <span className="flex items-center gap-2">
            <ShieldCheck className="size-4" /> The investigation is {inv.data.status.replace(/_/g, " ")}.{" "}
            <Link to={`/investigations/${inv.data.id}`} className="underline">Follow it live</Link>
          </span>
        </Alert>
      ) : (
        <Panel title="Remediation">
          {advisory.length ? (
            <ul className="list-inside list-disc space-y-1 text-sm">
              {advisory.map((m) => <li key={m.action}>{m.action}</li>)}
            </ul>
          ) : (
            <p className="text-sm text-muted">No remediation has been proposed yet. Run an investigation to get an evidence-backed plan.</p>
          )}
        </Panel>
      )}
      {canRead && (
        <>
          <Panel title="Approval requests" bodyClassName="px-0 pb-0">
            {approvals.isLoading ? <Spinner /> : approvals.error ? <ErrorState message={errorMessage(approvals.error)} /> : <ApprovalsTable rows={approvals.data?.items ?? []} />}
          </Panel>
          <Panel title="Executions" bodyClassName="px-0 pb-0">
            {executions.isLoading ? <Spinner /> : executions.error ? <ErrorState message={errorMessage(executions.error)} /> : <ExecutionsTable rows={executions.data?.items ?? []} />}
          </Panel>
        </>
      )}
    </div>
  );
}

/** Resource → Remediation tab: every action executed on this resource. */
export function ResourceRemediation({ resourceId }: { resourceId: string }) {
  const canRead = useAuthStore((s) => s.hasPermission("approval.read"));
  const query = useQuery({
    queryKey: ["executions", { resource_id: resourceId }],
    queryFn: () => executionsApi.list({ page: 1, page_size: 50, resource_id: resourceId }),
    enabled: canRead,
  });
  if (!canRead) return <Panel><EmptyState title="You need the approval.read permission to see remediation history" /></Panel>;
  return (
    <Panel title="Remediation history" bodyClassName="px-0 pb-0">
      {query.isLoading ? <Spinner /> : query.error ? <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} /> : <ExecutionsTable rows={query.data?.items ?? []} showResource={false} />}
    </Panel>
  );
}
