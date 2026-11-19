import { useState } from "react";
import { agentLabel, formatArgs, formatDuration } from "@/components/investigation/shared";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, Table, Td, Th } from "@/components/ui/display";
import type { ToolExecution } from "@/types/api";
import { formatRelative } from "@/utils/format";

/** Tool runs with their redacted output in a dialog. */
export function ToolExecutionsTable({ rows }: { rows: ToolExecution[] }) {
  const [open, setOpen] = useState<ToolExecution | null>(null);
  if (!rows.length) return <EmptyState title="No diagnostics ran" description="Diagnostic tool runs from investigations appear here with their redacted output." />;
  return (
    <>
      <Table>
        <thead><tr><Th>Evidence</Th><Th>Tool</Th><Th>Resource</Th><Th>Status</Th><Th>Summary</Th><Th>Duration</Th><Th>When</Th></tr></thead>
        <tbody>
          {rows.map((x) => (
            <tr key={x.id} className="cursor-pointer hover:bg-surface-2/60" onClick={() => setOpen(x)}>
              <Td className="font-mono text-xs text-cyan-300">{x.evidence_id ?? "—"}</Td>
              <Td className="font-mono text-xs">{x.tool}</Td>
              <Td className="text-xs">{x.resource_name ?? "—"}</Td>
              <Td><Badge tone={x.status === "success" ? "success" : x.status === "rejected" ? "warning" : "danger"}>{x.status}</Badge></Td>
              <Td className="max-w-md truncate text-xs text-muted">{x.summary ?? x.error}</Td>
              <Td className="text-xs text-muted">{formatDuration(x.duration_ms)}</Td>
              <Td className="text-xs text-muted">{formatRelative(x.created_at)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <Dialog wide open={!!open} onOpenChange={(o) => !o && setOpen(null)} title={open ? `${open.tool}${open.evidence_id ? ` · ${open.evidence_id}` : ""}` : ""} description={open ? `${open.resource_name ?? ""} · ${open.actor ?? agentLabel(open.agent_type)}` : undefined}>
        {open && (
          <div className="space-y-2">
            <p className="text-sm">{open.summary}</p>
            {open.error && <Alert tone="warning">{open.error}</Alert>}
            <p className="font-mono text-[11px] text-subtle">arguments: {formatArgs(open.arguments) || "(resource only)"}</p>
            {open.output && (
              <pre className="max-h-[55vh] overflow-auto rounded-lg border border-border bg-black/40 p-3 font-mono text-[11px] whitespace-pre-wrap text-fg/85">{open.output}</pre>
            )}
          </div>
        )}
      </Dialog>
    </>
  );
}
