import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ArrowLeft, FileBarChart, Search } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { investigationsApi } from "@/api/endpoints";
import { confidencePercent } from "@/components/investigation/report";
import { RcaReport } from "@/components/investigation/RcaReport";
import { requestTypeLabel, ROOT_CAUSE_META } from "@/components/investigation/shared";
import { Badge, EmptyState, ErrorState, Pagination, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Input } from "@/components/ui/form";
import { formatDateTime } from "@/utils/format";

const PAGE_SIZE = 20;

/** Completed investigations, each with its RCA report. */
export function ReportsPage() {
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const query = useQuery({
    queryKey: ["reports", { page, q }],
    queryFn: () => investigationsApi.list({ page, page_size: PAGE_SIZE, status: "completed", q: q || undefined }),
    placeholderData: keepPreviousData,
  });

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Reports</h1>
        <p className="text-sm text-muted">Root cause analysis reports from completed investigations. Open one to print it or download it as Markdown.</p>
      </div>
      <Panel bodyClassName="px-0 pb-0">
        <div className="px-5 pb-4">
          <div className="relative max-w-md">
            <Search className="absolute top-2.5 left-3 size-4 text-subtle" />
            <Input
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setPage(1);
              }}
              placeholder="Search reports…"
              className="pl-9"
              aria-label="Search reports"
            />
          </div>
        </div>
        {query.isLoading ? (
          <Spinner />
        ) : query.error ? (
          <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />
        ) : !query.data?.items.length ? (
          <EmptyState icon={<FileBarChart className="size-8" />} title="No reports yet" description="A report is produced when an investigation completes." />
        ) : (
          <>
            <Table>
              <thead>
                <tr><Th>Report</Th><Th>Type</Th><Th>Root cause</Th><Th>Confidence</Th><Th>Completed</Th></tr>
              </thead>
              <tbody>
                {query.data.items.map((inv) => {
                  const status = inv.report?.root_cause_status;
                  const meta = status ? ROOT_CAUSE_META[status] : undefined;
                  return (
                    <tr key={inv.id}>
                      <Td>
                        <Link to={`/reports/${inv.id}`} className="font-medium hover:text-blue-300">{inv.title}</Link>
                        {inv.report?.conclusion && <p className="max-w-xl truncate text-xs text-muted">{inv.report.conclusion}</p>}
                      </Td>
                      <Td className="text-xs text-muted">{requestTypeLabel(inv.request_type)}</Td>
                      <Td className="max-w-sm">
                        {inv.report?.root_cause ? (
                          <div className="flex items-center gap-2">
                            {meta && <Badge tone={meta.tone}>{meta.label}</Badge>}
                            <span className="truncate text-xs">{inv.report.root_cause}</span>
                          </div>
                        ) : (
                          <span className="text-xs text-subtle">Not established</span>
                        )}
                      </Td>
                      <Td className="text-xs">{inv.report?.root_cause ? confidencePercent(inv.report.confidence) : "—"}</Td>
                      <Td className="text-xs text-muted">{inv.completed_at ? formatDateTime(inv.completed_at) : "—"}</Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
            <Pagination page={page} pageSize={PAGE_SIZE} total={query.data.total} onPage={setPage} />
          </>
        )}
      </Panel>
    </div>
  );
}

/** One report, laid out for printing. */
export function ReportDetailPage() {
  const { id = "" } = useParams();
  const query = useQuery({ queryKey: ["investigation", id], queryFn: () => investigationsApi.get(id) });
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const inv = query.data;
  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between print:hidden">
        <Link to="/reports" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg"><ArrowLeft className="size-4" /> Reports</Link>
        <Link to={`/investigations/${inv.id}`} className="text-sm text-blue-300 hover:underline">Open investigation</Link>
      </div>
      <div>
        <h1 className="text-xl font-semibold">{inv.title}</h1>
        <p className="text-sm text-muted">{inv.request}</p>
        <p className="mt-1 text-xs text-subtle">
          {inv.username && <>Requested by {inv.username} · </>}started {formatDateTime(inv.started_at)}
          {inv.completed_at && <> · completed {formatDateTime(inv.completed_at)}</>}
        </p>
      </div>
      {inv.status === "completed" && inv.result.conclusion ? (
        <RcaReport inv={inv} printable />
      ) : (
        <Panel><EmptyState title="No report" description="This investigation has not completed, so it has no report yet." /></Panel>
      )}
    </div>
  );
}
