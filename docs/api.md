# REST API (Phase 6)

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
| GET | `/dashboard/summary` | `dashboard.read` | Counts + service health, including open incidents and pending approvals |
| GET | `/dashboard/activity?limit=` | `dashboard.read` + `audit.read` | |
| GET | `/models` · `/models/{id}` | `settings.read` | API keys are never returned (`has_api_key`) |
| POST | `/models` | `settings.manage` | `purpose`: `chat` (default) or `embedding`; the first model of each purpose becomes its default |
| PUT/DELETE | `/models/{id}` | `settings.manage` | `is_default`, `enabled`, `api_key`/`clear_api_key`; delete unpins agents |
| POST | `/models/{id}/test` | `settings.manage` | Lists server models + 1-token probe (embedding models: embeds a probe and reports `dimensions`); stores health |
| GET | `/agents` · `/agents/{id}` | `agent.read` | Config, `custom`, `uses_tools`, 24 h and all-time stats (count, success rate, last run) |
| PUT | `/agents/{id}` | `agent.configure` | `model_id` (null = default, chat models only), `enabled`, `configuration` (`temperature`, `max_tokens`, `timeout_seconds`, `max_retries`; for tool-using agents also `allowed_tools` (read tools), `max_steps` 1–15, `instructions` ≤ 2000) |
| POST | `/agents` | `agent.configure` | Custom agent: `{name, description, slug?, model_id?, allowed_tools[≥1], instructions?, max_steps?, …}` (201) |
| DELETE | `/agents/{id}` | `agent.configure` | Custom agents only (409 for built-in agents) |
| POST | `/agents/{id}/enable` · `/disable` | `agent.configure` | |
| GET | `/agents/executions?agent_type=&investigation_id=&status=&errors_only=` | `agent.read` | Paginated execution records |
| POST | `/investigations` | `incident.investigate` | `{request, title?, execution_mode, resource_ids?, incident_id?, investigation_type?}` → 202, queued. Explicit resources count as resolved |
| GET | `/investigations?status=&active=&q=` | `incident.read` | Paginated; completed items carry a `report` brief (conclusion, root cause, confidence) |
| GET | `/investigations/{id}` | `incident.read` | Includes `result` (report) |
| GET | `/investigations/{id}/events?after=` | `incident.read` | Ordered by `seq` |
| GET | `/investigations/{id}/stream` | `incident.read` | SSE; honours `Last-Event-ID`; ends with `event: end` |
| POST | `/investigations/{id}/resume` | `incident.investigate` | `{answer}` when status is `awaiting_input` |
| POST | `/investigations/{id}/cancel` | `incident.investigate` | Immediate if waiting (pending approvals are cancelled); otherwise at the next step |
| POST | `/investigations/{id}/remediation/{step}` | `incident.investigate` (+ `tool.execute.remediation` to request) | Manual mode: `{action: request\|skip}` for the step the run is waiting on → 202 |
| GET | `/investigations/{id}/tool-executions` | `incident.read` | Tool runs with redacted output and evidence ids |
| GET | `/incidents?status=&severity=&source=&assigned_to=&resource_id=&open=&q=` | `incident.read` | Paginated; `q` is full-text |
| GET | `/incidents/summary` | `incident.read` | Open counts by severity and status |
| POST | `/incidents` | `incident.create` | `{title, description, severity, priority?, resource_ids?, assigned_to?, investigation_id?}` → 201 |
| GET | `/incidents/{id}` | `incident.read` | Includes resources, linked investigations and `allowed_transitions` |
| PUT | `/incidents/{id}` | `incident.update` | Title, description, severity, priority, status (validated transition), `assigned_to`, `resource_ids` |
| POST | `/incidents/{id}/close` | `incident.update` | `{resolution, status: resolved\|closed}` |
| POST | `/incidents/{id}/investigate` | `incident.investigate` | `{request?, execution_mode?}` → 202; uses the incident's resources and moves it to `investigating` |
| GET | `/incidents/{id}/timeline` | `incident.read` | Status, assignment, comments, investigations, resource health |
| POST | `/incidents/{id}/comments` | `incident.update` | `{body}` |
| GET | `/incidents/{id}/similar` | `incident.read` | Past incidents and investigations by text and shared resources |
| GET | `/incidents/{id}/tool-executions` | `incident.read` | Tool runs from the linked investigations |
| GET | `/tools?resource_type=` | `incident.read` | The tenant's catalogue: built-in plus enabled MCP and HTTP tools; `kind` is `read` or `change`, `source` is `builtin`, `mcp` or `http` |
| POST | `/tools/{name}/run` | `tool.execute.readonly` + the tool's permission | Read tools only: `{resource_id?, arguments}` → status, summary, redacted output (audited). Registered tools take no `resource_id` |
| GET | `/tools/definitions?integration_id=&source=&status=` · `/tools/definitions/{id}` | `integration.read` | Registered tools, including disabled ones, with `status` and `usable` |
| PUT | `/tools/definitions/{id}` | `tool.manage` | `{description, kind, risk_level, required_permission, timeout_seconds, verify_with, clear_verify_with, enabled, approve_schema}`. Change tools always require `tool.execute.remediation`; 409 when enabling a changed or missing tool |
| POST | `/tools/definitions` | `tool.manage` | Create an HTTP tool: `{integration_id, name, description, parameters[], template{method, path, query, headers, body}, kind, risk_level, timeout_seconds, verify_with, enabled}` |
| PUT | `/tools/definitions/{id}/http` | `tool.manage` | Edit an HTTP tool's description, parameters or template |
| DELETE | `/tools/definitions/{id}` | `tool.manage` | Remove a registered tool |
| POST | `/tools/definitions/{id}/test` | `tool.manage` | Run an enabled read tool once: `{arguments}` |
| POST | `/integrations/{id}/sync-tools` | `tool.manage` | MCP `tools/list` → `{server, total, added, changed, missing, unchanged}` |
| GET | `/knowledge?q=&document_type=&status=&source=&tag=&index_status=` | `knowledge.read` | Paginated documents |
| GET | `/knowledge/stats` | `knowledge.read` | Counts by type, status and index status; embedding model; vector store status |
| POST | `/knowledge/search` | `knowledge.read` | `{query, document_types?, resource_id?, tags?, include_deprecated?, limit?}` → `{mode, note, hits[{…, match}]}` |
| POST | `/knowledge` | `knowledge.manage` | `{title, document_type, content, category?, tags?, resource_ids?, status?}` (201) |
| POST | `/knowledge/upload` | `knowledge.manage` | Multipart: `file` plus optional `title`, `document_type`, `category`, `tags`, `resource_ids` (comma-separated) and `status` |
| GET · PUT · DELETE | `/knowledge/{id}` | `knowledge.read` / `.manage` | Detail with chunks; a title or content change creates a version; generated documents allow only status, tags and category |
| GET | `/knowledge/{id}/versions` · `/versions/{n}` | `knowledge.read` | Version history |
| POST | `/knowledge/{id}/versions/{n}/restore` | `knowledge.manage` | Restore as a new version |
| POST | `/knowledge/{id}/validate` | `knowledge.manage` | `{validated}` |
| POST | `/knowledge/{id}/feedback` | `knowledge.read` | `{helpful, comment?, chunk_id?, investigation_id?}`; one vote per user (204) |
| POST | `/knowledge/reindex` | `knowledge.manage` | `{document_ids?}` → `{queued}` |
| GET | `/approvals?status=&risk=&mine=&investigation_id=&incident_id=` | `approval.read` | Paginated approval requests |
| GET | `/approvals/summary` | `approval.read` | Pending count, by risk, mine, expiring soon |
| GET | `/approvals/{id}` | `approval.read` | Includes cited evidence, `can_decide` and the execution |
| POST | `/approvals/{id}/approve` · `/reject` | `approval.approve` | `{comment?}`; `403 self_approval` for the requester unless policy allows; 409 if already decided or expired |
| POST | `/approvals/{id}/request-changes` | `approval.approve` | `{comment}` (required); the planner revises the step (at most twice) |
| POST | `/approvals/{id}/cancel` | `approval.read` | Requester or an approver |
| GET | `/executions?investigation_id=&incident_id=&resource_id=&status=` · `/executions/{id}` | `approval.read` | Remediation jobs with before/after state and verification |
| GET | `/policies` · `/policies/actions` | `settings.read` | Remediation policies; the registered remediation tools |
| POST | `/policies/preview` | `settings.read` | `{tool, resource_id, execution_mode}` → risk, approval and the matched rules |
| POST · PUT · DELETE | `/policies`, `/policies/{id}` | `settings.manage` | `{name, description, priority, rules: {match, effect}, enabled}` (audited) |
| GET | `/resources/{id}/tool-executions?limit=` | `resource.read` | Recent tool runs on that resource |
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
`invalid_model`, `encryption_unavailable`, `run_in_progress`, `invalid_transition`.

Collector failures are reported per target in a run's `target_results` with an `error_code`:
`unreachable`, `timeout`, `auth_failed`, `host_key_changed`, `missing_credentials`,
`docker_permission_denied`, `winrm_error`, `bad_output`.

Tool runs that fail report an `error_code` too:
- `invalid_arguments` (rejected, never executed);
- `permission_denied`, `missing_credentials`, `no_integration`, `no_target`, `no_address`,
  `host_key_not_pinned`;
- `unreachable`, `timeout`, `unsupported`.

Resources in other tenants always return `404`, never `403`, so their existence isn't revealed.
