# Installation (Phase 2)

## Requirements

- Linux x86_64 host with Docker Engine 24+ and the Compose v2 plugin.
- 2 vCPU / 4 GB RAM is enough for the LocalOps stack itself (no GPU required).
- A self-hosted, OpenAI-compatible model server reachable from the host: Ollama, vLLM,
  llama.cpp `server` or similar. It can run on another machine (for example a GPU node). A
  7–14B instruction-tuned model with JSON output support works well (e.g. Qwen2.5-14B-Instruct).
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
| `LOCALOPS_ENCRYPTION_KEY` | Fernet key encrypting model API keys at rest. Optional unless you store API keys. Back it up separately. |
| `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL` | Optional: seed a default model endpoint for every tenant at bootstrap. |
| `INVESTIGATION_TIMEOUT_SECONDS` | Hard limit per investigation run (default 1800). |

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

## 4. Connect a model

Open **Settings → AI Models → Add model**:

| Server | Provider | Base URL example |
|---|---|---|
| Ollama on the Docker host | `ollama` | `http://host.docker.internal:11434` (start Ollama with `OLLAMA_HOST=0.0.0.0`) |
| Ollama elsewhere | `ollama` | `http://gpu-node-01:11434` |
| vLLM | `vllm` | `http://gpu-node-01:8000/v1` |
| llama.cpp server | `llamacpp` | `http://gpu-node-01:8080/v1` |

Use the model name exactly as the server lists it (e.g. `qwen2.5:14b-instruct` for Ollama,
`Qwen/Qwen2.5-14B-Instruct` for vLLM). Press **Test**: it lists the server's models, checks
that yours is among them, and runs a one-token probe. If your server rejects
`response_format`, set **JSON mode** to *prompt only*.

Then open **AI Assistant** and describe a problem. The Supervisor classifies it, may ask a
clarifying question, plans the investigation and streams progress live. Diagnostic agents that
collect evidence arrive in Phase 4. Until then reports say plainly that no evidence was
collected.

## 5. Run the test suite (optional)

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v
```

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f api worker` |
| Investigations stuck in "Queued" | Check the worker: `docker compose ps worker` and `docker compose logs worker` |
| Upgrade | `git pull && docker compose up -d --build` (migrations run automatically) |
| Stop | `docker compose down` (data volumes are kept) |
| Prometheus metrics | `http://api:8000/metrics` on the internal network (not exposed by nginx) |

Backup and restore scripts are delivered in Phase 7. Until then, back up the
`localops_postgres-data` volume with `pg_dump` (`docker compose exec postgres pg_dump -U postgres localops`).
