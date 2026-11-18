# Security Model (Phase 2)

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
  schema. Planned tasks are filtered to known agents and capped. Nothing the model outputs runs a
  command; tool execution arrives in Phase 5 behind the policy and approval engine.
- Request text is passed to the model as delimited data, with an instruction to ignore embedded
  instructions. This reduces, but cannot eliminate, prompt injection, which is why model output
  never directly causes actions.
- Reports never invent evidence. Classifications are labelled unverified. Without diagnostic
  evidence, no root cause is stated.
- Prompts and completions are not logged; only token counts, latency and outcome are.
- `autonomous` execution mode is refused until Phase 5.

## Secrets at rest

Model API keys are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using
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
