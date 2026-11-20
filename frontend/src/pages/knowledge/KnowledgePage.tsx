import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, FilePlus2, RefreshCw, Search, ThumbsDown, ThumbsUp, Upload } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { knowledgeApi } from "@/api/endpoints";
import {
  DOCUMENT_STATUSES,
  DOCUMENT_TYPES,
  EDITABLE_TYPES,
  INDEX_STATUS_META,
  INDEX_STATUSES,
  SOURCE_LABEL,
  parseTags,
  titleFromFilename,
  typeLabel,
  UPLOAD_ACCEPT,
} from "@/components/knowledge/knowledge";
import { DocumentStatusBadge, IndexStatusBadge, MatchBadge } from "@/components/knowledge/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Pagination, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { EditableDocumentType, KnowledgeDocument, KnowledgeHit, KnowledgeSearchResponse } from "@/types/api";
import { formatDateTime, formatRelative } from "@/utils/format";

const PAGE_SIZE = 20;
const TABS = ["documents", "search", "statistics"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABEL: Record<Tab, string> = { documents: "Documents", search: "Search", statistics: "Statistics" };

// ------------------------------------------------------------------ dialogs

function UploadDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [type, setType] = useState<EditableDocumentType>("runbook");
  const [tags, setTags] = useState("");
  const [status, setStatus] = useState<"active" | "draft">("active");
  const mutation = useMutation({
    mutationFn: () =>
      knowledgeApi.upload({ file: file!, title: title.trim() || undefined, document_type: type, tags: parseTags(tags), status }),
    onSuccess: (doc) => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge"] });
      onOpenChange(false);
      navigate(`/knowledge/${doc.id}`);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (file) mutation.mutate();
  };
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Upload a document"
      description="Markdown, text, logs, CSV, JSON or PDF. The text is extracted, chunked and indexed for search."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="kb-upload" loading={mutation.isPending} disabled={!file}>
            <Upload /> Upload
          </Button>
        </>
      }
    >
      <form id="kb-upload" onSubmit={submit} className="space-y-4">
        <Field label="File" htmlFor="kb-file">
          <Input
            id="kb-file"
            type="file"
            accept={UPLOAD_ACCEPT}
            onChange={(e) => {
              const f = e.target.files?.[0] ?? null;
              setFile(f);
              if (f && !title) setTitle(titleFromFilename(f.name));
            }}
            required
          />
        </Field>
        <Field label="Title" htmlFor="kb-u-title" hint="Defaults to the file name.">
          <Input id="kb-u-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Type" htmlFor="kb-u-type">
            <Select id="kb-u-type" value={type} onChange={(e) => setType(e.target.value as EditableDocumentType)}>
              {EDITABLE_TYPES.map((t) => <option key={t} value={t}>{typeLabel(t)}</option>)}
            </Select>
          </Field>
          <Field label="Status" htmlFor="kb-u-status" hint="Drafts rank lower in search.">
            <Select id="kb-u-status" value={status} onChange={(e) => setStatus(e.target.value as "active" | "draft")}>
              <option value="active">Active</option>
              <option value="draft">Draft</option>
            </Select>
          </Field>
        </div>
        <Field label="Tags" htmlFor="kb-u-tags" hint="Comma separated.">
          <Input id="kb-u-tags" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="database, payroll" />
        </Field>
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}

function NewDocumentDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [type, setType] = useState<EditableDocumentType>("runbook");
  const [category, setCategory] = useState("");
  const [tags, setTags] = useState("");
  const [content, setContent] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      knowledgeApi.create({
        title: title.trim(),
        document_type: type,
        category: category.trim() || null,
        tags: parseTags(tags),
        content,
      }),
    onSuccess: (doc) => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge"] });
      onOpenChange(false);
      navigate(`/knowledge/${doc.id}`);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };
  return (
    <Dialog
      wide
      open={open}
      onOpenChange={onOpenChange}
      title="New document"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="kb-new" loading={mutation.isPending} disabled={!title.trim() || !content.trim()}>
            Create
          </Button>
        </>
      }
    >
      <form id="kb-new" onSubmit={submit} className="space-y-4">
        <Field label="Title" htmlFor="kb-n-title">
          <Input id="kb-n-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} autoFocus required />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label="Type" htmlFor="kb-n-type">
            <Select id="kb-n-type" value={type} onChange={(e) => setType(e.target.value as EditableDocumentType)}>
              {EDITABLE_TYPES.map((t) => <option key={t} value={t}>{typeLabel(t)}</option>)}
            </Select>
          </Field>
          <Field label="Category" htmlFor="kb-n-cat">
            <Input id="kb-n-cat" value={category} onChange={(e) => setCategory(e.target.value)} maxLength={100} />
          </Field>
          <Field label="Tags" htmlFor="kb-n-tags" hint="Comma separated.">
            <Input id="kb-n-tags" value={tags} onChange={(e) => setTags(e.target.value)} />
          </Field>
        </div>
        <Field label="Content (Markdown)" htmlFor="kb-n-content" hint="Use ## headings: each section becomes a separately searchable chunk.">
          <Textarea id="kb-n-content" value={content} onChange={(e) => setContent(e.target.value)} rows={16} className="font-mono text-xs" required />
        </Field>
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}

// ------------------------------------------------------------------ documents tab

function DocumentsTab() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [page, setPage] = useState(1);
  const q = params.get("q") ?? "";
  const type = params.get("type") ?? "";
  const status = params.get("status") ?? "";
  const source = params.get("source") ?? "";
  const index = params.get("index") ?? "";
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
    setPage(1);
  };
  const query = useQuery({
    queryKey: ["knowledge", "list", { page, q, type, status, source, index }],
    queryFn: () =>
      knowledgeApi.list({
        page,
        page_size: PAGE_SIZE,
        q: q || undefined,
        document_type: type || undefined,
        status: status || undefined,
        source: source || undefined,
        index_status: index || undefined,
      }),
    placeholderData: keepPreviousData,
    refetchInterval: (qr) => (qr.state.data?.items.some((d) => d.index_status === "pending") ? 5_000 : false),
  });
  const filtered = !!(q || type || status || source || index);
  return (
    <Panel bodyClassName="px-0 pb-0">
      <div className="flex flex-wrap gap-3 px-5 pb-4">
        <div className="relative min-w-60 flex-1">
          <Search className="absolute top-2.5 left-3 size-4 text-subtle" />
          <Input value={q} onChange={(e) => setParam("q", e.target.value)} placeholder="Filter by title…" className="pl-9" aria-label="Filter documents" />
        </div>
        <Select value={type} onChange={(e) => setParam("type", e.target.value)} className="w-44" aria-label="Type">
          <option value="">Any type</option>
          {DOCUMENT_TYPES.map((t) => <option key={t} value={t}>{typeLabel(t)}</option>)}
        </Select>
        <Select value={status} onChange={(e) => setParam("status", e.target.value)} className="w-36" aria-label="Status">
          <option value="">Any status</option>
          {DOCUMENT_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
        </Select>
        <Select value={source} onChange={(e) => setParam("source", e.target.value)} className="w-48" aria-label="Source">
          <option value="">Any source</option>
          {Object.entries(SOURCE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </Select>
        <Select value={index} onChange={(e) => setParam("index", e.target.value)} className="w-40" aria-label="Index status">
          <option value="">Any index state</option>
          {INDEX_STATUSES.map((s) => <option key={s} value={s}>{INDEX_STATUS_META[s].label}</option>)}
        </Select>
      </div>
      {query.isLoading ? (
        <Spinner />
      ) : query.error || !query.data ? (
        <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />
      ) : !query.data.items.length ? (
        <EmptyState
          icon={<BookOpen className="size-8" />}
          title="No documents"
          description={filtered ? "Nothing matches the filters." : "Upload runbooks and documentation. Resolved incidents and completed investigations are added automatically."}
        />
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th>Document</Th>
                <Th>Type</Th>
                <Th>Status</Th>
                <Th>Index</Th>
                <Th>Version</Th>
                <Th>Chunks</Th>
                <Th>Updated</Th>
              </tr>
            </thead>
            <tbody>
              {query.data.items.map((d: KnowledgeDocument) => (
                <tr key={d.id} className="cursor-pointer transition-colors hover:bg-surface-2/60" onClick={() => navigate(`/knowledge/${d.id}`)}>
                  <Td className="max-w-md">
                    <Link to={`/knowledge/${d.id}`} className="block truncate font-medium text-fg hover:text-blue-300" onClick={(e) => e.stopPropagation()}>
                      {d.title}
                    </Link>
                    <p className="truncate text-xs text-muted">
                      {SOURCE_LABEL[d.source] ?? d.source}
                      {d.tags.length > 0 && <> · {d.tags.join(", ")}</>}
                    </p>
                  </Td>
                  <Td className="text-xs">{typeLabel(d.document_type)}</Td>
                  <Td>
                    <div className="flex items-center gap-1">
                      <DocumentStatusBadge status={d.status} />
                      {d.validated_at && <Badge tone="success">validated</Badge>}
                    </div>
                  </Td>
                  <Td><IndexStatusBadge status={d.index_status} error={d.index_error} /></Td>
                  <Td className="font-mono text-xs text-muted">v{d.version}</Td>
                  <Td className="font-mono text-xs text-muted">{d.chunk_count}</Td>
                  <Td className="text-xs text-muted" title={formatDateTime(d.updated_at ?? d.created_at)}>{formatRelative(d.updated_at ?? d.created_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <Pagination page={page} pageSize={PAGE_SIZE} total={query.data.total} onPage={setPage} />
        </>
      )}
    </Panel>
  );
}

// ------------------------------------------------------------------ search tab

function Feedback({ hit }: { hit: KnowledgeHit }) {
  const [sent, setSent] = useState<boolean | null>(null);
  const mutation = useMutation({
    mutationFn: (helpful: boolean) => knowledgeApi.feedback(hit.document_id, { helpful, chunk_id: hit.chunk_id }),
    onSuccess: (_d, helpful) => setSent(helpful),
  });
  if (sent !== null) return <span className="text-xs text-subtle">Thanks for the feedback</span>;
  return (
    <div className="flex items-center gap-1">
      <Button variant="ghost" size="sm" aria-label="Helpful" title="Helpful" onClick={() => mutation.mutate(true)} disabled={mutation.isPending}>
        <ThumbsUp />
      </Button>
      <Button variant="ghost" size="sm" aria-label="Not helpful" title="Not helpful" onClick={() => mutation.mutate(false)} disabled={mutation.isPending}>
        <ThumbsDown />
      </Button>
      {mutation.error && <span className="text-xs text-red-300">{errorMessage(mutation.error)}</span>}
    </div>
  );
}

function SearchTab() {
  const [text, setText] = useState("");
  const [type, setType] = useState("");
  const [deprecated, setDeprecated] = useState(false);
  const mutation = useMutation<KnowledgeSearchResponse, unknown, void>({
    mutationFn: () =>
      knowledgeApi.search({
        query: text.trim(),
        document_types: type ? [type] : undefined,
        include_deprecated: deprecated || undefined,
        limit: 20,
      }),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (text.trim()) mutation.mutate();
  };
  const res = mutation.data;
  return (
    <div className="space-y-4">
      <Panel>
        <form onSubmit={submit} className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-72 flex-1">
            <Search className="absolute top-2.5 left-3 size-4 text-subtle" />
            <Input
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="Describe the symptom, e.g. “postgres too many connections”"
              className="pl-9"
              aria-label="Search the knowledge base"
              maxLength={2000}
            />
          </div>
          <Select value={type} onChange={(e) => setType(e.target.value)} className="w-44" aria-label="Type">
            <option value="">Any type</option>
            {DOCUMENT_TYPES.map((t) => <option key={t} value={t}>{typeLabel(t)}</option>)}
          </Select>
          <label className="flex items-center gap-2 text-xs text-muted">
            <input type="checkbox" className="size-4" checked={deprecated} onChange={(e) => setDeprecated(e.target.checked)} />
            Include deprecated
          </label>
          <Button type="submit" loading={mutation.isPending} disabled={!text.trim()}>Search</Button>
        </form>
      </Panel>
      {mutation.error ? <Alert>{errorMessage(mutation.error)}</Alert> : null}
      {res && (
        <>
          {res.mode === "keyword" && res.note && <Alert tone="warning">{res.note}</Alert>}
          {res.mode === "hybrid" && res.note && <Alert tone="primary">{res.note}</Alert>}
          {!res.hits.length ? (
            <Panel><EmptyState title="No matches" description="Try other words, or describe the symptom differently." /></Panel>
          ) : (
            <ul className="space-y-3">
              {res.hits.map((h) => (
                <li key={h.chunk_id} className="panel p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Link to={`/knowledge/${h.document_id}`} className="font-medium text-fg hover:text-blue-300">{h.title}</Link>
                    <Badge>{typeLabel(h.document_type)}</Badge>
                    <MatchBadge match={h.match} />
                    {h.validated && <Badge tone="success">validated</Badge>}
                    {h.status !== "active" && <DocumentStatusBadge status={h.status} />}
                    <span className="ml-auto font-mono text-[11px] text-subtle" title="Fused relevance score">score {h.score.toFixed(3)}</span>
                  </div>
                  {h.heading && <p className="mt-1 text-xs text-muted">{h.heading}</p>}
                  <p className="mt-2 text-sm whitespace-pre-wrap text-fg/85">{h.excerpt}</p>
                  <div className="mt-2 flex items-center justify-between">
                    <span className="text-xs text-subtle">v{h.version}{h.tags.length > 0 && ` · ${h.tags.join(", ")}`}</span>
                    <Feedback hit={h} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ statistics tab

function Counts({ title, data, label }: { title: string; data: Record<string, number>; label?: (k: string) => string }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  return (
    <Panel title={title}>
      {!entries.length ? (
        <p className="text-xs text-subtle">No documents yet.</p>
      ) : (
        <ul className="space-y-1.5 text-sm">
          {entries.map(([k, v]) => (
            <li key={k} className="flex items-center justify-between">
              <span className="text-muted">{label ? label(k) : k}</span>
              <span className="font-mono tabular-nums">{v}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function StatisticsTab() {
  const query = useQuery({ queryKey: ["knowledge", "stats"], queryFn: knowledgeApi.stats });
  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const s = query.data;
  const store = { ok: ["success", "available"], unavailable: ["danger", "unavailable"], not_configured: ["neutral", "not configured"] } as const;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          { label: "Documents", value: s.documents },
          { label: "Chunks", value: s.chunks },
          { label: "Validated", value: s.validated },
        ].map((m) => (
          <div key={m.label} className="panel px-4 py-3">
            <div className="font-mono text-2xl font-semibold tabular-nums">{m.value}</div>
            <div className="text-xs text-muted">{m.label}</div>
          </div>
        ))}
        <div className="panel px-4 py-3">
          <div className="flex items-center gap-2">
            <Badge tone={store[s.vector_store][0]}>vector store {store[s.vector_store][1]}</Badge>
          </div>
          <div className="mt-1 truncate text-xs text-muted" title={s.embedding_model ?? undefined}>
            {s.embedding_model ? `Embeddings: ${s.embedding_model}` : "No embedding model: keyword search only"}
          </div>
        </div>
      </div>
      {!s.embedding_model && (
        <Alert tone="primary">
          Add an embedding model under Settings → AI Models (purpose “embedding”, e.g. nomic-embed-text on Ollama) to enable semantic search, then re-index.
        </Alert>
      )}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Counts title="By type" data={s.by_type} label={typeLabel} />
        <Counts title="By status" data={s.by_status} />
        <Counts title="By index state" data={s.by_index_status} label={(k) => INDEX_STATUS_META[k as keyof typeof INDEX_STATUS_META]?.label ?? k} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ page

export function KnowledgePage() {
  const queryClient = useQueryClient();
  const canManage = useAuthStore((s) => s.hasPermission("knowledge.manage"));
  const [tab, setTab] = useState<Tab>("documents");
  const [uploading, setUploading] = useState(false);
  const [creating, setCreating] = useState(false);
  const reindex = useMutation({
    mutationFn: () => knowledgeApi.reindex(),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["knowledge"] }),
  });
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Knowledge Base</h1>
          <p className="text-sm text-muted">Runbooks, documentation and past incidents the agents search during investigations.</p>
        </div>
        {canManage && (
          <div className="flex flex-wrap gap-2">
            <Button
              variant="secondary"
              onClick={() => {
                if (window.confirm("Re-index every document? This runs in the background.")) reindex.mutate();
              }}
              loading={reindex.isPending}
            >
              <RefreshCw /> Re-index all
            </Button>
            <Button variant="secondary" onClick={() => setCreating(true)}><FilePlus2 /> New document</Button>
            <Button onClick={() => setUploading(true)}><Upload /> Upload</Button>
          </div>
        )}
      </div>
      {reindex.data && <Alert tone="success">{reindex.data.queued} document(s) queued for re-indexing.</Alert>}
      {reindex.error ? <Alert>{errorMessage(reindex.error)}</Alert> : null}

      <div className="flex gap-1 overflow-x-auto border-b border-border text-sm">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 whitespace-nowrap ${tab === t ? "border-primary text-fg" : "border-transparent text-muted hover:text-fg"}`}
          >
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>

      {tab === "documents" && <DocumentsTab />}
      {tab === "search" && <SearchTab />}
      {tab === "statistics" && <StatisticsTab />}

      {uploading && <UploadDialog open={uploading} onOpenChange={setUploading} />}
      {creating && <NewDocumentDialog open={creating} onOpenChange={setCreating} />}
    </div>
  );
}
