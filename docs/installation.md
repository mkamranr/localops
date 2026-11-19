# Installation (Phase 3)

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
| `LOCALOPS_ENCRYPTION_KEY` | Fernet key encrypting model API keys and integration credentials at rest. Required for integrations. Back it up separately. |
| `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL` | Optional: seed a default model endpoint for every tenant at bootstrap. |
| `INVESTIGATION_TIMEOUT_SECONDS` | Hard limit per investigation run (default 1800). |
| `DISCOVERY_CONCURRENCY`, `DISCOVERY_MAX_TARGETS` | Targets collected in parallel (default 8), and the target cap per integration (default 500). |
| `METRIC_RETENTION_DAYS` | How long resource metric samples are kept (default 30). |

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
clarifying question, plans the investigation and streams progress live. Entities it names are
checked against the inventory (section 5), and the Discovery agent adds what the inventory knows
about them as evidence. Log, metric and root-cause agents arrive in Phase 4.

## 5. Add infrastructure

Open **Tools & Integrations**. Each integration is one connection profile: a type, connection
settings, encrypted credentials and an explicit list of targets (hostnames or IPs, one per
line, `host[:port] [display name]`). There is no network sweeping. Set
`LOCALOPS_ENCRYPTION_KEY` first; integrations refuse to store credentials without it.

Press **Test** to check one target. Then press **Discover now**, or pick a schedule
(15 minutes to daily). Discovered resources appear under **Infrastructure**, and their
relationships under **Dependencies**. Resources that a later run no longer finds are marked
*stale*, not deleted.

LocalOps only runs a fixed set of **read-only** commands on targets. There is no free-form
command field. Use a dedicated, least-privileged account.

### Linux (SSH)

- Create a plain user (for example `localops`) with no sudo rights. Every collected fact is
  readable without root: `/etc/os-release`, `/proc`, `df`, `ip`, `ss`, `systemctl`.
- Authenticate with a private key (recommended; OpenSSH or PEM, optionally with a passphrase)
  or a password.
- **Host keys are pinned on first use.** The first successful connection stores the server's key
  fingerprint, shown in the Test result and under *Pinned host keys*. If the key ever changes,
  LocalOps refuses to connect and writes an audit record. After verifying the new key out of
  band, an administrator can press **Reset pin**.
- **Docker containers** are collected over the same SSH session when the `docker` CLI works for
  that user. That requires membership of the `docker` group, which is **root-equivalent** on
  the host. If that is not acceptable, set *Docker containers* to *Do not collect* and use a
  Docker integration in TLS mode instead.

### Windows (WinRM)

- Enable WinRM on the target (`winrm quickconfig`, or a GPO). HTTP uses port 5985 with NTLM
  message encryption; HTTPS uses port 5986 and needs a server certificate.
- Use a domain or local account that is a member of **Remote Management Users**. For a
  non-administrator account, also grant *Enable Account* and *Remote Enable* on the WMI
  namespace `root\cimv2` (`wmimgmt.msc`).
- Authentication: NTLM (default), or Basic over HTTPS only. Kerberos is not included.
- Facts collected: OS and build, domain, uptime, CPU and memory use, fixed disks, running
  services, stopped automatic services, IPv4 addresses, listening TCP ports, and established
  connections.

### Docker

- **SSH mode** (default) runs the `docker` CLI over SSH, with the same account requirements as
  the Linux integration above.
- **TLS mode** talks to the Engine API on a daemon started with `--tlsverify` (usually port
  2376). Paste the CA certificate, client certificate and client key. Anyone holding that client
  certificate has full control of the daemon, so keep it only in LocalOps.

### HTTP checks

One URL per line. LocalOps records the status code, latency, resolved addresses and, for HTTPS,
the certificate's expiry. Health is *degraded* when a response is slow or the certificate
expires within 14 days, and *unhealthy* on an unexpected status, a connection error or an
expired certificate. HTTP services are linked to the host or container they resolve to.

### What discovery builds

| Relationship | Derived from |
|---|---|
| container `RUNS_ON` host | Docker inventory |
| resource `CONNECTS_TO` resource | Established TCP connections between two inventoried resources (kept for 24 h after last seen) |
| HTTP service `RUNS_ON` host / container | Resolved IP and published container ports |

Add anything else, such as `DEPENDS_ON` or `STORES_DATA_IN`, by hand on a resource's
**Dependencies** tab. Discovery never changes manual relationships. Health rules: disk ≥ 90 %
is degraded and ≥ 97 % unhealthy; memory ≥ 95 % and load per CPU ≥ 2 are degraded; failed
services are degraded; an unreachable host, or a container that exited with an error or is
unhealthy, is unhealthy.

## 6. Run the test suite (optional)

```bash
docker compose -f docker-compose.test.yml run --rm --build tests
docker compose -f docker-compose.test.yml down -v
```

## Operations

| Task | Command |
|---|---|
| Logs | `docker compose logs -f api worker` |
| Investigations stuck in "Queued" | Check the worker: `docker compose ps worker` and `docker compose logs worker` |
| Discovery runs stay "queued" | Same: discovery and scheduled runs execute in the worker |
| A target fails with `host_key_changed` | The SSH host key differs from the pinned one. Verify it out of band, then **Reset pin** on the integration |
| Upgrade | `git pull && docker compose up -d --build` (migrations run automatically) |
| Stop | `docker compose down` (data volumes are kept) |
| Prometheus metrics | `http://api:8000/metrics` on the internal network (not exposed by nginx) |

Backup and restore scripts are delivered in Phase 7. Until then, back up the
`localops_postgres-data` volume with `pg_dump` (`docker compose exec postgres pg_dump -U postgres localops`).
