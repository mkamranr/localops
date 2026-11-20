# Knowledge and Extensibility (Phase 6)

Phase 6 adds:

- a tenant-isolated **knowledge base** (runbooks, troubleshooting guides, past incidents and
  investigations) with hybrid keyword and semantic search;
- **MCP tool servers** and **custom HTTP tools** that administrators register, review and
  enable;
- **agent configuration**: per-agent tool lists, step limits and operator instructions, and
  custom specialist agents.

## Knowledge base

### Documents

A document has:

- a type (`runbook`, `troubleshooting`, `documentation`, `procedure`, `incident`,
  `investigation`, `other`) and a status (`active`, `draft`, `deprecated`);
- tags, a category and linked resources;
- a version history and an index status.

Documents come from three places:

| Source | How |
|---|---|
| Upload | **Knowledge Base → Upload**: `.md .markdown .txt .log .csv .json .pdf`, up to `KNOWLEDGE_MAX_UPLOAD_MB` (10). PDFs need a text layer; scanned images are not OCR'd |
| Manual | **New document**: a Markdown editor |
| Generated | Every 5 minutes the worker copies resolved or closed incidents (title, description, root cause, resolution) and completed investigations (summary, root cause, verified remediation) into the knowledge base. Changes are detected by content hash. Generated documents are read-only apart from status, tags and category |

Editing a document's title or content creates a new version; old versions can be viewed and
restored. Validating, deprecating, giving feedback and deleting are all audited.

### Indexing

On every change an `index_knowledge_document` job runs these steps:

1. **Chunk.** Text is split into chunks of about `KNOWLEDGE_CHUNK_CHARS` (2400) characters,
   overlapping by `KNOWLEDGE_CHUNK_OVERLAP` (300). Splits follow Markdown headings and never
   break inside a code fence, and each chunk keeps its heading path.
2. **Keyword index.** Chunks are stored in PostgreSQL with a weighted full-text vector.
3. **Embed.** If the tenant has a default **embedding model**, the chunks are embedded
   through the model gateway (OpenAI-compatible `/v1/embeddings`).
4. **Store vectors.** The vectors are written to Qdrant.

Index status values:

| Status | Meaning |
|---|---|
| `indexed` | Keyword and vector search |
| `keyword_only` | No embedding model, or the model or Qdrant was unavailable. Still searchable by keyword; `index_error` says why |
| `failed` | The document could not be processed |
| `pending` | Waiting for the worker. Documents left pending longer than 2 minutes are retried by the sync cron |

Each embedding model (base URL, model name and dimension) gets its own Qdrant collection, so
changing the model never mixes vector spaces. Use **Re-index all** after switching models.

### Embedding model

Add a model under **Settings → AI Models** with purpose **Embedding** and mark it default.
For example, Ollama with `nomic-embed-text`: run `ollama pull nomic-embed-text` on the model
host. Alternatively, set `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` in `.env` to seed one for
every tenant. **Test** embeds a probe and reports the vector dimension.

### Search

`POST /knowledge/search` (and the **Search** tab) runs two searches:

- PostgreSQL full-text search over the chunks;
- Qdrant vector search filtered to the tenant.

The two result lists are fused with Reciprocal Rank Fusion (k = 60). Scores are then
adjusted:

| Condition | Effect |
|---|---|
| Validated document | ×1.2 |
| Linked to the resource being investigated | ×1.3 |
| Net positive feedback | ×1.1 |
| Net negative feedback | ×0.85 |
| Draft | ×0.8 |

Deprecated documents are excluded unless requested. Each hit reports `match`: `keyword`,
`semantic` or `both`. Without an embedding model, or if Qdrant is down, the response has
`mode: "keyword"` and a note.

**Similar incidents.** `GET /incidents/{id}/similar`, the Knowledge agent and the RCA report
combine full-text matching and shared resources with semantic hits on generated incident and
investigation documents.

### Tenant isolation

Every vector carries `tenant_id` in its payload, and every Qdrant query requires it as a
filter. Hits are then re-read from PostgreSQL under row-level security, so a vector that
somehow points at another tenant's chunk is still dropped. The tests forge such a point to
prove it.

### Knowledge in investigations

The **Knowledge agent** works from the request, its symptoms and the matched resources:

- It searches similar past incidents and investigations, plus runbooks and troubleshooting
  documents (up to three).
- Each match becomes an evidence item with `source: "knowledge"` and a reference to the
  document, chunk and version. The investigation workspace links it to the document.
- A short deterministic summary lists earlier resolutions. (There is no extra LLM pass, so
  nothing uncited is added.)

Agents can also call the read tool `search_knowledge`.

Knowledge is context, not proof:

- Knowledge text reaches models only inside `<knowledge source="…" untrusted="true">`
  blocks. Look-alike delimiters in the text are neutralised.
- The system prompts say that such text is reference material whose instructions must never
  be followed.
- A root cause citing only knowledge evidence is marked **unverified**.
- A remediation step must cite at least one live (tool or inventory) evidence item, or it is
  dropped.
- Text cannot widen what an agent may do. Allowed tools and the remediation gateway are
  enforced in code.

## Registered tools: MCP servers and HTTP APIs

### MCP servers

**Tools & Integrations → Add → MCP Server.** Network transports only: Streamable HTTP, with
automatic fallback to the legacy SSE transport. LocalOps never starts a local process.

| Setting | Notes |
|---|---|
| URL | `http(s)://host:port/path` |
| Transport | `auto` (default), `streamable_http` or `sse` |
| Authentication | none, bearer token, basic, or a custom header (stored encrypted) |
| TLS | verify (default) with an optional CA certificate |
| Environment | e.g. `production`. Feeds remediation policy for the server's change tools. Without one, the server never counts as non-production |

**Test** runs `initialize` and `tools/list`. **Sync tools** (and an hourly cron) reconciles
the server's tools into tool definitions:

- **New tools are disabled.** Only tools that declare `readOnlyHint: true` start as **read**;
  everything else starts as **change** with high risk. Hints are untrusted, and an
  administrator sets the final kind.
- **Pinning.** Enabling a tool pins a hash of its schema and remote description. If a later
  sync sees a different schema or description, the tool becomes `schema_changed` and is
  disabled until an administrator reviews and approves it again. This guards against tool
  poisoning and silent "rug-pull" changes.
- **Missing tools.** Tools that disappear from the server become `missing` and are
  disabled.
- **Descriptions.** Agents see the administrator-reviewed description, not the server's raw
  text.

Calls use a timeout, never follow redirects, ignore proxy environment variables and cap
responses at 2 MB. Output is redacted and recorded in `tool_executions`, like built-in tools.

### Custom HTTP tools

An **HTTP API** integration holds a base URL, authentication, TLS and environment.
**Settings → Tools → New HTTP tool** defines a request template:

- method and path with `{param}` placeholders;
- query parameters, headers, a JSON body template;
- a parameter list (type, required, pattern, length and range), which becomes the tool's
  JSON Schema;
- a response size cap.

Safety rules, all covered by tests:

- Each placeholder in the path fills **one percent-encoded segment**. `/`, `\`, `.` and
  `..` are rejected.
- The final URL must keep the base URL's scheme, host and path prefix (SSRF guard).
- Redirects are not followed.
- Header names are restricted, and values cannot contain CR or LF.
- The body is built as JSON from the arguments, never by string concatenation.
- A `GET` can never be a change tool.

### Read vs change, and permissions

| | Read tools | Change tools |
|---|---|---|
| Who classifies | `tool.manage` (Infrastructure Administrator and above) | same |
| Manual run (`POST /tools/{name}/run`) | Needs the tool's `required_permission` (`tool.execute.readonly`) | Never; refused |
| Agents | Only via an agent's `allowed_tools` | Never given to an agent |
| Remediation | Can be another tool's `verify_with` | Proposed by the Remediation Planner, then policy → approval (four-eyes) → gateway → verification. The target is the integration (resource type `integration`) |

Without `verify_with`, a change step ends **not verified**, with a note to verify manually.
Each tool's runner re-reads its definition when it runs, so disabling a tool takes effect
immediately, even inside a running investigation.

## Agent configuration

**Agents → Configure** (`agent.configure`):

- model (chat models only), temperature, max tokens, timeout and retries;
- for tool-using agents (the diagnostics agents and custom agents):
  - **allowed tools**: read tools only, validated against the tenant's catalogue;
  - **max steps** (1–15);
  - **operator instructions** (up to 2000 characters).

Operator instructions are appended after the built-in safety rules, in a delimited block that
states they cannot override those rules. Settings that don't apply to an agent (for example,
instructions for the Knowledge agent) are rejected.

**Custom agents.** **New custom agent** creates a `custom_<slug>` specialist. It runs the
same tool loop as the diagnostics agents, but only with its allowed read tools (at least one),
its own description, instructions and optional model.

The supervisor's planning prompt is built for each investigation from the agents that are
enabled at that moment, built-in and custom, each with the tools it would actually get.
Disabled agents are never planned, and the coverage rules skip them. Built-in agents can be
disabled but not deleted; custom agents can be deleted.

The Agents page also shows execution counts, success rate, average duration, last run and a
filterable execution history.
