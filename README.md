# LocalOps

**Open-source, AI-powered infrastructure operations and autonomous troubleshooting platform.**
Self-hosted, multi-tenant, and designed to run in connected or fully air-gapped environments.

> Status: **Phase 5 – Controlled remediation** (see [ProjectSpecification.md](ProjectSpecification.md) §20).
>
> - **Phase 1:** authentication, tenants, RBAC, audit, dashboard, Compose deployment.
> - **Phase 2:** a model gateway for Ollama, vLLM, llama.cpp and any OpenAI-compatible server;
>   the agent registry; the Supervisor; LangGraph investigations with PostgreSQL checkpointing;
>   live SSE progress.
> - **Phase 3:** read-only discovery of Linux (SSH), Windows (WinRM), Docker and HTTP
>   endpoints; a resource inventory with health, history and metrics; relationships and
>   blast-radius views.
> - **Phase 4:**
>   - diagnostics agents that choose read-only tools by tool calling (processes, journal and
>     event logs, container state and logs, HTTP, DNS and TCP checks), with redaction;
>   - evidence-cited root cause analysis with confidence guards;
>   - incidents, opened automatically when a resource turns unhealthy, with timeline,
>     investigation and write-back;
>   - similar-incident search, printable RCA reports, and live logs on resources.
> - **Phase 5:**
>   - a Remediation Planner that proposes registered actions (restart, start or stop services
>     and containers, terminate a process, reboot), each citing evidence;
>   - remediation policies, approvals with four-eyes by default, expiry and an audit trail;
>   - an execution gateway that re-checks every approval at run time, and a Verification agent
>     that confirms the result;
>   - autonomous mode for policy-allowed, low-risk actions only.

## Quick start (Docker Compose)

```bash
cp .env.example .env
# Edit .env: replace every CHANGE_ME (APP_SECRET_KEY, DB passwords, BOOTSTRAP_ADMIN_PASSWORD)
docker compose up -d --build
docker compose ps            # all services healthy, "migrate" exited 0
```

Open `http://<host>:8300` and sign in with tenant **`system`**, username `admin` and your
`BOOTSTRAP_ADMIN_PASSWORD`. You'll be asked to change the password at first sign-in.

Then add a model under **Settings → AI Models** (or set `LLM_BASE_URL`/`LLM_MODEL` in `.env`
before the first start), add hosts under **Tools & Integrations** (set `LOCALOPS_ENCRYPTION_KEY`
first), and open **AI Assistant**.

See [docs/installation.md](docs/installation.md) and [docs/ai.md](docs/ai.md) for details.

## Repository layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI app (`app/`), Alembic migrations, tests, Dockerfile |
| `frontend/` | React + TypeScript + Tailwind UI, Dockerfile |
| `deployment/` | nginx reverse proxy and PostgreSQL init scripts |
| `docs/` | Architecture, AI orchestration, installation, security, API notes |
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
