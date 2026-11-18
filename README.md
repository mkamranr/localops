# LocalOps

**Open-source, AI-powered infrastructure operations and autonomous troubleshooting platform.**
Self-hosted, multi-tenant, and designed to run in connected or fully air-gapped environments.

> Status: **Phase 1 – Platform Foundation** (see [ProjectSpecification.md](ProjectSpecification.md) §20).
> Authentication, tenants, RBAC, audit logging, the dashboard shell and the Docker Compose
> deployment are done. The model gateway and LangGraph agents arrive in Phase 2.

## Quick start (Docker Compose)

```bash
cp .env.example .env
# Edit .env: replace every CHANGE_ME (APP_SECRET_KEY, DB passwords, BOOTSTRAP_ADMIN_PASSWORD)
docker compose up -d --build
docker compose ps            # all services healthy, "migrate" exited 0
```

Open `http://<host>:8300` and sign in with tenant **`system`**, username `admin` and your
`BOOTSTRAP_ADMIN_PASSWORD`. You'll be asked to change the password at first sign-in.

See [docs/installation.md](docs/installation.md) for details.

## Repository layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI app (`app/`), Alembic migrations, tests, Dockerfile |
| `frontend/` | React + TypeScript + Tailwind UI, Dockerfile |
| `deployment/` | nginx reverse proxy and PostgreSQL init scripts |
| `docs/` | Architecture, installation, security, API notes |
| `docker-compose.yml` | Full stack: postgres, redis, qdrant, migrate, api, worker, frontend, nginx |
| `docker-compose.test.yml` | Self-contained backend test stack |

## Development

Backend (Python 3.12, managed with [uv](https://docs.astral.sh/uv/)):

```bash
cd backend
uv sync
uv run ruff check . && uv run mypy app
uv run pytest tests/unit                      # no services needed
# Integration tests need PostgreSQL prepared by deployment/postgres/init/01-roles.sh:
LOCALOPS_TEST_DB=1 DB_HOST=localhost DB_OWNER_PASSWORD=... DB_APP_PASSWORD=... uv run pytest
```

Or run the full suite in containers:

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v
```

Frontend (Node 20):

```bash
cd frontend
npm ci
npm run dev          # proxies /api to http://localhost:8000 (override with LOCALOPS_API_URL)
npm run lint && npm test && npm run build
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
