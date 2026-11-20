import type { Tone } from "@/components/ui/display";
import type {
  DocumentStatus,
  EditableDocumentType,
  IndexStatus,
  KnowledgeDocument,
  KnowledgeDocumentType,
  KnowledgeMatch,
} from "@/types/api";

export const DOCUMENT_TYPE_LABEL: Record<KnowledgeDocumentType, string> = {
  runbook: "Runbook",
  troubleshooting: "Troubleshooting",
  documentation: "Documentation",
  procedure: "Procedure",
  incident: "Past incident",
  investigation: "Past investigation",
  other: "Other",
};

export const EDITABLE_TYPES: EditableDocumentType[] = ["runbook", "troubleshooting", "documentation", "procedure", "other"];
export const DOCUMENT_TYPES = Object.keys(DOCUMENT_TYPE_LABEL) as KnowledgeDocumentType[];
export const DOCUMENT_STATUSES: DocumentStatus[] = ["active", "draft", "deprecated"];
export const INDEX_STATUSES: IndexStatus[] = ["indexed", "keyword_only", "pending", "failed"];
export const SOURCE_LABEL: Record<KnowledgeDocument["source"], string> = {
  upload: "Uploaded file",
  manual: "Written in LocalOps",
  incident: "Resolved incident",
  investigation: "Completed investigation",
};
export const UPLOAD_ACCEPT = ".md,.markdown,.txt,.log,.csv,.json,.pdf";

export const INDEX_STATUS_META: Record<IndexStatus, { label: string; tone: Tone; hint: string }> = {
  indexed: { label: "Indexed", tone: "success", hint: "Searchable by keyword and meaning" },
  keyword_only: { label: "Keyword only", tone: "warning", hint: "keyword search only (no embedding model, or it was unavailable)" },
  pending: { label: "Pending", tone: "neutral", hint: "Waiting to be indexed" },
  failed: { label: "Failed", tone: "danger", hint: "Indexing failed; see the error and re-index" },
};

export const STATUS_TONE: Record<DocumentStatus, Tone> = { active: "success", draft: "neutral", deprecated: "warning" };

export const MATCH_META: Record<KnowledgeMatch, { label: string; tone: Tone; hint: string }> = {
  both: { label: "keyword + semantic", tone: "violet", hint: "Matched both by words and by meaning" },
  semantic: { label: "semantic", tone: "primary", hint: "Matched by meaning (embedding similarity)" },
  keyword: { label: "keyword", tone: "neutral", hint: "Matched by words (full-text search)" },
};

export function indexMeta(status: string) {
  return INDEX_STATUS_META[status as IndexStatus] ?? { label: status, tone: "neutral" as Tone, hint: "" };
}

export function typeLabel(type: string): string {
  return DOCUMENT_TYPE_LABEL[type as KnowledgeDocumentType] ?? type;
}

/** "Disk, linux,, Disk " → ["disk", "linux"] (lower-case, de-duplicated, like the API). */
export function parseTags(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[,\n]/)) {
    const tag = raw.trim().replace(/\s+/g, " ").toLowerCase().slice(0, 60);
    if (tag && !out.includes(tag)) out.push(tag);
  }
  return out.slice(0, 30);
}

export function isGenerated(doc: Pick<KnowledgeDocument, "source">): boolean {
  return doc.source === "incident" || doc.source === "investigation";
}

/** Link to the incident / investigation a generated document was copied from. */
export function sourceLink(doc: Pick<KnowledgeDocument, "source" | "source_id">): string | null {
  if (!doc.source_id) return null;
  if (doc.source === "incident") return `/incidents/${doc.source_id}`;
  if (doc.source === "investigation") return `/investigations/${doc.source_id}`;
  return null;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Title suggested from an uploaded file name: "db-pool_runbook.md" → "db pool runbook". */
export function titleFromFilename(name: string): string {
  return name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " ").trim();
}
