# REST API (Phase 1)

Base path: `/api/v1`. Interactive OpenAPI docs are at `/api/v1/docs` when `APP_ENV` is not
`production`.

Authenticate with `Authorization: Bearer <access_token>`. Obtain it from `POST /auth/login`;
the refresh token is set as an `httpOnly` cookie.

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/auth/login` | — | `{tenant, username, password}` → access token + refresh cookie |
| POST | `/auth/refresh` | refresh cookie | Rotates the refresh token |
| POST | `/auth/logout` | authenticated | Revokes session; 204 |
| GET | `/auth/me` | authenticated | Profile, tenant, roles, permissions |
| POST | `/auth/change-password` | authenticated | Returns a new access token; other sessions revoked |
| GET/POST | `/tenants` | `tenant.manage` | Create includes the first Tenant Administrator |
| GET/PUT | `/tenants/{id}` | `tenant.manage` | |
| POST | `/tenants/{id}/disable` · `/enable` | `tenant.manage` | The `system` tenant can't be disabled |
| GET | `/users?q=&status=&page=&page_size=` | `user.read` | |
| POST | `/users` | `user.create` (+ `role.assign` if `role_ids`) | |
| GET | `/users/{id}` | `user.read` | |
| PUT | `/users/{id}` | `user.update` | email, display_name, status, password (admin reset) |
| DELETE | `/users/{id}` | `user.delete` | Soft delete |
| PUT | `/users/{id}/roles` | `role.assign` | Escalation-guarded |
| GET | `/roles`, `/roles/{id}` | `role.read` | |
| GET | `/permissions` | `role.read` | |
| GET | `/audit-logs?action=&outcome=&user_id=&entity_type=&entity_id=&date_from=&date_to=` | `audit.read` | `action` is a prefix match |
| GET | `/dashboard/summary` | `dashboard.read` | Counts + service health (placeholder zeros for later phases) |
| GET | `/dashboard/activity?limit=` | `dashboard.read` + `audit.read` | |
| GET | `/health` | — | Liveness |
| GET | `/ready` | — | 200 when PostgreSQL, Redis and Qdrant respond, else 503 |

## Error format

```json
{"error": {"code": "forbidden", "message": "...", "details": {"missing_permissions": ["audit.read"]}, "request_id": "..."}}
```

Common codes: `unauthenticated`, `token_expired`, `token_revoked`, `invalid_credentials`,
`invalid_refresh_token`, `account_inactive`, `forbidden`, `not_found`, `conflict`,
`validation_error`, `rate_limited`, `service_unavailable`, `internal_error`.

Resources in other tenants always return `404`, never `403`, so their existence isn't revealed.
