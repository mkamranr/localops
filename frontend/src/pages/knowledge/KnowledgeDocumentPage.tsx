import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BadgeCheck, BookOpen, Pencil, RefreshCw, RotateCcw, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { errorMessage } from "@/api/client";
import { knowledgeApi } from "@/api/endpoints";
import {
  EDITABLE_TYPES,
  formatBytes,
  isGenerated,
  parseTags,
  SOURCE_LABEL,
  sourceLink,
  typeLabel,
} from "@/components/knowledge/knowledge";
import { DocumentStatusBadge, IndexStatusBadge } from "@/components/knowledge/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Badge, EmptyState, ErrorState, Panel, Spinner, Table, Td, Th } from "@/components/ui/display";
import { Field, Input, Select, Textarea } from "@/components/ui/form";
import { useAuthStore } from "@/stores/authStore";
import type { DocumentStatus, EditableDocumentType, KnowledgeDocumentDetail } from "@/types/api";
import { formatDateTime, formatRelative } from "@/utils/format";

const TABS = ["content", "chunks", "versions"] as const;
type Tab = (typeof TABS)[number];

function EditDialog({ doc, onOpenChange }: { doc: KnowledgeDocumentDetail; onOpenChange: (o: boolean) => void }) {
  const queryClient = useQueryClient();
  const generated = isGenerated(doc);
  const [title, setTitle] = useState(doc.title);
  const [type, setType] = useState<EditableDocumentType>(
    (EDITABLE_TYPES as string[]).includes(doc.document_type) ? (doc.document_type as EditableDocumentType) : "other",
  );
  const [status, setStatus] = useState<DocumentStatus>(doc.status);
  const [category, setCategory] = useState(doc.category ?? "");
  const [tags, setTags] = useState(doc.tags.join(", "));
  const [content, setContent] = useState(doc.content);
  const [note, setNote] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      knowledgeApi.update(doc.id, {
        status,
        category: category.trim() || null,
        tags: parseTags(tags),
        ...(generated
          ? {}
          : {
              title: title.trim() !== doc.title ? title.trim() : undefined,
              document_type: type !== doc.document_type ? type : undefined,
              content: content !== doc.content ? content : undefined,
              note: note.trim() || undefined,
            }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge"] });
      onOpenChange(false);
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    mutation.mutate();
  };
  return (
    <Dialog
      wide={!generated}
      open
      onOpenChange={onOpenChange}
      title="Edit document"
      description={
        generated
          ? "Generated from an incident or investigation: only status, category and tags can change."
          : "Changing the title or content creates a new version and re-indexes the document."
      }
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="kb-edit" loading={mutation.isPending}>Save</Button>
        </>
      }
    >
      <form id="kb-edit" onSubmit={submit} className="space-y-4">
        {!generated && (
          <Field label="Title" htmlFor="kb-e-title">
            <Input id="kb-e-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} required />
          </Field>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {!generated && (
            <Field label="Type" htmlFor="kb-e-type">
              <Select id="kb-e-type" value={type} onChange={(e) => setType(e.target.value as EditableDocumentType)}>
                {EDITABLE_TYPES.map((t) => <option key={t} value={t}>{typeLabel(t)}</option>)}
              </Select>
            </Field>
          )}
          <Field label="Status" htmlFor="kb-e-status">
            <Select id="kb-e-status" value={status} onChange={(e) => setStatus(e.target.value as DocumentStatus)}>
              <option value="active">Active</option>
              <option value="draft">Draft</option>
              <option value="deprecated">Deprecated</option>
            </Select>
          </Field>
          <Field label="Category" htmlFor="kb-e-cat">
            <Input id="kb-e-cat" value={category} onChange={(e) => setCategory(e.target.value)} maxLength={100} />
          </Field>
        </div>
        <Field label="Tags" htmlFor="kb-e-tags" hint="Comma separated.">
          <Input id="kb-e-tags" value={tags} onChange={(e) => setTags(e.target.value)} />
        </Field>
        {!generated && (
          <>
            <Field label="Content (Markdown)" htmlFor="kb-e-content">
              <Textarea id="kb-e-content" value={content} onChange={(e) => setContent(e.target.value)} rows={16} className="font-mono text-xs" required />
            </Field>
            <Field label="Change note" htmlFor="kb-e-note" hint="Optional; stored with the new version.">
              <Input id="kb-e-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} />
            </Field>
          </>
        )}
        {mutation.error && <Alert>{errorMessage(mutation.error)}</Alert>}
      </form>
    </Dialog>
  );
}

function VersionsTab({ doc, canManage }: { doc: KnowledgeDocumentDetail; canManage: boolean }) {
  const queryClient = useQueryClient();
  const [viewing, setViewing] = useState<number | null>(null);
  const versions = useQuery({ queryKey: ["knowledge", doc.id, "versions"], queryFn: () => knowledgeApi.versions(doc.id) });
  const detail = useQuery({
    queryKey: ["knowledge", doc.id, "version", viewing],
    queryFn: () => knowledgeApi.version(doc.id, viewing!),
    enabled: viewing !== null,
  });
  const restore = useMutation({
    mutationFn: (v: number) => knowledgeApi.restore(doc.id, v),
    onSuccess: () => {
      setViewing(null);
      void queryClient.invalidateQueries({ queryKey: ["knowledge"] });
    },
  });
  const restorable = canManage && !isGenerated(doc);
  const askRestore = (v: number) => {
    if (window.confirm(`Restore version ${v}? Its content becomes a new version.`)) restore.mutate(v);
  };
  return (
    <Panel title="Version history" bodyClassName="px-0 pb-0">
      {restore.error ? <div className="px-5 pb-3"><Alert>{errorMessage(restore.error)}</Alert></div> : null}
      {versions.isLoading ? (
        <Spinner />
      ) : versions.error ? (
        <ErrorState message={errorMessage(versions.error)} />
      ) : (
        <Table>
          <thead><tr><Th>Version</Th><Th>Title</Th><Th>Note</Th><Th>Size</Th><Th>By</Th><Th>When</Th><Th /></tr></thead>
          <tbody>
            {(versions.data ?? []).map((v) => (
              <tr key={v.version}>
                <Td className="font-mono text-xs">
                  v{v.version} {v.version === doc.version && <Badge tone="primary">current</Badge>}
                </Td>
                <Td className="max-w-64 truncate text-sm">{v.title}</Td>
                <Td className="max-w-64 truncate text-xs text-muted">{v.note ?? "—"}</Td>
                <Td className="text-xs text-muted">{formatBytes(v.size)}</Td>
                <Td className="text-xs text-muted">{v.created_by ?? "system"}</Td>
                <Td className="text-xs text-muted" title={formatDateTime(v.created_at)}>{formatRelative(v.created_at)}</Td>
                <Td className="text-right whitespace-nowrap">
                  <Button variant="ghost" size="sm" onClick={() => setViewing(v.version)}>View</Button>
                  {restorable && v.version !== doc.version && (
                    <Button variant="ghost" size="sm" onClick={() => askRestore(v.version)} loading={restore.isPending && restore.variables === v.version}>
                      <RotateCcw /> Restore
                    </Button>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      {viewing !== null && (
        <Dialog
          wide
          open
          onOpenChange={(o) => !o && setViewing(null)}
          title={`Version ${viewing}`}
          description={detail.data ? `${detail.data.title} · ${formatDateTime(detail.data.created_at)}` : undefined}
          footer={
            restorable && viewing !== doc.version ? (
              <Button onClick={() => askRestore(viewing)} loading={restore.isPending}><RotateCcw /> Restore this version</Button>
            ) : undefined
          }
        >
          {detail.isLoading ? (
            <Spinner />
          ) : detail.error || !detail.data ? (
            <ErrorState message={errorMessage(detail.error)} />
          ) : (
            <pre className="max-h-[60vh] overflow-auto rounded-lg border border-border bg-black/40 p-3 font-mono text-xs whitespace-pre-wrap text-fg/85">
              {detail.data.content}
            </pre>
          )}
        </Dialog>
      )}
    </Panel>
  );
}

function Meta({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] tracking-wide text-subtle uppercase">{label}</dt>
      <dd className="mt-0.5 text-sm break-words">{children}</dd>
    </div>
  );
}

export function KnowledgeDocumentPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const canManage = useAuthStore((s) => s.hasPermission("knowledge.manage"));
  const [tab, setTab] = useState<Tab>("content");
  const [editing, setEditing] = useState(false);
  const query = useQuery({
    queryKey: ["knowledge", id],
    queryFn: () => knowledgeApi.get(id),
    refetchInterval: (q) => (q.state.data?.index_status === "pending" ? 3_000 : false),
  });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["knowledge"] });
  const validate = useMutation({ mutationFn: (v: boolean) => knowledgeApi.validate(id, v), onSuccess: refresh });
  const setStatus = useMutation({ mutationFn: (status: DocumentStatus) => knowledgeApi.update(id, { status }), onSuccess: refresh });
  const reindex = useMutation({ mutationFn: () => knowledgeApi.reindex([id]), onSuccess: refresh });
  const remove = useMutation({
    mutationFn: () => knowledgeApi.remove(id),
    onSuccess: () => {
      refresh();
      navigate("/knowledge");
    },
  });

  if (query.isLoading) return <Spinner />;
  if (query.error || !query.data) return <ErrorState message={errorMessage(query.error)} onRetry={() => query.refetch()} />;
  const doc = query.data;
  const origin = sourceLink(doc);
  const actionError = validate.error ?? setStatus.error ?? reindex.error ?? remove.error;
  const resources = Object.entries(doc.resource_names ?? {});

  return (
    <div className="space-y-5">
      <Link to="/knowledge" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg"><ArrowLeft className="size-4" /> Knowledge Base</Link>
      <div className="panel flex flex-wrap items-center gap-4 px-5 py-4">
        <BookOpen className="size-6 text-blue-300" />
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-lg font-semibold">{doc.title}</h1>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <Badge>{typeLabel(doc.document_type)}</Badge>
            <DocumentStatusBadge status={doc.status} />
            {doc.validated_at && <Badge tone="success" title={`by ${doc.validated_by ?? "?"} ${formatDateTime(doc.validated_at)}`}>validated</Badge>}
            <IndexStatusBadge status={doc.index_status} error={doc.index_error} />
            <span className="text-xs text-subtle">v{doc.version} · updated {formatRelative(doc.updated_at ?? doc.created_at)}</span>
          </div>
        </div>
        {canManage && (
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" onClick={() => setEditing(true)}><Pencil /> Edit</Button>
            <Button variant="secondary" onClick={() => validate.mutate(!doc.validated_at)} loading={validate.isPending}>
              <BadgeCheck /> {doc.validated_at ? "Unvalidate" : "Validate"}
            </Button>
            <Button
              variant="secondary"
              onClick={() => setStatus.mutate(doc.status === "deprecated" ? "active" : "deprecated")}
              loading={setStatus.isPending}
            >
              {doc.status === "deprecated" ? "Activate" : "Deprecate"}
            </Button>
            <Button variant="secondary" onClick={() => reindex.mutate()} loading={reindex.isPending}><RefreshCw /> Re-index</Button>
            <Button
              variant="danger"
              onClick={() => {
                if (window.confirm(`Delete "${doc.title}" and all its versions?`)) remove.mutate();
              }}
              loading={remove.isPending}
            >
              <Trash2 /> Delete
            </Button>
          </div>
        )}
      </div>
      {actionError ? <Alert>{errorMessage(actionError)}</Alert> : null}
      {doc.index_status === "failed" && doc.index_error && <Alert>Indexing failed: {doc.index_error}</Alert>}
      {doc.index_status === "keyword_only" && (
        <Alert tone="warning">Keyword search only{doc.index_error ? `: ${doc.index_error}` : " — no embedding model is configured."}</Alert>
      )}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-4">
        <div className="space-y-4 xl:col-span-3">
          <div className="flex gap-1 overflow-x-auto border-b border-border text-sm">
            {TABS.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                className={`-mb-px border-b-2 px-3 py-2 whitespace-nowrap capitalize ${tab === t ? "border-primary text-fg" : "border-transparent text-muted hover:text-fg"}`}
              >
                {t === "chunks" ? `Chunks (${doc.chunks.length})` : t}
              </button>
            ))}
          </div>
          {tab === "content" && (
            <Panel>
              <pre className="max-h-[70vh] overflow-auto font-mono text-xs leading-relaxed whitespace-pre-wrap text-fg/90">{doc.content}</pre>
            </Panel>
          )}
          {tab === "chunks" &&
            (doc.chunks.length ? (
              <ul className="space-y-3">
                {doc.chunks.map((c) => (
                  <li key={c.id} className="panel p-4">
                    <div className="flex items-center gap-2 text-xs">
                      <span className="font-mono text-subtle">#{c.chunk_index}</span>
                      <span className="flex-1 truncate text-muted">{c.heading || "(no heading)"}</span>
                      <Badge tone={c.embedded ? "success" : "neutral"}>{c.embedded ? "embedded" : "keyword only"}</Badge>
                    </div>
                    <pre className="mt-2 max-h-64 overflow-auto font-mono text-[11px] whitespace-pre-wrap text-fg/85">{c.content}</pre>
                  </li>
                ))}
              </ul>
            ) : (
              <Panel><EmptyState title="No chunks yet" description="The document has not been indexed yet." /></Panel>
            ))}
          {tab === "versions" && <VersionsTab doc={doc} canManage={canManage} />}
        </div>

        <Panel title="Details" className="h-fit">
          <dl className="space-y-3">
            <Meta label="Source">
              {origin ? <Link to={origin} className="text-blue-300 hover:underline">{SOURCE_LABEL[doc.source]}</Link> : SOURCE_LABEL[doc.source] ?? doc.source}
            </Meta>
            {doc.filename && <Meta label="File">{doc.filename}</Meta>}
            <Meta label="Size">{formatBytes(doc.size)}</Meta>
            {doc.category && <Meta label="Category">{doc.category}</Meta>}
            {doc.tags.length > 0 && (
              <Meta label="Tags">
                <div className="flex flex-wrap gap-1">{doc.tags.map((t) => <Badge key={t}>{t}</Badge>)}</div>
              </Meta>
            )}
            {resources.length > 0 && (
              <Meta label="Linked resources">
                <ul className="space-y-0.5">
                  {resources.map(([rid, name]) => (
                    <li key={rid}><Link to={`/infrastructure/${rid}`} className="text-blue-300 hover:underline">{name}</Link></li>
                  ))}
                </ul>
              </Meta>
            )}
            <Meta label="Embedding model">{doc.embedding_model ?? "—"}</Meta>
            <Meta label="Indexed">{doc.indexed_at ? formatDateTime(doc.indexed_at) : "—"}</Meta>
            <Meta label="Feedback">
              <span className="text-green-300">{doc.helpful} helpful</span> · <span className="text-red-300">{doc.not_helpful} not helpful</span>
            </Meta>
            <Meta label="Created">{formatDateTime(doc.created_at)} by {doc.created_by ?? "system"}</Meta>
          </dl>
        </Panel>
      </div>
      {editing && <EditDialog doc={doc} onOpenChange={setEditing} />}
    </div>
  );
}
