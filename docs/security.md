# Security Model (Phase 4)

## Authentication

- **Local accounts.** Passwords are hashed with Argon2id (minimum 12 characters). LDAP and OIDC
  are planned.
- **Access token.** A JWT (HS256, signed with `APP_SECRET_KEY`), valid for 15 minutes and kept
  only in browser memory. Claims: `sub`, `tid` (tenant), `jti`, `iat`, `exp`. Permissions are
  **never** read from the token; they're loaded from the database on every request.
- **Refresh token.** An opaque `<tenant_id>.<token_id>.<secret>` value in an `httpOnly`,
  `SameSite=Strict` cookie scoped to `/api/v1/auth`. Only `SHA-256(secret)` is stored.
  - Tokens rotate on every refresh.
  - **Reuse detection:** presenting an already-rotated token (outside a 30-second grace window
    for concurrent browser tabs) revokes the whole token family and writes an
    `auth.refresh_reuse` audit event.
- **Logout** revokes the refresh-token family and denylists the access token's `jti` in Redis
  until it expires. If Redis is unreachable, authenticated requests fail closed with 503.
- **Password change or admin reset** revokes all the user's refresh tokens. Access tokens issued
  before the change are rejected.
- **Login rate limiting.** 10 attempts per 5 minutes per (tenant, username, client IP).
  Failure messages are uniform, so they don't reveal whether a tenant or user exists.

## Authorization (RBAC)

- Permission codes are global (`resource.read`, `approval.approve`, …) and seeded by migration.
- Roles are per tenant. There are seven built-ins (§13.2). `tenant.manage` exists only in the
  Super Administrator role, which only exists in the `system` tenant.
- **Privilege-escalation guard:** you can only grant, revoke, disable or reset a user whose roles
  carry permissions you already hold. Nobody can change their own roles or status.

## Tenant isolation

Isolation is enforced by **PostgreSQL row-level security**, not only by application filters.

| Role | Used by | Properties |
|---|---|---|
| `localops_owner` | `migrate` only | Owns the schema; exempt from RLS (no `FORCE`). |
| `localops_app` | api, worker | `NOSUPERUSER NOBYPASSRLS`; least-privilege grants. |

- Every request runs in a transaction that sets `app.tenant_id` with transaction-local
  `set_config(..., true)`. Every tenant-owned table has the policy
  `tenant_id = app_current_tenant()`. With no context set, queries return **zero rows**.
- Composite foreign keys (`(user_id, tenant_id)`, `(role_id, tenant_id)`) make cross-tenant role
  assignment structurally impossible.
- Login resolves a tenant slug through a narrow `SECURITY DEFINER` function
  (`auth_resolve_tenant`). RLS is never disabled.
- The refresh token embeds its tenant id, so the RLS context can be set before lookup.
- Platform administrators manage the `tenants` table through `app.platform_admin='on'`, which the
  application sets only after checking `tenant.manage` in code. Seeding a new tenant switches the
  RLS context to that tenant rather than widening policies.
- Redis keys are namespaced `localops:t:<tenant_id>:…`.

> **Limitation:** RLS protects against application bugs such as forgotten filters or IDOR. It
> doesn't protect against a fully compromised application process, which could set
> `app.tenant_id` itself. Phase 7 hardening will revisit this.

These properties are tested directly in SQL as the app role (`backend/tests/integration/test_rls.py`).

LangGraph checkpoints follow the same rule. The thread id is `<tenant_id>:<investigation_id>`,
and RLS on the checkpoint tables compares that prefix with the session's tenant. The worker opens
its checkpoint connection as `localops_app` with the tenant set at session level. Cross-tenant
reads return nothing and cross-tenant writes are rejected (`test_investigations.py`).

## AI safety

- The model gateway only calls endpoints that an administrator configured. Model traffic ignores
  ambient proxy variables. LangSmith tracing is forced off.
- Model output is **advisory and validated**. Every Supervisor result is parsed against a strict
  schema. Planned tasks are filtered to known agents and capped.
- **The model can only choose read-only tools from a fixed catalogue** (see *Diagnostic tools*
  below). It cannot supply a command, a script or a target outside the inventory. Tools that
  change state, such as restarts or configuration changes, arrive in Phase 5 behind the policy
  and approval engine.
- Request text and tool output are passed to the model as delimited data, with an instruction
  to ignore embedded instructions. This reduces, but cannot eliminate, prompt injection, which
  is why model output can at most trigger read-only diagnostics.
- Reports never invent evidence:
  - Entities are marked verified only when they match the inventory.
  - Evidence ids are created by LocalOps for real tool runs and inventory reads, never by the
    model.
  - Claims citing unknown or no evidence are downgraded to unverified hypotheses.
  - A root cause is `confirmed` only with at least two successful tool results and confidence
    of 0.7 or more.
- Prompts and completions are not logged; only token counts, latency and outcome are.
- `autonomous` execution mode is refused until Phase 5.

## Infrastructure access (Phase 3–4)

- **Read-only, allowlisted collection.** Each discovery collector holds a constant script: a
  POSIX shell script piped to `sh -s`, fixed PowerShell, fixed `docker` CLI invocations, or
  Engine API GET requests. No user-supplied or model-supplied text is ever executed on a target
  as code. Targets are
  validated as hostnames or IPs (no leading `-`, so no option injection), and only
  administrators with `integration.configure` can define them.
- **Credentials** (passwords, private keys and passphrases, TLS client keys) are Fernet-encrypted
  with `LOCALOPS_ENCRYPTION_KEY`. They are write-only through the API (only the field *names*
  are returned), never logged, never included in audit details, and never passed to a language
  model. TLS client material is written to private temporary files for the duration of one
  connection only.
- **SSH host-key pinning.** The first successful connection pins the server key per
  (tenant, host, port). Later connections offer only the pinned key's algorithm and accept only
  that key. A mismatch is refused with `host_key_changed`, the presented fingerprint is
  recorded in the audit log (`integration.host_key_mismatch`), and the pin can only be reset by
  `integration.configure` (`integration.host_key_reset`, audited). The SSH client never reads
  local `~/.ssh` configuration, known_hosts files or agents.
- **Docker access.** Docker-over-SSH requires the account to be in the `docker` group, which is
  root-equivalent on that host. TLS mode is the alternative; its client certificate grants full
  control of the daemon. This is documented in the UI and the installation guide.
- **WinRM** uses NTLM with message encryption over HTTP, or HTTPS. Basic authentication is
  refused unless HTTPS is used.
- **Limits.** Every connection and command has a timeout. Command output is capped at 2 MB,
  collected lists are capped, targets per integration are capped (`DISCOVERY_MAX_TARGETS`), and
  per-run parallelism is bounded (`DISCOVERY_CONCURRENCY`). One discovery run per integration
  executes at a time (Redis lock).
- **Inventory data in prompts.** Resource names, health and facts that reach the Supervisor are
  passed as delimited data. A hostile hostname cannot cause an action, because model output
  never executes anything.
- All seven inventory tables are under row-level security. Resources, relationships,
  integrations and host keys of other tenants return `404`.

## Diagnostic tools (Phase 4)

- **Fixed catalogue.** Every tool is defined in code: a Pydantic argument model, a command
  template, the resource types it applies to, a timeout and an output cap. All tools are
  `read` risk.
- **Arguments.**
  - Arguments are validated before anything runs: unit, service and provider names against
    strict patterns, enums for logs and priorities, bounded integers for lines and time windows.
  - Values are then quoted, with `shlex.quote` for shells and literal single quotes for
    PowerShell.
  - Attempts such as `; rm -rf /`, `$(...)`, backticks or a leading `-` are rejected and recorded
    as `rejected`, never executed.
- **Targets come from the inventory.** Tools take a `resource_id` and connect through that
  resource's integration, with its pinned host key. Network checks (DNS, TCP, HTTP) accept only
  inventoried resources, so the worker cannot be used to scan arbitrary addresses.
- **Redaction.** Collected output passes through `app/tools/redaction.py` before it is stored,
  streamed to the UI or given to a model. It masks:
  - `password=`, `secret:`, `token=`, `api_key` and similar assignments;
  - `Authorization` and Bearer/Basic values;
  - private-key blocks;
  - credentials in URLs;
  - AWS access keys, GitHub and Slack tokens, and JWTs.

  Raw output is never persisted. At most 16 KB of redacted output is stored per run, and at
  most `TOOL_OUTPUT_MODEL_BYTES` reaches a model. Redaction is pattern-based, so it is best
  effort: keep secrets out of logs at the source too.
- **Permission and limits.**
  - Remote tools need `tool.execute.readonly`, held by the user who started the investigation,
    or by the user pressing *Run* on the Logs tab.
  - Each investigation is limited to `INVESTIGATION_MAX_TOOL_CALLS` tool runs, with at most two
    concurrent runs per host. Each tool has its own timeout.
- **Accounts on the targets.** The diagnostics need read access only:
  - Linux: membership of `systemd-journal` (or `adm`) to read the journal;
  - Windows: membership of **Event Log Readers** to read the event logs;
  - Docker: the `docker` group, as for discovery.
- **Audit.** Every run writes a `tool_executions` row and a `tool.execute` audit entry with the
  tool, the resource, the validated arguments, the status and the actor.

## Incidents (Phase 4)

- Four tables are under row-level security: `incidents`, `incident_resources`,
  `incident_events` and `tool_executions`. `incident_events` is append-only for the app role.
- Permissions: `incident.read`, `incident.create`, `incident.update` and
  `incident.investigate`. Every create, update, comment, status change and close is audited.
- Status changes are validated server-side. `awaiting_approval` and `remediating` can only be
  set by the Phase 5 workflow, not through the API.

## Secrets at rest

Model API keys and integration credentials are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using
`LOCALOPS_ENCRYPTION_KEY`. That key is separate from `APP_SECRET_KEY` so either can rotate
independently. Keys are write-only through the API, and the audit log records only that a key
was set, replaced or cleared.

## Audit logging

- `audit_logs` is append-only for the app role, which has no `UPDATE`/`DELETE` grant. It has no
  foreign key to users, so records outlive the entities they describe.
- Each record is written in the same transaction as the action it records. Failures (bad login,
  wrong current password) are written in their own transaction, so the rejection doesn't roll
  them back.
- Each entry has the actor, tenant, action, outcome, entity, details, IP, user agent and a
  request ID. The request ID is propagated via `X-Request-ID` and included in every error
  response.

## Transport and headers

- nginx is the only published service. `/metrics` and API docs are not exposed; docs are
  disabled when `APP_ENV=production`.
- Response headers: CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`.
- No CDN, web fonts or telemetry. The frontend bundle is fully self-contained.
- Terminate TLS in front of nginx (or extend `deployment/nginx/nginx.conf`) and keep
  `COOKIE_SECURE=true`.
