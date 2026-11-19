import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Ban,
  CheckCircle2,
  ClipboardCheck,
  Clock,
  ExternalLink,
  MessageSquareDiff,
  ShieldAlert,
  XCircle,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { approvalsApi, type ApprovalQuery } from "@/api/endpoints";
import { formatArgs } from "@/components/investigation/shared";
import { RISK_META, timeLeft } from "@/components/remediation/remediation";
import { ApprovalStatusBadge, ExecutionDetail, RiskBadge } from "@/components/remediation/shared";
import { Button } from "@/components/ui/button";
import { Alert, Badge, EmptyState, ErrorState, Pagination, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Select, Textarea } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { ApprovalDetail, ApprovalStatus, RiskLevel } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatDateTime, formatRelative } from "@/utils/format";

const PAGE_SIZE = 20;
type Tab = "pending" | "mine" | "history";

function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export function ApprovalsPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = (params.get("tab") as Tab | null) ?? "pending";
  const risk = (params.get("risk") as RiskLevel | null) ?? undefined;
  const [page, setPage] = useState(1);
  const now = useNow();
  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
    setPage(1);
  };
  const summary = useQuery({ queryKey: ["approvals", "summary"], queryFn: approvalsApi.summary, refetchInterval: 15_000 });
  const filter: ApprovalQuery = {
    page,
    page_size: PAGE_SIZE,
    risk,
    status: tab === "pending" ? "pending" : undefined,
    mine: tab === "mine" || undefined,
  };
  const query = useQuery({
    queryKey: ["approvals", filter],
    queryFn: () => approvalsApi.list(filter),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });
  const s = summary.data;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">Approvals</h1>
        <p className="text-sm text-muted">
          Remediation actions proposed by investigations. Nothing changes a system until it is approved here (or a
          policy allows it to run unattended), and every decision is audited.
        </p>
      </div>

      <div className="flex flex-wrap gap-3">
        <Stat label="Pending" value={s?.pending ?? 0} tone="text-amber-400" />
        <Stat label="Pending for my investigations" value={s?.pending_for_me ?? 0} tone="text-fg" />
        <Stat label="Expiring within 15 min" value={s?.expiring_soon ?? 0} tone="text-red-400" />
        {(["critical", "high", "medium", "low"] as RiskLevel[]).map((r) => (
          <Stat key={r} label={`${RISK_META[r].label} pending`} value={s?.by_risk[r] ?? 0} tone={r === "low" ? "text-green-400" : r === "medium" ? "text-amber-400" : "text-red-400"} />
        ))}
      </div>

      <Panel bodyClassName="px-0 pb-0">
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 pb-4">
          <div className="flex gap-1 rounded-lg border border-border bg-surface-2 p-1" role="tablist">
            {(
              [
                ["pending", "Pending"],
                ["mine", "My requests"],
                ["history", "All requests"],
              ] as [Tab, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                role="tab"
                aria-selected={tab === key}
                onClick={() => set("tab", key === "pending" ? "" : key)}
                className={cn("rounded-md px-3 py-1 text-sm", tab === key ? "bg-surface-3 text-fg" : "text-muted hover:text-fg")}
              >
                {label}
              </button>
            ))}
          </div>
          <Select value={risk ?? ""} onChange={(e) => set("risk", e.target.value)} className="w-40" aria-label="Risk">
            <option value="">Any risk</option>
            {(["critical", "high", "medium", "low"] as RiskLevel[]).map((r) => <option key={r} value={r}>{RISK_META[r].label}</option>)}
          </Select>
        </div>
        {query.isLoading ? (
          <Spinner />
        ) : query.error ? (
          <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />
        ) : !query.data?.items.length ? (
          <EmptyState
            icon={<ClipboardCheck className="size-8" />}
            title={tab === "pending" ? "Nothing waiting for approval" : "No approval requests"}
            description="Approval requests appear when an investigation proposes a remediation action."
          />
        ) : (
          <>
            <Table>
              <thead>
                <tr><Th>Action</Th><Th>Resource</Th><Th>Risk</Th><Th>Status</Th><Th>Requested by</Th><Th>Requested</Th><Th>Expires / decided</Th></tr>
              </thead>
              <tbody>
                {query.data.items.map((a) => (
                  <tr key={a.id} className="cursor-pointer hover:bg-surface-2/60" onClick={() => navigate(`/approvals/${a.id}`)}>
                    <Td className="max-w-md">
                      <div className="truncate text-sm">{a.description}</div>
                      <div className="font-mono text-[11px] text-subtle">{a.action}</div>
                    </Td>
                    <Td className="text-xs">{a.resource_name ?? "—"}</Td>
                    <Td><RiskBadge risk={a.risk_level} /></Td>
                    <Td><ApprovalStatusBadge status={a.status} auto={a.auto_approved} /></Td>
                    <Td className="text-xs">{a.requested_by_username ?? "—"}</Td>
                    <Td className="text-xs text-muted">{formatRelative(a.created_at, now)}</Td>
                    <Td className="text-xs text-muted">
                      {a.status === "pending" ? (
                        <span className={cn(new Date(a.expires_at).getTime() - now < 15 * 60_000 && "text-red-300")}>{timeLeft(a.expires_at, now)}</span>
                      ) : (
                        `${a.approved_by_username ?? (a.auto_approved ? "policy" : "—")} · ${formatRelative(a.decided_at, now)}`
                      )}
                    </Td>
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

function Stat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="panel flex min-w-28 flex-1 flex-col px-4 py-3">
      <span className={cn("font-mono text-2xl font-semibold tabular-nums", tone)}>{value}</span>
      <span className="text-xs text-muted">{label}</span>
    </div>
  );
}

// ------------------------------------------------------------------ detail

const DECIDED_TEXT: Partial<Record<ApprovalStatus, string>> = {
  approved: "Approved; the investigation resumes and runs the action.",
  executed: "Approved and executed.",
  failed: "Approved, but the action did not complete.",
  rejected: "Rejected; the action was not run.",
  changes_requested: "Changes were requested; the planner revises the step.",
  cancelled: "Cancelled; the action was not run.",
  expired: "Expired before anyone decided; the action was not run.",
};

function DecisionPanel({ a }: { a: ApprovalDetail }) {
  const queryClient = useQueryClient();
  const user = useAuthStore((s) => s.user);
  const canApprove = useAuthStore((s) => s.hasPermission("approval.approve"));
  const [comment, setComment] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject" | "changes" | "cancel") => {
      const c = comment.trim() || undefined;
      if (decision === "approve") return approvalsApi.approve(a.id, c);
      if (decision === "reject") return approvalsApi.reject(a.id, c);
      if (decision === "changes") return approvalsApi.requestChanges(a.id, comment.trim());
      return approvalsApi.cancel(a.id, c);
    },
    onSuccess: () => {
      setComment("");
      void queryClient.invalidateQueries({ queryKey: ["approval", a.id] });
      void queryClient.invalidateQueries({ queryKey: ["approvals"] });
    },
  });
  const isRequester = user?.id === a.requested_by;
  if (a.status !== "pending") {
    return (
      <Panel title="Decision">
        <div className="space-y-2 text-sm">
          <ApprovalStatusBadge status={a.status} auto={a.auto_approved} />
          <p className="text-fg/85">{DECIDED_TEXT[a.status]}</p>
          <p className="text-xs text-muted">
            {a.auto_approved ? "Auto-approved by policy" : `By ${a.approved_by_username ?? "—"}`} · {formatDateTime(a.decided_at)}
          </p>
          {a.decision_comment && <blockquote className="border-l-2 border-border-strong pl-3 text-sm text-fg/80">{a.decision_comment}</blockquote>}
        </div>
      </Panel>
    );
  }
  const busy = decide.isPending ? decide.variables : null;
  return (
    <Panel title="Decision">
      <div className="space-y-3">
        {!a.can_decide && a.cannot_decide_reason && <Alert tone="warning">{a.cannot_decide_reason}</Alert>}
        <Field label="Comment" htmlFor="decision-comment" hint="Required when requesting changes; recorded in the audit log.">
          <Textarea id="decision-comment" value={comment} onChange={(e) => setComment(e.target.value)} rows={3} maxLength={2000} placeholder="Reason, conditions or requested changes…" />
        </Field>
        <div className="flex flex-wrap gap-2">
          {canApprove && (
            <>
              <Button onClick={() => decide.mutate("approve")} disabled={!a.can_decide || !!busy} loading={busy === "approve"}>
                <CheckCircle2 /> Approve
              </Button>
              <Button variant="danger" onClick={() => decide.mutate("reject")} disabled={!a.can_decide || !!busy} loading={busy === "reject"}>
                <XCircle /> Reject
              </Button>
              <Button variant="secondary" onClick={() => decide.mutate("changes")} disabled={!a.can_decide || comment.trim().length < 3 || !!busy} loading={busy === "changes"}>
                <MessageSquareDiff /> Request changes
              </Button>
            </>
          )}
          {(isRequester || canApprove) && (
            <Button variant="ghost" onClick={() => decide.mutate("cancel")} disabled={!!busy} loading={busy === "cancel"}>
              <Ban /> Cancel request
            </Button>
          )}
        </div>
        {decide.error && <Alert>{errorMessage(decide.error)}</Alert>}
      </div>
    </Panel>
  );
}

export function ApprovalDetailPage() {
  const { id = "" } = useParams();
  const now = useNow(10_000);
  const query = useQuery({
    queryKey: ["approval", id],
    queryFn: () => approvalsApi.get(id),
    refetchInterval: (q) => (q.state.data?.status === "pending" || q.state.data?.status === "approved" ? 5_000 : false),
  });
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const a = query.data;
  const policy = a.policy ?? {};
  return (
    <div className="space-y-5">
      <Link to="/approvals" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
        <ArrowLeft className="size-4" /> Approvals
      </Link>
      <div className="panel flex flex-wrap items-start justify-between gap-4 px-5 py-4">
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <RiskBadge risk={a.risk_level} />
            <ApprovalStatusBadge status={a.status} auto={a.auto_approved} />
            {a.status === "pending" && (
              <Badge tone={new Date(a.expires_at).getTime() - now < 15 * 60_000 ? "danger" : "neutral"}>
                <Clock className="size-3" /> expires {timeLeft(a.expires_at, now)}
              </Badge>
            )}
          </div>
          <h1 className="text-lg font-semibold">{a.description}</h1>
          <p className="text-xs text-muted">
            Requested by <span className="text-fg">{a.requested_by_username ?? "—"}</span> · {formatDateTime(a.created_at)}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
        <div className="space-y-5">
          <Panel title="Operation">
            <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-2 text-sm">
              <dt className="text-muted">Tool</dt>
              <dd className="font-mono text-xs">{a.action}</dd>
              <dt className="text-muted">Target</dt>
              <dd>
                {a.resource_id ? (
                  <Link to={`/infrastructure/${a.resource_id}`} className="text-blue-300 hover:underline">{a.resource_name}</Link>
                ) : (
                  a.resource_name ?? "—"
                )}
              </dd>
              <dt className="text-muted">Parameters</dt>
              <dd className="font-mono text-xs break-all">{formatArgs(a.action_parameters) || "(resource only)"}</dd>
              <dt className="text-muted">Rationale</dt>
              <dd className="text-fg/90">{a.rationale || "—"}</dd>
              <dt className="text-muted">Expected impact</dt>
              <dd className="text-amber-200/90">{a.expected_impact || "Not stated"}</dd>
            </dl>
          </Panel>

          <Panel title={`Cited evidence (${a.evidence.length})`}>
            {a.evidence.length ? (
              <ul className="space-y-3">
                {a.evidence.map((e) => (
                  <li key={e.id} className="rounded-lg border border-border bg-surface-2 p-3">
                    <div className="flex items-center gap-2 text-xs">
                      <span className="font-mono text-cyan-300">{e.id}</span>
                      <span className="font-mono text-muted">{e.tool}</span>
                      {e.resource && <span className="text-subtle">on {e.resource}</span>}
                    </div>
                    <p className="mt-1 text-sm">{e.summary}</p>
                    {e.excerpt && (
                      <pre className="mt-2 max-h-48 overflow-auto rounded border border-border bg-black/40 p-2 font-mono text-[11px] whitespace-pre-wrap text-fg/80">{e.excerpt}</pre>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted">{a.evidence_ids.length ? a.evidence_ids.join(", ") : "No evidence cited."}</p>
            )}
          </Panel>

          {a.execution && (
            <Panel title="Execution">
              <ExecutionDetail job={a.execution} />
            </Panel>
          )}
        </div>

        <div className="space-y-5">
          <DecisionPanel a={a} />
          <Panel title="Policy">
            <div className="space-y-2 text-sm">
              <p>
                <span className="text-muted">Risk:</span> {RISK_META[a.risk_level]?.label}
              </p>
              <p>
                <span className="text-muted">Matched rules:</span>{" "}
                {policy.matched_rules?.length ? policy.matched_rules.join(", ") : "none (tool default)"}
              </p>
              <p>
                <span className="text-muted">Self-approval:</span> {policy.allow_self_approval ? "allowed by policy" : "not allowed (four-eyes)"}
              </p>
              <p>
                <span className="text-muted">Approval valid for:</span> {policy.approval_ttl_minutes ?? "—"} min
              </p>
              {policy.notes?.map((n) => (
                <p key={n} className="flex gap-1.5 text-xs text-amber-200/80"><ShieldAlert className="mt-0.5 size-3 shrink-0" />{n}</p>
              ))}
            </div>
          </Panel>
          <Panel title="Context">
            <div className="space-y-2 text-sm">
              <Link to={`/investigations/${a.investigation_id}`} className="flex items-center gap-1.5 text-blue-300 hover:underline">
                <ExternalLink className="size-3.5" /> Investigation{a.investigation_title ? `: ${a.investigation_title}` : ""}
              </Link>
              {a.incident_id && (
                <Link to={`/incidents/${a.incident_id}`} className="flex items-center gap-1.5 text-blue-300 hover:underline">
                  <ExternalLink className="size-3.5" /> Incident {a.incident_key}
                </Link>
              )}
              <p className="text-xs text-muted">Plan step {a.step_index + 1}</p>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  );
}
