# Installation (Phase 1)

## Requirements

- Linux x86_64 host with Docker Engine 24+ and the Compose v2 plugin.
- 2 vCPU / 4 GB RAM is enough for the Phase 1 stack (no GPU required).
- One free TCP port for the web UI (default `8300`).

Air-gapped bundles (pre-exported images, checksums, SBOM) are delivered in Phase 7. Until then,
build on a connected machine and transfer images with `docker save` / `docker load`.

## 1. Configure

```bash
cp .env.example .env
```

Set every `CHANGE_ME` value. Generate secrets with, for example:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

| Variable | Purpose |
|---|---|
| `APP_SECRET_KEY` | Signs access tokens (≥ 32 chars). Rotating it signs everyone out. |
| `POSTGRES_SUPERUSER_PASSWORD` | Used only inside the postgres container. |
| `LOCALOPS_OWNER_PASSWORD` | Schema owner, used only by the one-shot `migrate` service. |
| `LOCALOPS_APP_PASSWORD` | Runtime role for api/worker: not superuser, subject to row-level security. |
| `BOOTSTRAP_ADMIN_PASSWORD` | Initial Super Administrator password (≥ 12 chars, must be changed at first sign-in). |
| `COOKIE_SECURE` | Keep `true` behind HTTPS. Set `false` only for plain-HTTP evaluation. |
| `LOCALOPS_HTTP_PORT` | Published port of the nginx reverse proxy. |

> The database roles and passwords are created **once**, when the postgres volume is first
> initialised. Changing them later requires `ALTER ROLE` inside PostgreSQL.

## 2. Start

```bash
docker compose up -d --build
docker compose ps
```

Startup order is enforced by health checks:

```
postgres ─► migrate (alembic upgrade head + bootstrap) ─► api, worker
redis, qdrant ─────────────────────────────────────────► api
api, frontend ─────────────────────────────────────────► nginx (port 8300)
```

`migrate` should show `Exited (0)`. The bootstrap is idempotent. It creates:

- tenant `system` with all seven built-in roles and the Super Administrator;
- tenant `default` (if `BOOTSTRAP_CREATE_DEFAULT_TENANT=true`) with six roles, for regular users.

## 3. Verify

```bash
curl -s http://localhost:8300/api/v1/health      # {"status":"ok"}
curl -s http://localhost:8300/api/v1/ready       # postgres, redis, qdrant all "ok"
```

Then open `http://<host>:8300`, sign in to tenant `system` as `admin`, and change the password.
Create further tenants under **Settings → Tenants**. Each new tenant gets its own Tenant
Administrator, who signs in with that tenant's slug.

## 4. Run the test suite (optional)

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v
```

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f api worker` |
| Upgrade | `git pull && docker compose up -d --build` (migrations run automatically) |
| Stop | `docker compose down` (data volumes are kept) |
| Prometheus metrics | `http://api:8000/metrics` on the internal network (not exposed by nginx) |

Backup and restore scripts are delivered in Phase 7. Until then, back up the
`localops_postgres-data` volume with `pg_dump` (`docker compose exec postgres pg_dump -U postgres localops`).
