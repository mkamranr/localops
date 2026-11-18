import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Search, Sparkles } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { investigationsApi } from "@/api/endpoints";
import { InvestigationStatusBadge, requestTypeLabel, STATUS_META } from "@/components/investigation/shared";
import { Button } from "@/components/ui/button";
import { EmptyState, ErrorState, Pagination, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Input, Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { InvestigationStatus } from "@/types/api";
import { formatDateTime, formatRelative } from "@/utils/format";

const PAGE_SIZE = 20;

export function InvestigationsPage() {
  const navigate = useNavigate();
  const canInvestigate = useAuthStore((s) => s.hasPermission("incident.investigate"));
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const query = useQuery({
    queryKey: ["investigations", { page, q, status }],
    queryFn: () => investigationsApi.list({ page, page_size: PAGE_SIZE, q: q || undefined, status: status || undefined }),
    placeholderData: keepPreviousData,
    refetchInterval: 10_000,
  });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Investigations</h1>
          <p className="text-sm text-muted">AI-led investigations, their agent activity and reports.</p>
        </div>
        {canInvestigate && (
          <Button asChild>
            <Link to="/assistant"><Sparkles /> New investigation</Link>
          </Button>
        )}
      </div>

      <Panel bodyClassName="px-0 pb-0">
        <div className="flex flex-wrap gap-3 px-5 pb-4">
          <div className="relative min-w-60 flex-1">
            <Search className="absolute top-2.5 left-3 size-4 text-subtle" />
            <Input
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setPage(1);
              }}
              placeholder="Search requests…"
              className="pl-9"
              aria-label="Search"
            />
          </div>
          <Select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
            className="w-48"
            aria-label="Status"
          >
            <option value="">All statuses</option>
            {(Object.keys(STATUS_META) as InvestigationStatus[]).map((s) => (
              <option key={s} value={s}>{STATUS_META[s].label}</option>
            ))}
          </Select>
        </div>
        {query.isLoading ? (
          <Spinner />
        ) : query.error || !query.data ? (
          <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />
        ) : !query.data.items.length ? (
          <EmptyState title="No investigations" description={q || status ? "Nothing matches the filters." : "Start one from the AI Assistant."} />
        ) : (
          <>
            <Table>
              <thead>
                <tr>
                  <Th>Investigation</Th>
                  <Th>Status</Th>
                  <Th>Type</Th>
                  <Th>Mode</Th>
                  <Th>Started</Th>
                </tr>
              </thead>
              <tbody>
                {query.data.items.map((inv) => (
                  <tr
                    key={inv.id}
                    className="cursor-pointer transition-colors hover:bg-surface-2/60"
                    onClick={() => navigate(`/investigations/${inv.id}`)}
                  >
                    <Td className="max-w-md">
                      <Link to={`/investigations/${inv.id}`} className="block truncate font-medium text-fg hover:text-blue-300" onClick={(e) => e.stopPropagation()}>
                        {inv.title}
                      </Link>
                      {inv.pending_question && <p className="truncate text-xs text-amber-300">{inv.pending_question}</p>}
                      {inv.error && <p className="truncate text-xs text-red-300">{inv.error}</p>}
                    </Td>
                    <Td><InvestigationStatusBadge status={inv.status} /></Td>
                    <Td className="text-xs text-muted capitalize">{requestTypeLabel(inv.request_type)}</Td>
                    <Td className="text-xs text-muted capitalize">{inv.execution_mode}</Td>
                    <Td className="text-xs text-muted" title={formatDateTime(inv.started_at)}>{formatRelative(inv.started_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <Pagination page={page} pageSize={PAGE_SIZE} total={query.data.total} onPage={setPage} />
          </>
        )}
      </Panel>
    </div>
  );
}
