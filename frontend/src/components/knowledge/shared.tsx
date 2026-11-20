import { Badge } from "@/components/ui/display";
import { indexMeta, MATCH_META, STATUS_TONE } from "@/components/knowledge/knowledge";
import type { DocumentStatus, KnowledgeMatch } from "@/types/api";

export function MatchBadge({ match }: { match: KnowledgeMatch }) {
  const m = MATCH_META[match] ?? MATCH_META.keyword;
  return (
    <Badge tone={m.tone} title={m.hint}>
      {m.label}
    </Badge>
  );
}

export function IndexStatusBadge({ status, error }: { status: string; error?: string | null }) {
  const m = indexMeta(status);
  return (
    <Badge tone={m.tone} title={error ? `${m.hint}: ${error}` : m.hint}>
      {m.label}
    </Badge>
  );
}

export function DocumentStatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status as DocumentStatus] ?? "neutral"}>{status}</Badge>;
}
