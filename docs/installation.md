# Installation (Phase 6)

## Requirements

- Linux x86_64 host with Docker Engine 24+ and the Compose v2 plugin.
- 2 vCPU / 4 GB RAM is enough for the LocalOps stack itself (no GPU required).
- A self-hosted, OpenAI-compatible model server reachable from the host: Ollama, vLLM,
  llama.cpp `server` or similar. It can run on another machine (for example a GPU node).
  Diagnostics agents choose tools through **tool calling**, so use an instruction-tuned model
  that calls tools reliably: Qwen2.5/Qwen3 14B or larger works well. Smaller models can
  classify and plan, but tend to call tools badly.
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

**Tool calling** decides how agents request diagnostic tools:

| Setting | Use when |
|---|---|
| Auto (default) | Most servers. Uses the OpenAI `tools` API, and falls back to JSON if the server rejects it |
| Native | The server supports `tools` and you want errors rather than a fallback. For vLLM, start it with `--enable-auto-tool-choice --tool-call-parser hermes` (Qwen); Ollama and llama.cpp (`--jinja`) support tools for models whose template has them |
| JSON | The server has no tool support. Agents answer with a JSON action instead |

Then open **AI Assistant** and describe a problem, for example *"Why is CPU high on web-01?"*.

1. The Supervisor classifies the request, may ask a clarifying question, and plans the
   investigation, streaming progress live.
2. Entities it names are checked against the inventory (section 5).
3. The diagnostics agents run read-only tools on the matched resources: system snapshot,
   processes, journal or event log, container state and logs, HTTP checks.
4. The Root Cause Analysis agent writes a report in which every claim cites that evidence.

Remote tools need the `tool.execute.readonly` permission (Operator and above). Without it,
agents use inventory data only.

### Embedding model (knowledge search)

For semantic knowledge search, add a second model with purpose **Embedding** and mark it
default. For example, with Ollama run `ollama pull nomic-embed-text` on the model host and
use base URL `http://host.docker.internal:11434` and model `nomic-embed-text`. **Test**
reports the vector dimension.

Without an embedding model, the knowledge base still works, with keyword search only. You can
also seed the model for every tenant with `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` in `.env`.
See [knowledge.md](knowledge.md).

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
- For the diagnostics agents and the **Logs** tab to read the systemd journal, add the user to
  the `systemd-journal` group (or `adm` on some distributions):
  `sudo usermod -aG systemd-journal localops`. Without it, `journalctl` returns only that
  user's own messages.
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
- To read the System and Application event logs (diagnostics and the **Logs** tab), also add
  the account to **Event Log Readers**.
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

## 6. Incidents

- **Automatic incidents:** when discovery or a health check finds a resource *unhealthy*,
  LocalOps opens an incident for it, one open incident per resource. Recovery is added to the
  incident's timeline, but the incident is not closed automatically. Set
  `INCIDENT_AUTO_CREATE=false` to turn this off.
- **Investigate:** press **Investigate** on an incident to start an investigation on its
  resources. The root cause and summary are written back to the incident when the
  investigation completes. Completed investigations also appear under **Reports**, where they
  can be printed or downloaded as Markdown.
- **Remediation:** after the root cause, the Remediation Planner may propose up to
  `REMEDIATION_MAX_STEPS` actions from the registered remediation tools. In `assisted` mode
  each step is sent for approval (**Approvals** in the sidebar); in `manual` mode you request
  approval for each step yourself, or skip it; in `autonomous` mode only steps that a policy
  marks as unattended (by default: restarting or starting a container in a non-production
  environment) run without a person. A second person approves, the action runs, and the
  Verification agent re-checks the system. The incident moves through `awaiting_approval`,
  `remediating` and `monitoring` (or back to `investigating` if verification fails).

Related settings in `.env`:

| Setting | Default | Meaning |
|---|---|---|
| `INVESTIGATION_MAX_TOOL_CALLS` | 30 | Diagnostic tool runs allowed per investigation |
| `TOOL_OUTPUT_MODEL_BYTES` | 6000 | Redacted tool output a model sees per run |
| `INCIDENT_AUTO_CREATE` | true | Open incidents for resources that turn unhealthy |
| `REMEDIATION_ENABLED` | true | Kill switch: `false` blocks every remediation action, even approved ones |
| `REMEDIATION_MAX_STEPS` | 3 | Steps the planner may propose per investigation |
| `APPROVAL_DEFAULT_TTL_MINUTES` | 60 | How long an approval request stays open |
| `VERIFICATION_DELAY_SECONDS` | 5 | Wait before re-checking a system after an action |

## 7. Remediation prerequisites

Diagnostics need read access only. Remediation needs the rights to make the change, and nothing
more. Grant them only on hosts where you want LocalOps to act.

**Linux.** Service actions and reboots use `sudo -n` (never a password prompt). Add a sudoers
file with `visudo -f /etc/sudoers.d/localops`:

```
# all services (simplest)
localops ALL=(root) NOPASSWD: /usr/bin/systemctl restart *, /usr/bin/systemctl start *, /usr/bin/systemctl stop *

# or only named units (tighter)
localops ALL=(root) NOPASSWD: /usr/bin/systemctl restart nginx, /usr/bin/systemctl restart payroll-api

# reboots (critical: always need a second approver)
localops ALL=(root) NOPASSWD: /usr/bin/systemctl reboot

# terminating other users' processes (the tool's use_sudo option)
localops ALL=(root) NOPASSWD: /usr/bin/kill
```

Replace `localops` with the SSH user of the integration. Without a rule, the action fails with a
clear "sudo" message and nothing changes. Processes owned by the SSH user can be terminated
without sudo.

**Windows.** The WinRM account needs the right to control the services it should restart. Use
local Administrators membership, or grant start/stop rights per service with
`sc.exe sdset <service> <SDDL>`. Reboots need the *Force shutdown from a remote system* user
right.

**Docker.** Membership of the `docker` group (SSH) or the TLS client certificate already allows
start, stop and restart.

**Approvers.** Create at least one user with the *Infrastructure Administrator* or *Tenant
Administrator* role besides the people who run investigations. By default nobody can approve
their own request.

## 8. Knowledge base and tools (Phase 6)

- **Knowledge Base → Upload** a runbook (Markdown, text or PDF) and try **Search**.
  Resolved and closed incidents are added automatically within about 5 minutes.
- **Tools & Integrations → Add → MCP Server** with the server's Streamable HTTP URL.
  - **Test**, then **Sync tools**.
  - Review each tool, set *read* or *change*, and enable it.
  - Allow read tools for an agent under **Agents → Configure**, or create a custom agent.
- **Custom HTTP tools**: add an **HTTP API** integration (base URL and authentication), then
  **Settings → Tools → New HTTP tool**.

## 9. Run the test suite (optional)

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
| Diagnostics report "journal" results with only a few lines | The SSH user is not in `systemd-journal` / `adm` |
| Windows event-log tools fail with access denied | Add the account to **Event Log Readers** |
| Agents never call tools, or the task shows "failed" with a model error | Use a tool-capable model, or set **Tool calling** to *JSON* for that model |
| A target fails with `host_key_changed` | The SSH host key differs from the pinned one. Verify it out of band, then **Reset pin** on the integration |
| Upgrade | `git pull && docker compose up -d --build` (migrations run automatically) |
| Stop | `docker compose down` (data volumes are kept) |
| Prometheus metrics | `http://api:8000/metrics` on the internal network (not exposed by nginx) |

Backup and restore scripts are delivered in Phase 7. Until then, back up the
`localops_postgres-data` volume with `pg_dump` (`docker compose exec postgres pg_dump -U postgres localops`).
