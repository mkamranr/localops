# REST API (Phase 3)

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
| GET | `/dashboard/summary` | `dashboard.read` | Counts + service health (resources are live; incidents and approvals are placeholders) |
| GET | `/dashboard/activity?limit=` | `dashboard.read` + `audit.read` | |
| GET | `/models` · `/models/{id}` | `settings.read` | API keys are never returned (`has_api_key`) |
| POST | `/models` | `settings.manage` | First model becomes the default |
| PUT/DELETE | `/models/{id}` | `settings.manage` | `is_default`, `enabled`, `api_key`/`clear_api_key`; delete unpins agents |
| POST | `/models/{id}/test` | `settings.manage` | Lists server models + 1-token probe; stores health |
| GET | `/agents` · `/agents/{id}` | `agent.read` | Config, availability phase, 24 h stats |
| PUT | `/agents/{id}` | `agent.configure` | `model_id` (null = default), `enabled`, `configuration` |
| POST | `/agents/{id}/enable` · `/disable` | `agent.configure` | |
| GET | `/agents/executions?agent_type=&investigation_id=` | `agent.read` | Paginated execution records |
| POST | `/investigations` | `incident.investigate` | `{request, title?, execution_mode}` → 202, queued |
| GET | `/investigations?status=&active=&q=` | `incident.read` | Paginated |
| GET | `/investigations/{id}` | `incident.read` | Includes `result` (report) |
| GET | `/investigations/{id}/events?after=` | `incident.read` | Ordered by `seq` |
| GET | `/investigations/{id}/stream` | `incident.read` | SSE; honours `Last-Event-ID`; ends with `event: end` |
| POST | `/investigations/{id}/resume` | `incident.investigate` | `{answer}` when status is `awaiting_input` |
| POST | `/investigations/{id}/cancel` | `incident.investigate` | Immediate if waiting; otherwise at the next step |
| GET | `/integrations` · `/integrations/{id}` | `integration.read` | Credentials are never returned (`has_credentials`, `credential_fields`) |
| POST | `/integrations` | `integration.configure` | `{name, integration_type, configuration, credentials, targets, schedule_minutes}` |
| PUT/DELETE | `/integrations/{id}` | `integration.configure` | Omit `credentials` to keep them; `clear_credentials`, `clear_schedule`. Delete keeps discovered resources |
| POST | `/integrations/{id}/test` | `integration.configure` | `{target?}`; pins the SSH host key on first use |
| POST | `/integrations/{id}/discover` | `integration.configure` | 202, queued `DiscoveryRun`; 409 if a run is active or the integration is disabled |
| GET | `/integrations/{id}/runs` · `/integrations/runs/{run_id}` | `integration.read` | Stats and per-target results |
| GET | `/integrations/{id}/host-keys` | `integration.read` | Pinned SSH host keys |
| DELETE | `/integrations/{id}/host-keys/{key_id}` | `integration.configure` | Reset a pin (audited) |
| GET | `/resources?q=&type=&environment=&health=&group=&tag=&integration_id=&stale=` | `resource.read` | Paginated |
| POST | `/resources` | `resource.create` | Manual resource |
| GET | `/resources/summary` · `/resources/facets` | `resource.read` | Counts by health/type; environments, groups, tags |
| GET/PUT/DELETE | `/resources/{id}` | `resource.read` / `.update` / `.delete` | Discovered facts are read-only |
| POST | `/resources/discover` | `resource.create` + `integration.read` | `{integration_id}` (alias of the integration endpoint) |
| GET | `/resources/{id}/health` | `resource.read` | Status, reasons, checked at |
| POST | `/resources/{id}/health/check` | `resource.update` | Re-collects that resource's target; 202 |
| GET | `/resources/{id}/history?page=` · `/metrics?hours=` | `resource.read` | Events; metric samples |
| GET | `/resources/{id}/relationships` | `resource.read` | Direct relationships |
| GET | `/resources/{id}/dependencies?direction=up\|down\|both&depth=1..6` | `resource.read` | Nodes, edges and `blast_radius` (transitive dependents) |
| GET | `/resources/graph?environment=&group=&type=&include_stale=` | `resource.read` | Whole-tenant topology, capped at 500 nodes |
| POST | `/resource-relationships` | `resource.update` | `{source_id, target_id, relationship_type}`; manual |
| DELETE | `/resource-relationships/{id}` | `resource.update` | Manual relationships only |
| GET | `/health` | — | Liveness |
| GET | `/ready` | — | 200 when PostgreSQL, Redis and Qdrant respond, else 503 |

## Error format

```json
{"error": {"code": "forbidden", "message": "...", "details": {"missing_permissions": ["audit.read"]}, "request_id": "..."}}
```

Common codes: `unauthenticated`, `token_expired`, `token_revoked`, `invalid_credentials`,
`invalid_refresh_token`, `account_inactive`, `forbidden`, `not_found`, `conflict`,
`validation_error`, `rate_limited`, `service_unavailable`, `internal_error`, `mode_unavailable`,
`invalid_model`, `encryption_unavailable`, `run_in_progress`.

Collector failures are reported per target in a run's `target_results` with an `error_code`:
`unreachable`, `timeout`, `auth_failed`, `host_key_changed`, `missing_credentials`,
`docker_permission_denied`, `winrm_error`, `bad_output`.

Resources in other tenants always return `404`, never `403`, so their existence isn't revealed.
