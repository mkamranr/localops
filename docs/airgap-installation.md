# Air-gapped installation

LocalOps can be installed on a server with no internet access. You build an offline bundle on a connected machine, move it into the air gap, verify it, and install it with one script.

Nothing is downloaded at runtime:

- every image is loaded from the bundle;
- every image runs with `pull_policy: never`;
- the application has no telemetry.

## Overview

```
 connected build host                    air-gapped LocalOps host
 ───────────────────                     ─────────────────────────
 scripts/build-bundle.sh   ──transfer──▶  scripts/verify-bundle.sh
   images + SBOM + checksums               scripts/install.sh
                                            (your model server: Ollama / vLLM / llama.cpp)
```

## Requirements

| | Build host (connected) | LocalOps host (air-gapped) |
|---|---|---|
| OS / CPU | Linux x86_64 | Linux x86_64 |
| Docker | 24+ with buildx | 24+ |
| Docker Compose | v2 | v2.24+ |
| Other | git, internet access | coreutils, curl (for `security-check.sh`) |
| Disk | ~15 GB | ~20 GB plus data |
| Memory | | 8 GB minimum, 16 GB recommended |

The model server is separate and runs on GPU hardware inside the air gap. See [Models](#models).

## 1. Build the bundle (connected host)

```bash
git clone <your LocalOps repository> && cd localops
git checkout v1.0.0                     # build from a release tag
scripts/build-bundle.sh --scan          # --version is taken from the tag
```

The script does the following:

1. Builds the backend and frontend images for `linux/amd64`, labelled with the version, git revision and build date.
2. Pulls the pinned third-party images (PostgreSQL, Redis, Qdrant, nginx). The image list is read from `docker-compose.yml`, so it can't drift.
3. Saves every image into the bundle and records each image ID in `manifest.json`.
4. Writes an SBOM for every image in SPDX and CycloneDX format (with Syft) to `SBOM/`.
5. With `--scan`, runs a Trivy vulnerability report for every image. The build fails if it finds a CRITICAL vulnerability that has a fix available, unless you pass `--allow-critical`.
6. Adds the configuration, scripts, the model guide and these docs, then writes `checksums/SHA256SUMS`.

The output is `dist/localops-airgap-<version>-linux-amd64.tar.gz` plus a `.sha256` file.

## 2. Transfer and verify

Copy both files with your approved transfer process. On the air-gapped host:

```bash
sha256sum -c localops-airgap-1.0.0-linux-amd64.tar.gz.sha256
tar xzf localops-airgap-1.0.0-linux-amd64.tar.gz
cd localops-airgap-1.0.0-linux-amd64
scripts/verify-bundle.sh
```

`verify-bundle.sh` checks:

- every file against `SHA256SUMS`;
- that every image in the manifest is present;
- the OS and CPU, the Docker and Compose versions, and free disk space.

On a Windows transfer machine, use `scripts/verify-bundle.ps1 -Archive <file>` or `-BundleDir <dir>`.

## 3. Install

```bash
sudo scripts/install.sh
```

Or non-interactively:

```bash
sudo scripts/install.sh --non-interactive \
  --llm-url http://gpu-node-01:8000/v1 --llm-provider vllm --llm-model Qwen/Qwen2.5-14B-Instruct \
  --embedding-model BAAI/bge-m3 --san localops.corp.example --san 10.20.0.15
```

On a fresh install (default prefix `/opt/localops`, mode 700), `install.sh`:

1. Verifies the bundle, loads the images, and checks that each loaded image ID matches the manifest.
2. Installs the compose files, nginx and PostgreSQL configuration, scripts and docs into the prefix.
3. Writes `.env` (mode 600) with freshly generated secrets: `APP_SECRET_KEY`, the three database passwords, `LOCALOPS_ENCRYPTION_KEY` and the first administrator's password.
4. Checks the model server (from a container, the same way the API reaches it) and confirms that the configured models are listed. If they aren't, it warns: the AI features stay unavailable until the models are configured in **Settings > AI Models**.
5. Generates a self-signed TLS certificate (hostname, IPs, `localhost` and any `--san` values) unless `deployment/tls/server.crt` and `server.key` already exist.
6. Starts the stack and waits until every service is healthy.
7. Prints the HTTPS URL and the administrator password. The password is shown only once.

Sign in with tenant `system`, user `admin`. Then:

1. Change the administrator password.
2. **Run a first backup** to secure the keys: `/opt/localops/scripts/backup.sh` (see [backup.md](backup.md)).

### Options

| Option | Purpose |
|---|---|
| `--prefix DIR` | installation directory (default `/opt/localops`) |
| `--llm-url`, `--llm-model`, `--llm-provider` | chat model server (`ollama`, `vllm`, `llamacpp`, `openai_compatible`) |
| `--embedding-url`, `--embedding-model` | embedding model for semantic knowledge search (optional) |
| `--san NAME` | extra DNS name or IP for the self-signed certificate |
| `--http-port N`, `--https-port N` | published ports (defaults 8300, which redirects, and 8443) |
| `--docker-secrets` | keep secrets in `secrets/` files mounted as Docker secrets instead of in `.env` |
| `--no-tls` | plain HTTP only (not recommended; session cookies are then not `Secure`) |
| `--non-interactive` | never prompt |

## Day-2 operations

All operator scripts live in `/opt/localops/scripts`:

| Script | |
|---|---|
| `status.sh` | service state, health, readiness |
| `start.sh` / `stop.sh` | start or stop (data is kept) |
| `backup.sh` / `restore.sh` | see [backup.md](backup.md) |
| `security-check.sh` | hardening, TLS, headers, exposed ports, rate limiting (`--airgap` also proves there is no internet route) |

To run `docker compose` by hand, run it from `/opt/localops`. The compose file set comes from `COMPOSE_FILE` in `.env`.

## TLS certificates

The installer creates a self-signed certificate, so browsers show a warning until you trust it. To use a certificate from your own CA:

```bash
cp your.crt /opt/localops/deployment/tls/server.crt     # full chain, PEM
cp your.key /opt/localops/deployment/tls/server.key     # PEM, unencrypted
chmod 644 /opt/localops/deployment/tls/server.key       # nginx runs unprivileged; the prefix itself is mode 700
cd /opt/localops && docker compose restart nginx
```

- nginx accepts only TLS 1.2 and 1.3, with modern ciphers.
- HTTP requests to `LOCALOPS_HTTP_PORT` get a permanent redirect to HTTPS.
- HSTS is sent. Browsers ignore it while the certificate is untrusted.

## Docker secrets

With `--docker-secrets`, each secret is stored in `/opt/localops/secrets/<name>` (directory mode 700) and mounted into the containers as a Docker secret. In `.env`, the matching variables hold the placeholder `docker-secret`.

The application reads `<NAME>_FILE` for every setting, for example `LOCALOPS_ENCRYPTION_KEY_FILE`. PostgreSQL reads `POSTGRES_PASSWORD_FILE`. The configuration is in `docker-compose.secrets.yml`.

## Models

LocalOps doesn't bundle model weights or a model runtime. Bring your own OpenAI-compatible server. `models/README.md` in the bundle explains how to move Ollama, vLLM or llama.cpp models into the air gap. `models/manifest.json` lists the recommended models.

## Upgrades

1. Build and transfer the new bundle.
2. Run its `install.sh` against the same prefix:

```bash
cd localops-airgap-1.1.0-linux-amd64
sudo scripts/install.sh --backup-before-upgrade
```

An upgrade:

- keeps `.env`, the secrets, the TLS certificate and all data volumes;
- loads the new images;
- applies database migrations automatically;
- offers to take a backup first (interactive mode, or `--backup-before-upgrade`).

## What "air-gapped" means here

- **No registry access.** All images come from the bundle, and `docker-compose.bundle.yml` sets `pull_policy: never` on every service.
- **Nothing downloaded at build time inside the air gap.** Python and Node dependencies are already inside the images, installed from lockfiles. The frontend loads no CDN assets.
- **No telemetry.** Application telemetry is off. LangSmith tracing is disabled. Qdrant telemetry is disabled. `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` and `DO_NOT_TRACK=1` are set.
- **Proved in CI.** The air-gap smoke workflow installs the bundle with the application network marked `internal: true` (`docker-compose.airgap-test.yml`). It then runs the security checks, uses a local stub model, and does a backup, wipe, reinstall and restore.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `verify-bundle.sh` reports checksum failures | The transfer corrupted or truncated a file. Copy it again and compare it with the `.sha256` file. |
| `image ... does not match the manifest` | An image with the same tag but different content already exists locally. Remove it (`docker rmi`) and re-run. |
| `services not healthy` | `cd /opt/localops && docker compose logs <service>`. The migrate service log shows database errors. |
| Model check reports `unreachable` | The URL must be reachable from containers. For a server on the LocalOps host itself, use `host.docker.internal`, and make Ollama listen on `0.0.0.0`. |
| `LOCALOPS_ENCRYPTION_KEY must be set in production` | The key is required in production. Restore it from the keys file (`restore.sh --keys`) or the original `.env`. Don't generate a new one if credentials are already stored: they would become unreadable. |
| Browser warns about the certificate | The certificate is self-signed. Trust `deployment/tls/server.crt`, or install a CA-issued certificate. |
