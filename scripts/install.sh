#!/usr/bin/env bash
# Install or upgrade LocalOps from an extracted offline bundle (no internet needed).
#
#   sudo scripts/install.sh [options]
#
# Options:
#   --prefix DIR             installation directory (default /opt/localops)
#   --non-interactive        never prompt (use flags / defaults)
#   --llm-url URL            model server, e.g. http://gpu-node:8000/v1 or http://host.docker.internal:11434
#   --llm-model NAME         chat model name as the server lists it
#   --llm-provider P         ollama | vllm | llamacpp | openai_compatible (default ollama)
#   --embedding-url URL      embedding server (default: same as --llm-url)
#   --embedding-model NAME   embedding model (optional; enables semantic knowledge search)
#   --san NAME               extra DNS name or IP for the self-signed certificate (repeat)
#   --no-tls                 serve plain HTTP only (not recommended)
#   --docker-secrets         keep secrets in ./secrets files (Docker secrets) instead of .env
#   --http-port N            published HTTP port (default 8300; redirects to HTTPS)
#   --https-port N           published HTTPS port (default 8443)
#   --extra-compose FILE     add a compose override file (repeatable; copied into the prefix)
#   --backup-before-upgrade  run scripts/backup.sh before upgrading an existing installation
#   --keys-passphrase-file F passphrase for that backup's keys file
#   --skip-verify            skip bundle verification (not recommended)
#
# A fresh install generates every secret, a self-signed TLS certificate and the first
# administrator password (printed once). An upgrade keeps .env, secrets, certificates
# and data volumes. See README-OFFLINE.md.

# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PREFIX="/opt/localops"
INTERACTIVE=1
[[ -t 0 ]] || INTERACTIVE=0
LLM_URL="" LLM_MODEL="" LLM_PROVIDER="" EMB_URL="" EMB_MODEL=""
SANS=() EXTRA_COMPOSE=()
answer=""
TLS=1 SECRETS_MODE=0 HTTP_PORT="" HTTPS_PORT="" VERIFY=1 BACKUP_FIRST=0 KEYS_PASS_FILE=""

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --non-interactive) INTERACTIVE=0; shift ;;
    --llm-url) LLM_URL="$2"; shift 2 ;;
    --llm-model) LLM_MODEL="$2"; shift 2 ;;
    --llm-provider) LLM_PROVIDER="$2"; shift 2 ;;
    --embedding-url) EMB_URL="$2"; shift 2 ;;
    --embedding-model) EMB_MODEL="$2"; shift 2 ;;
    --san) SANS+=("$2"); shift 2 ;;
    --no-tls) TLS=0; shift ;;
    --docker-secrets) SECRETS_MODE=1; shift ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --https-port) HTTPS_PORT="$2"; shift 2 ;;
    --extra-compose) EXTRA_COMPOSE+=("$2"); shift 2 ;;
    --backup-before-upgrade) BACKUP_FIRST=1; shift ;;
    --keys-passphrase-file) KEYS_PASS_FILE="$2"; shift 2 ;;
    --skip-verify) VERIFY=0; shift ;;
    -h | --help) usage 0 ;;
    *) warn "unknown option $1"; usage 1 ;;
  esac
done

ask() { # ask VAR "question" default
  local __var="$1" question="$2" default="${3:-}" reply
  if [[ "$INTERACTIVE" == 1 ]]; then
    read -rp "$question${default:+ [$default]}: " reply </dev/tty
    printf -v "$__var" '%s' "${reply:-$default}"
  else
    printf -v "$__var" '%s' "$default"
  fi
}

require_cmd docker awk sed grep tar
[[ -f "$BUNDLE_DIR/manifest.json" ]] || die "run this script from an extracted LocalOps bundle"
VERSION="$(sed -n 's/^  "version": "\(.*\)",$/\1/p' "$BUNDLE_DIR/manifest.json")"
[[ -n "$VERSION" ]] || die "cannot read the bundle version from manifest.json"

# ------------------------------------------------------------------ 1. verify
if [[ "$VERIFY" == 1 ]]; then
  "$SCRIPT_DIR/verify-bundle.sh"
else
  warn "skipping bundle verification"
fi

mkdir -p "$PREFIX"
chmod 700 "$PREFIX"
PREFIX="$(cd "$PREFIX" && pwd)"
export LOCALOPS_HOME="$PREFIX"
PROJECT_DIR="$PREFIX"
UPGRADE=0
[[ -f "$PREFIX/.env" ]] && UPGRADE=1

if [[ "$UPGRADE" == 1 ]]; then
  info "Upgrading the LocalOps installation in $PREFIX to $VERSION"
  if [[ "$BACKUP_FIRST" == 0 && "$INTERACTIVE" == 1 ]]; then
    ask answer "Back up the current installation first? [Y/n]" "Y"
    [[ "$answer" =~ ^[Nn] ]] || BACKUP_FIRST=1
  fi
  if [[ "$BACKUP_FIRST" == 1 ]]; then
    [[ -x "$PREFIX/scripts/backup.sh" ]] || die "no backup script in $PREFIX/scripts"
    "$PREFIX/scripts/backup.sh" ${KEYS_PASS_FILE:+--keys-passphrase-file "$KEYS_PASS_FILE"}
  fi
else
  info "Installing LocalOps $VERSION into $PREFIX"
fi

# ------------------------------------------------------------------ 2. load images
info "Loading container images"
while IFS= read -r line; do
  ref="$(sed -n 's/.*"ref": "\([^"]*\)".*/\1/p' <<<"$line")"
  file="$(sed -n 's/.*"file": "\([^"]*\)".*/\1/p' <<<"$line")"
  want="$(sed -n 's/.*"image_id": "\([^"]*\)".*/\1/p' <<<"$line")"
  [[ -n "$ref" && -n "$file" && -n "$want" ]] || continue
  docker load -q -i "$BUNDLE_DIR/$file" >/dev/null
  have="$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null || true)"
  [[ "$have" == "$want" ]] || die "image $ref does not match the manifest (got ${have:-nothing}, expected $want)"
  ok "$ref"
done < <(grep '"ref":' "$BUNDLE_DIR/manifest.json")

# ------------------------------------------------------------------ 3. files
info "Installing configuration and scripts"
mkdir -p "$PREFIX/deployment/tls" "$PREFIX/scripts" "$PREFIX/models" "$PREFIX/backups"
cp "$BUNDLE_DIR"/config/docker-compose*.yml "$BUNDLE_DIR/config/.env.example" "$PREFIX/"
rm -rf "$PREFIX/deployment/nginx" "$PREFIX/deployment/postgres"
cp -r "$BUNDLE_DIR/config/deployment/nginx" "$BUNDLE_DIR/config/deployment/postgres" "$PREFIX/deployment/"
cp "$BUNDLE_DIR"/scripts/* "$PREFIX/scripts/"
chmod +x "$PREFIX"/scripts/*.sh
cp "$BUNDLE_DIR"/models/* "$PREFIX/models/"
cp "$BUNDLE_DIR/README-OFFLINE.md" "$BUNDLE_DIR/manifest.json" "$PREFIX/"
[[ -f "$BUNDLE_DIR/BACKUP.md" ]] && cp "$BUNDLE_DIR/BACKUP.md" "$PREFIX/"
extra_names=()
for f in "${EXTRA_COMPOSE[@]}"; do
  [[ -f "$f" ]] || die "compose file $f not found"
  cp "$f" "$PREFIX/"
  extra_names+=("$(basename "$f")")
done

# ------------------------------------------------------------------ 4. .env
ADMIN_PASSWORD=""
if [[ "$UPGRADE" == 0 ]]; then
  cp "$PREFIX/.env.example" "$PREFIX/.env"
  chmod 600 "$PREFIX/.env"
fi
compose_files="docker-compose.yml:docker-compose.bundle.yml"
if [[ "$UPGRADE" == 1 ]]; then
  current="$(env_get COMPOSE_FILE)"
  [[ "$current" == *docker-compose.tls.yml* ]] || TLS=0
  [[ "$current" == *docker-compose.secrets.yml* ]] && SECRETS_MODE=1
  for f in ${current//:/ }; do
    case "$f" in
      docker-compose.yml | docker-compose.bundle.yml | docker-compose.tls.yml | docker-compose.secrets.yml) ;;
      *) [[ " ${extra_names[*]} " == *" $f "* ]] || extra_names+=("$f") ;;
    esac
  done
fi
[[ "$TLS" == 1 ]] && compose_files+=":docker-compose.tls.yml"
[[ "$SECRETS_MODE" == 1 ]] && compose_files+=":docker-compose.secrets.yml"
for f in "${extra_names[@]}"; do compose_files+=":$f"; done
env_set COMPOSE_FILE "$compose_files"
env_set LOCALOPS_VERSION "$VERSION"
env_set APP_ENV production
[[ -n "$HTTP_PORT" ]] && env_set LOCALOPS_HTTP_PORT "$HTTP_PORT"
[[ -n "$HTTPS_PORT" ]] && env_set LOCALOPS_HTTPS_PORT "$HTTPS_PORT"
[[ -n "$(env_get LOCALOPS_HTTPS_PORT)" ]] || env_set LOCALOPS_HTTPS_PORT 8443
env_set COOKIE_SECURE "$([[ "$TLS" == 1 ]] && echo true || echo false)"

if [[ "$UPGRADE" == 0 ]]; then
  info "Generating secrets"
  ADMIN_PASSWORD="$(rand_password)"
  secret_set APP_SECRET_KEY "$(rand_secret_key)"
  secret_set POSTGRES_SUPERUSER_PASSWORD "$(rand_password)"
  secret_set LOCALOPS_OWNER_PASSWORD "$(rand_password)"
  secret_set LOCALOPS_APP_PASSWORD "$(rand_password)"
  secret_set LOCALOPS_ENCRYPTION_KEY "$(rand_fernet_key)"
  secret_set BOOTSTRAP_ADMIN_PASSWORD "$ADMIN_PASSWORD"
fi
for key in "${SECRET_KEYS[@]}"; do
  value="$(secret_get "$key")"
  [[ -n "$value" && "$value" != CHANGE_ME* ]] || die "$key is not set in $PREFIX/.env"
done

# ------------------------------------------------------------------ 5. model server
if [[ "$UPGRADE" == 0 && -z "$LLM_URL" ]]; then
  echo "LocalOps uses your own model server (Ollama, vLLM, llama.cpp or any OpenAI-compatible API)."
  echo "Leave blank to configure it later in Settings > AI Models."
  ask LLM_URL "Model server URL (e.g. http://gpu-node:8000/v1)" ""
  if [[ -n "$LLM_URL" ]]; then
    ask LLM_PROVIDER "Provider (ollama|vllm|llamacpp|openai_compatible)" "ollama"
    ask LLM_MODEL "Chat model name" ""
    ask EMB_MODEL "Embedding model name (optional, e.g. nomic-embed-text)" ""
  fi
fi
[[ -n "$LLM_URL" ]] && env_set LLM_BASE_URL "$LLM_URL"
[[ -n "$LLM_MODEL" ]] && env_set LLM_MODEL "$LLM_MODEL"
[[ -n "$LLM_PROVIDER" ]] && env_set LLM_PROVIDER "$LLM_PROVIDER"
if [[ -n "$EMB_MODEL" ]]; then
  env_set EMBEDDING_MODEL "$EMB_MODEL"
  env_set EMBEDDING_BASE_URL "${EMB_URL:-$(env_get LLM_BASE_URL)}"
  env_set EMBEDDING_PROVIDER "${LLM_PROVIDER:-$(env_get LLM_PROVIDER)}"
fi

# Probe from a container (like the API will), listing the models the server offers.
probe_model() {
  local url="$1" model="$2"
  docker run --rm --add-host host.docker.internal:host-gateway -e URL="$url" -e MODEL="$model" \
    "$(backend_image)" python -c '
import json, os, sys, urllib.request
url, model = os.environ["URL"].rstrip("/"), os.environ["MODEL"]
base = url[:-3] if url.endswith("/v1") else url
names = set()
for path, key in (("/v1/models", "data"), ("/api/tags", "models")):
    try:
        with urllib.request.urlopen(base + path, timeout=8) as r:
            for m in json.load(r).get(key, []):
                names.add(m.get("id") or m.get("name") or "")
    except Exception:
        pass
if not names:
    print("unreachable"); sys.exit(2)
ok = not model or model in names or model + ":latest" in names
print("ok" if ok else "missing: available models: " + ", ".join(sorted(n for n in names if n)))
sys.exit(0 if ok else 1)
' 2>/dev/null
}

if [[ -n "$(env_get LLM_BASE_URL)" ]]; then
  info "Checking the model server"
  for pair in "$(env_get LLM_BASE_URL)|$(env_get LLM_MODEL)" "$(env_get EMBEDDING_BASE_URL)|$(env_get EMBEDDING_MODEL)"; do
    url="${pair%%|*}" model="${pair#*|}"
    [[ -n "$url" ]] || continue
    if result="$(probe_model "$url" "$model")"; then
      ok "$url ${model:+serves $model}"
    else
      warn "$url: ${result:-unreachable}. AI features stay unavailable until the model server and model are available (Settings > AI Models)."
    fi
  done
fi

# ------------------------------------------------------------------ 6. TLS
if [[ "$TLS" == 1 ]]; then
  https_port="$(env_get LOCALOPS_HTTPS_PORT)"
  sed -i "s/set \$https_port [0-9]*;/set \$https_port ${https_port:-8443};/" "$PREFIX/deployment/nginx/nginx-tls.conf"
  if [[ -f "$PREFIX/deployment/tls/server.crt" && -f "$PREFIX/deployment/tls/server.key" ]]; then
    ok "using the existing certificate in $PREFIX/deployment/tls"
  else
    info "Generating a self-signed TLS certificate"
    names=("${SANS[@]}" "$(hostname -f 2>/dev/null || hostname)" "$(hostname -s 2>/dev/null || hostname)" localhost 127.0.0.1)
    read -ra ips <<<"$(hostname -I 2>/dev/null || true)"
    names+=("${ips[@]}")
    name_args=()
    for n in "${names[@]}"; do [[ -n "$n" ]] && name_args+=(--name "$n"); done
    docker run --rm --network none --user "$(id -u):$(id -g)" \
      -v "$PREFIX/deployment/tls:/tls" "$(backend_image)" \
      python -m app.cli gen-cert --out-dir /tls "${name_args[@]}"
    warn "the certificate is self-signed: browsers will warn until you trust it or replace deployment/tls/server.crt and server.key with one from your CA"
  fi
fi

# ------------------------------------------------------------------ 7. start
info "Starting LocalOps"
dc up -d --no-build --remove-orphans
wait_healthy 600
ok "all services healthy"

http_port="$(env_get LOCALOPS_HTTP_PORT)"
host="$(hostname -f 2>/dev/null || hostname)"
if [[ "$TLS" == 1 ]]; then
  url="https://$host:$(env_get LOCALOPS_HTTPS_PORT)"
else
  url="http://$host:${http_port:-8300}"
fi
echo
info "LocalOps $VERSION is running: $url"
if [[ "$UPGRADE" == 0 ]]; then
  echo "  Sign in with tenant 'system', user '$(env_get BOOTSTRAP_ADMIN_USERNAME)', password:"
  echo "    $ADMIN_PASSWORD"
  echo "  This password is shown only once; change it after signing in."
  echo "  Back up the keys now: $PREFIX/scripts/backup.sh  (see BACKUP.md)"
fi
echo "  Manage: $PREFIX/scripts/{status,stop,start,backup,restore}.sh"
