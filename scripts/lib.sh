#!/usr/bin/env bash
# Shared helpers for the LocalOps operator scripts. Sourced, not executed.
#
# Layout (same in a source checkout and in an installation directory):
#   <project>/docker-compose*.yml  <project>/.env  <project>/deployment/  <project>/scripts/
# The set of compose files comes from COMPOSE_FILE in .env (written by install.sh), so
# every script runs plain `docker compose` from the project directory.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${LOCALOPS_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if [[ -t 1 ]]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_OFF=""
fi

info() { printf '%s==>%s %s\n' "$C_BOLD" "$C_OFF" "$*"; }
ok() { printf '  %sOK%s   %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn() { printf '%sWARNING:%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }
die() { printf '%sERROR:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is required but was not found"
  done
}

# docker compose, always run from the project directory.
dc() { (cd "$PROJECT_DIR" && docker compose "$@"); }

# Value of KEY in the project's .env ("" when absent). Strips surrounding quotes.
env_get() {
  local key="$1" file="${2:-$PROJECT_DIR/.env}" line
  [[ -f "$file" ]] || { printf ''; return 0; }
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  printf '%s' "$line"
}

# Set KEY=VALUE in an env file (replacing an existing line or appending one).
env_set() {
  local key="$1" value="$2" file="${3:-$PROJECT_DIR/.env}" tmp
  tmp="$(mktemp "${file}.XXXXXX")"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    $0 ~ "^" k "=" { if (!done) { print k "=" v; done = 1 }; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$file" >"$tmp"
  chmod --reference="$file" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  mv "$tmp" "$file"
}

# Random secrets (no external tools beyond coreutils).
rand_password() { head -c 32 /dev/urandom | base64 | tr -d '+/=\n' | cut -c1-32; }
rand_secret_key() { head -c 48 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_' | tr -d '='; }
rand_fernet_key() { head -c 32 /dev/urandom | base64 | tr -d '\n' | tr '+/' '-_'; }

# Secret settings: values live in .env, or in secrets/ files with docker-compose.secrets.yml.
# shellcheck disable=SC2034  # used by the scripts that source this file
SECRET_KEYS=(APP_SECRET_KEY POSTGRES_SUPERUSER_PASSWORD LOCALOPS_OWNER_PASSWORD
  LOCALOPS_APP_PASSWORD LOCALOPS_ENCRYPTION_KEY BOOTSTRAP_ADMIN_PASSWORD)

uses_docker_secrets() { [[ "$(env_get COMPOSE_FILE)" == *docker-compose.secrets.yml* ]]; }

secret_get() {
  local key="$1" lower
  lower="$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')"
  if uses_docker_secrets && [[ -f "$PROJECT_DIR/secrets/$lower" ]]; then
    tr -d '\n' <"$PROJECT_DIR/secrets/$lower"
  else
    env_get "$key"
  fi
}

secret_set() {
  local key="$1" value="$2" lower
  lower="$(printf '%s' "$key" | tr '[:upper:]' '[:lower:]')"
  if uses_docker_secrets; then
    mkdir -p "$PROJECT_DIR/secrets"
    chmod 700 "$PROJECT_DIR/secrets"
    printf '%s' "$value" >"$PROJECT_DIR/secrets/$lower"
    # Readable by the unprivileged container users through the bind mount; the
    # directory (mode 700) keeps other host users out.
    chmod 644 "$PROJECT_DIR/secrets/$lower"
    env_set "$key" "docker-secret"
  else
    env_set "$key" "$value"
  fi
}

backend_image() {
  local version
  version="$(env_get LOCALOPS_VERSION)"
  printf 'localops-backend:%s' "${version:-dev}"
}

# Wait until the core services are healthy (and migrations succeeded).
wait_healthy() {
  local timeout="${1:-300}" waited=0 state migrate svc pending
  while :; do
    migrate="$(dc ps -a migrate --format '{{.State}} {{.ExitCode}}' 2>/dev/null | head -n 1 || true)"
    if [[ "$migrate" == exited* && "$migrate" != "exited 0" ]]; then
      dc logs --no-color --tail 50 migrate >&2 || true
      die "database migration failed (see the log above)"
    fi
    pending=""
    for svc in postgres redis qdrant api worker frontend nginx; do
      state="$(dc ps "$svc" --format '{{.State}} {{.Health}}' 2>/dev/null | head -n 1 || true)"
      [[ "$state" == "running healthy" ]] || pending+=" $svc"
    done
    [[ -z "$pending" ]] && return 0
    if ((waited >= timeout)); then
      dc ps >&2 || true
      die "services not healthy after ${timeout}s:${pending} (check: docker compose logs${pending})"
    fi
    sleep 5
    waited=$((waited + 5))
  done
}

# Read a passphrase from a file or interactively (twice when confirm=1).
read_passphrase() {
  local file="$1" confirm="${2:-0}" first second
  if [[ -n "$file" ]]; then
    [[ -r "$file" ]] || die "cannot read passphrase file $file"
    head -n 1 "$file" | tr -d '\r\n'
    return 0
  fi
  [[ -t 0 ]] || die "a keys passphrase is required: use --keys-passphrase-file in non-interactive mode"
  read -rsp "Keys file passphrase (min 12 characters): " first </dev/tty; echo >&2
  if [[ "$confirm" == 1 ]]; then
    read -rsp "Repeat passphrase: " second </dev/tty; echo >&2
    [[ "$first" == "$second" ]] || die "passphrases do not match"
  fi
  [[ ${#first} -ge 12 ]] || die "passphrase must be at least 12 characters"
  printf '%s' "$first"
}

# Run seal-keys/unseal-keys in a network-less backend container; stdin -> stdout.
keys_tool() {
  local command="$1" passphrase="$2" tmpdir rc=0
  tmpdir="$(mktemp -d)"
  printf '%s' "$passphrase" >"$tmpdir/pass"
  chmod 644 "$tmpdir/pass"
  docker run --rm -i --network none -v "$tmpdir/pass:/run/pass:ro" "$(backend_image)" \
    python -m app.cli "$command" --passphrase-file /run/pass || rc=$?
  rm -rf "$tmpdir"
  return "$rc"
}
