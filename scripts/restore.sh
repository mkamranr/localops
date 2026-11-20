#!/usr/bin/env bash
# Restore LocalOps from a backup made by scripts/backup.sh.
#
#   scripts/restore.sh ARCHIVE [--keys KEYS_FILE] [--keys-passphrase-file F] [--yes]
#
# Replaces the current database and vector collections with the backup's contents.
# With --keys, the application keys (APP_SECRET_KEY, LOCALOPS_ENCRYPTION_KEY) and the TLS
# private key are restored from the sealed keys file; this installation's own database
# passwords are kept. Without --keys, the current LOCALOPS_ENCRYPTION_KEY must be the one
# that was in use when the backup was taken; restore verifies this at the end.
# Works on a fresh installation (install.sh, then restore.sh) or an existing one.

# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ARCHIVE="" KEYS="" PASS_FILE="" YES=0

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keys) KEYS="$2"; shift 2 ;;
    --keys-passphrase-file) PASS_FILE="$2"; shift 2 ;;
    --yes | -y) YES=1; shift ;;
    -h | --help) usage 0 ;;
    -*) warn "unknown option $1"; usage 1 ;;
    *) ARCHIVE="$1"; shift ;;
  esac
done

require_cmd docker tar gzip sha256sum awk sed base64
[[ -n "$ARCHIVE" ]] || usage 1
[[ -f "$ARCHIVE" ]] || die "$ARCHIVE not found"
[[ -z "$KEYS" || -f "$KEYS" ]] || die "keys file $KEYS not found"
[[ -f "$PROJECT_DIR/.env" ]] || die "no .env in $PROJECT_DIR (install LocalOps first, then restore)"
DB="$(env_get POSTGRES_DB)"
DB="${DB:-localops}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 755 "$WORK"

# ------------------------------------------------------------------ 1. verify
info "Verifying $ARCHIVE"
tar -xzf "$ARCHIVE" -C "$WORK"
[[ -f "$WORK/manifest.json" && -f "$WORK/postgres.dump" ]] || die "$ARCHIVE is not a LocalOps backup"
(cd "$WORK" && sha256sum --quiet --strict -c SHA256SUMS) || die "backup checksums do not match: the archive is damaged"
ok "checksums match"
backup_version="$(sed -n 's/^  "localops_version": "\(.*\)",$/\1/p' "$WORK/manifest.json")"
created="$(sed -n 's/^  "created_at": "\(.*\)",$/\1/p' "$WORK/manifest.json")"
echo "  backup of LocalOps ${backup_version:-?} taken $created"
if grep -q '"keys": "managed-elsewhere"' "$WORK/manifest.json" && [[ -z "$KEYS" ]]; then
  warn "this backup was taken without a keys file; the current LOCALOPS_ENCRYPTION_KEY must match the original"
fi

# ------------------------------------------------------------------ 2. keys
if [[ -n "$KEYS" ]]; then
  info "Unsealing the keys file"
  PASSPHRASE="$(read_passphrase "$PASS_FILE" 0)"
  keys_tool unseal-keys "$PASSPHRASE" <"$KEYS" >"$WORK/keys.env" || die "cannot unseal $KEYS (wrong passphrase or damaged file)"
  chmod 600 "$WORK/keys.env"
  for key in APP_SECRET_KEY LOCALOPS_ENCRYPTION_KEY; do
    value="$(env_get "$key" "$WORK/keys.env")"
    [[ -n "$value" ]] || die "the keys file has no $key"
    secret_set "$key" "$value"
  done
  tls_key="$(env_get TLS_SERVER_KEY_B64 "$WORK/keys.env")"
  if [[ -n "$tls_key" && -f "$WORK/config/deployment/tls/server.crt" ]]; then
    mkdir -p "$PROJECT_DIR/deployment/tls"
    cp "$WORK/config/deployment/tls/server.crt" "$PROJECT_DIR/deployment/tls/server.crt"
    printf '%s' "$tls_key" | base64 -d >"$PROJECT_DIR/deployment/tls/server.key"
    chmod 644 "$PROJECT_DIR/deployment/tls/server.key"
    ok "TLS certificate and key restored"
  fi
  rm -f "$WORK/keys.env"
  ok "application keys restored"
fi

if [[ "$YES" == 0 ]]; then
  [[ -t 0 ]] || die "restore replaces all current data: re-run with --yes to confirm"
  read -rp "This REPLACES all current LocalOps data with the backup. Type 'restore' to continue: " answer </dev/tty
  [[ "$answer" == restore ]] || die "cancelled"
fi

# ------------------------------------------------------------------ 3. stop the application
info "Stopping the application services"
dc stop nginx frontend api worker >/dev/null 2>&1 || true
dc up -d --no-build postgres redis qdrant
for _ in $(seq 1 60); do
  [[ "$(dc ps postgres --format '{{.Health}}')" == healthy && "$(dc ps qdrant --format '{{.Health}}')" == healthy ]] && break
  sleep 2
done

# ------------------------------------------------------------------ 4. database
info "Restoring PostgreSQL ($DB)"
dc exec -T postgres dropdb -U postgres --if-exists --force "$DB"
# --create recreates the database with its owner, privileges and settings from the dump.
dc exec -T postgres pg_restore -U postgres -d postgres --create --exit-on-error <"$WORK/postgres.dump"
ok "database restored"

# ------------------------------------------------------------------ 5. vectors
if compgen -G "$WORK/qdrant/*.snapshot" >/dev/null; then
  info "Restoring Qdrant collections"
  dc run --rm --no-deps -T --user "$(id -u):$(id -g)" -v "$WORK/qdrant:/backup:ro" \
    api python -m app.cli qdrant-restore --in /backup
else
  warn "no vector snapshots in this backup: re-index knowledge documents after the restore"
fi

# ------------------------------------------------------------------ 6. start + checks
info "Starting LocalOps (migrations bring older backups up to date)"
if [[ "$(env_get COMPOSE_FILE)" == *docker-compose.bundle.yml* ]]; then
  dc up -d --no-build --force-recreate
else
  dc up -d --force-recreate
fi
wait_healthy 600
ok "all services healthy"

info "Checking stored credentials"
if dc exec -T api python -m app.cli check-secrets; then
  info "Restore complete"
else
  die "stored credentials cannot be decrypted: restore again with the matching keys file (--keys)"
fi
