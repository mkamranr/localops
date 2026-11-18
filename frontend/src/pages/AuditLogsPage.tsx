import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, RotateCcw } from "lucide-react";
import { Fragment, useState } from "react";
import { errorMessage } from "@/api/client";
import { auditApi } from "@/api/endpoints";
import { Button } from "@/components/ui/button";
import {
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
import { Input, Select } from "@/components/ui/form";
import { formatDateTime } from "@/utils/format";

const PAGE_SIZE = 25;

export function AuditLogsPage() {
  const [page, setPage] = useState(1);
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);

  const filters = {
    action: action.trim() || undefined,
    outcome: outcome || undefined,
    date_from: dateFrom ? new Date(dateFrom).toISOString() : undefined,
    date_to: dateTo ? new Date(dateTo).toISOString() : undefined,
  };
  const query = useQuery({
    queryKey: ["audit", page, filters],
    queryFn: () => auditApi.list({ page, page_size: PAGE_SIZE, ...filters }),
    placeholderData: keepPreviousData,
  });

  const reset = () => {
    setAction("");
    setOutcome("");
    setDateFrom("");
    setDateTo("");
    setPage(1);
  };
  const onFilter = <T,>(setter: (v: T) => void) => (v: T) => {
    setter(v);
    setPage(1);
  };

  return (
    <Panel bodyClassName="px-0 pb-0">
      <div className="flex flex-wrap items-end gap-3 px-5 pb-4">
        <div className="w-56">
          <Input
            placeholder="Action prefix, e.g. auth."
            aria-label="Filter by action"
            value={action}
            onChange={(e) => onFilter(setAction)(e.target.value)}
          />
        </div>
        <div className="w-40">
          <Select aria-label="Filter by outcome" value={outcome} onChange={(e) => onFilter(setOutcome)(e.target.value)}>
            <option value="">All outcomes</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </Select>
        </div>
        <Input type="datetime-local" aria-label="From" className="w-52" value={dateFrom} onChange={(e) => onFilter(setDateFrom)(e.target.value)} />
        <Input type="datetime-local" aria-label="To" className="w-52" value={dateTo} onChange={(e) => onFilter(setDateTo)(e.target.value)} />
        <Button variant="ghost" size="sm" onClick={reset}>
          <RotateCcw /> Reset
        </Button>
      </div>

      {query.isLoading ? (
        <Spinner />
      ) : query.error ? (
        <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />
      ) : !query.data?.items.length ? (
        <EmptyState title="No audit events" description="Try widening the filters." />
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th className="w-8" />
                <Th>Time</Th>
                <Th>Action</Th>
                <Th>Outcome</Th>
                <Th>Actor</Th>
                <Th>Entity</Th>
                <Th>IP address</Th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((e) => (
                <Fragment key={e.id}>
                  <tr
                    className="cursor-pointer hover:bg-surface-2/60"
                    onClick={() => setExpanded(expanded === e.id ? null : e.id)}
                  >
                    <Td className="text-subtle">
                      {expanded === e.id ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                    </Td>
                    <Td className="whitespace-nowrap text-muted">{formatDateTime(e.created_at)}</Td>
                    <Td className="font-mono text-xs text-blue-300">{e.action}</Td>
                    <Td>
                      <Badge tone={e.outcome === "success" ? "success" : "danger"}>{e.outcome}</Badge>
                    </Td>
                    <Td>{e.username ?? <span className="text-subtle">system</span>}</Td>
                    <Td className="text-muted">
                      {e.entity_type ? (
                        <span>
                          {e.entity_type} <span className="font-mono text-xs text-subtle">{e.entity_id?.slice(0, 8)}</span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </Td>
                    <Td className="font-mono text-xs text-muted">{e.ip_address ?? "—"}</Td>
                  </tr>
                  {expanded === e.id && (
                    <tr>
                      <Td colSpan={7} className="bg-bg/50">
                        <div className="grid gap-2 text-xs md:grid-cols-[10rem_1fr]">
                          <span className="text-subtle">Event ID</span>
                          <span className="font-mono">{e.id}</span>
                          <span className="text-subtle">Request ID</span>
                          <span className="font-mono">{e.request_id ?? "—"}</span>
                          <span className="text-subtle">Details</span>
                          <pre className="overflow-x-auto rounded-md border border-border bg-surface p-3 font-mono text-[11px] text-muted">
                            {JSON.stringify(e.details, null, 2)}
                          </pre>
                        </div>
                      </Td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </Table>
          <Pagination page={page} pageSize={PAGE_SIZE} total={query.data.total} onPage={setPage} />
        </>
      )}
    </Panel>
  );
}
