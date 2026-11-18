import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Lightbulb, Plus, Send, Sparkles } from "lucide-react";
import { useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { investigationsApi, modelsApi } from "@/api/endpoints";
import { InvestigationWorkspace } from "@/components/investigation/InvestigationWorkspace";
import { InvestigationStatusBadge } from "@/components/investigation/shared";
import { Button } from "@/components/ui/button";
import { Alert, Panel, Spinner } from "@/components/ui/display";
import { Select } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { ExecutionMode } from "@/types/api";
import { cn } from "@/utils/cn";
import { formatRelative } from "@/utils/format";

const EXAMPLES = [
  "Why is the payroll application slow since 9am?",
  "Users report 502 errors on the customer portal. What should we check?",
  "What would be impacted if we reboot db-prod-01 tonight?",
  "Disk usage alert on the backup server — investigate.",
];

function Composer({ onStarted }: { onStarted: (id: string) => void }) {
  const [request, setRequest] = useState("");
  const [mode, setMode] = useState<ExecutionMode>("assisted");
  const queryClient = useQueryClient();
  const canReadSettings = useAuthStore((s) => s.hasPermission("settings.read"));
  const models = useQuery({ queryKey: ["models"], queryFn: modelsApi.list, enabled: canReadSettings });
  const noModel = canReadSettings && models.data && !models.data.some((m) => m.is_default && m.enabled);

  const mutation = useMutation({
    mutationFn: () => investigationsApi.create({ request: request.trim(), execution_mode: mode }),
    onSuccess: (inv) => {
      setRequest("");
      void queryClient.invalidateQueries({ queryKey: ["investigations"] });
      onStarted(inv.id);
    },
  });
  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (request.trim().length >= 3 && !mutation.isPending) mutation.mutate();
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
  };

  return (
    <div className="mx-auto w-full max-w-3xl space-y-5 pt-6">
      <div className="text-center">
        <div className="mx-auto grid size-14 place-items-center rounded-2xl bg-gradient-to-br from-violet-600 to-blue-600 shadow-lg shadow-violet-600/30">
          <Sparkles className="size-7 text-white" />
        </div>
        <h1 className="mt-4 text-2xl font-semibold">What should we investigate?</h1>
        <p className="mt-1 text-sm text-muted">
          Describe a symptom, an incident or a planned change. The Supervisor agent classifies the request,
          plans the investigation and streams its progress here.
        </p>
      </div>

      {noModel && (
        <Alert tone="warning">
          No default AI model is configured.{" "}
          <Link to="/settings/models" className="underline">Add one under Settings → AI Models</Link> before starting.
        </Alert>
      )}

      <form onSubmit={submit} className="panel overflow-hidden focus-within:border-primary/60">
        <textarea
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          onKeyDown={onKey}
          rows={4}
          maxLength={4000}
          placeholder="e.g. The payroll API has been timing out for some users since this morning…"
          className="block w-full resize-none bg-transparent px-4 py-3 text-sm text-fg placeholder:text-subtle focus:outline-none"
          aria-label="Investigation request"
          autoFocus
        />
        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border bg-surface-2/50 px-3 py-2">
          <div className="flex items-center gap-2 text-xs text-muted">
            <label htmlFor="mode">Mode</label>
            <Select id="mode" value={mode} onChange={(e) => setMode(e.target.value as ExecutionMode)} className="h-8 w-auto text-xs">
              <option value="assisted">Assisted — read-only diagnostics run automatically</option>
              <option value="manual">Manual — recommendations only</option>
              <option value="autonomous" disabled>Autonomous — requires approval engine (later)</option>
            </Select>
          </div>
          <div className="flex items-center gap-3">
            <span className="hidden text-[11px] text-subtle sm:inline">Ctrl + Enter</span>
            <Button type="submit" loading={mutation.isPending} disabled={request.trim().length < 3}>
              <Send /> Investigate
            </Button>
          </div>
        </div>
      </form>
      {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}

      <div className="grid gap-2 sm:grid-cols-2">
        {EXAMPLES.map((ex) => (
          <button
            key={ex}
            type="button"
            onClick={() => setRequest(ex)}
            className="flex items-start gap-2 rounded-lg border border-border bg-surface/60 p-3 text-left text-sm text-muted transition-colors hover:border-border-strong hover:text-fg"
          >
            <Lightbulb className="mt-0.5 size-4 shrink-0 text-amber-400" /> {ex}
          </button>
        ))}
      </div>
    </div>
  );
}

function History({ current, onSelect }: { current: string | null; onSelect: (id: string | null) => void }) {
  const query = useQuery({
    queryKey: ["investigations", "assistant-history"],
    queryFn: () => investigationsApi.list({ page: 1, page_size: 15 }),
    refetchInterval: 15_000,
  });
  return (
    <Panel
      title="Recent"
      actions={
        <Button variant="ghost" size="sm" onClick={() => onSelect(null)}>
          <Plus /> New
        </Button>
      }
      className="h-fit"
      bodyClassName="px-2"
    >
      {query.isLoading ? (
        <Spinner />
      ) : !query.data?.items.length ? (
        <p className="px-3 py-4 text-xs text-subtle">Your investigations will appear here.</p>
      ) : (
        <ul className="space-y-1">
          {query.data.items.map((inv) => (
            <li key={inv.id}>
              <button
                type="button"
                onClick={() => onSelect(inv.id)}
                className={cn(
                  "w-full rounded-lg px-3 py-2 text-left transition-colors",
                  current === inv.id ? "bg-surface-3" : "hover:bg-surface-2",
                )}
              >
                <p className="truncate text-sm text-fg">{inv.title}</p>
                <div className="mt-1 flex items-center justify-between gap-2">
                  <InvestigationStatusBadge status={inv.status} />
                  <span className="text-[11px] text-subtle">{formatRelative(inv.started_at)}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

export function AssistantPage() {
  const [params, setParams] = useSearchParams();
  const current = params.get("id");
  const select = (id: string | null) => setParams(id ? { id } : {});
  return (
    <div className="grid gap-6 lg:grid-cols-[16rem_minmax(0,1fr)]">
      <div className="order-2 lg:order-1">
        <History current={current} onSelect={select} />
      </div>
      <div className="order-1 min-w-0 lg:order-2">
        {current ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between text-xs text-muted">
              <span className="flex items-center gap-1.5"><Bot className="size-3.5" /> AI Assistant</span>
              <Link to={`/investigations/${current}`} className="text-blue-400 hover:underline">Open in Investigations →</Link>
            </div>
            <InvestigationWorkspace id={current} compact />
          </div>
        ) : (
          <Composer onStarted={select} />
        )}
      </div>
    </div>
  );
}
