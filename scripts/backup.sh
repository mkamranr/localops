#!/usr/bin/env bash
# Back up LocalOps: PostgreSQL, Qdrant vectors, configuration, plus a separate
# passphrase-sealed keys file.
#
#   scripts/backup.sh [options]
#   scripts/backup.sh --verify backups/localops-backup-<ts>.tar.gz
#
# Options:
#   --out DIR                    backup directory (default <install>/backups)
#   --keys-passphrase-file F     passphrase for the keys file (else prompted)
#   --keys-managed-elsewhere     no keys file (you keep the keys another way); recorded
#                                in the backup and warned about on restore
#   --keys-only                  write only the keys file (e.g. once, before scheduled backups)
#   --no-vectors                 skip Qdrant (vectors can be rebuilt by re-indexing knowledge)
#   --keep N                     keep the newest N backups and keys files (default 7, 0 = all)
#   --verify ARCHIVE             check an existing backup archive and exit
#
# Output: localops-backup-<ts>.tar.gz and localops-keys-<ts>.enc. Store the keys file
# separately from the backup archive. See BACKUP.md / docs/backup.md.

# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT="" PASS_FILE="" KEYS_ELSEWHERE=0 KEYS_ONLY=0 VECTORS=1 KEEP=7 VERIFY=""

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --keys-passphrase-file) PASS_FILE="$2"; shift 2 ;;
    --keys-managed-elsewhere) KEYS_ELSEWHERE=1; shift ;;
    --keys-only) KEYS_ONLY=1; shift ;;
    --no-vectors) VECTORS=0; shift ;;
    --keep) KEEP="$2"; shift 2 ;;
    --verify) VERIFY="$2"; shift 2 ;;
    -h | --help) usage 0 ;;
    *) warn "unknown option $1"; usage 1 ;;
  esac
done

require_cmd docker tar gzip sha256sum awk sed
[[ -f "$PROJECT_DIR/.env" ]] || die "no .env in $PROJECT_DIR"
[[ "$KEEP" =~ ^[0-9]+$ ]] || die "--keep needs a number"
DB="$(env_get POSTGRES_DB)"
DB="${DB:-localops}"

# ------------------------------------------------------------------ verify mode
verify_archive() {
  local archive="$1" tmp rc=0
  [[ -f "$archive" ]] || die "$archive not found"
  tmp="$(mktemp -d)"
  tar -xzf "$archive" -C "$tmp"
  [[ -f "$tmp/manifest.json" && -f "$tmp/SHA256SUMS" ]] || { rm -rf "$tmp"; die "$archive is not a LocalOps backup"; }
  (cd "$tmp" && sha256sum --quiet --strict -c SHA256SUMS) || rc=1
  [[ "$rc" == 0 ]] && ok "checksums match" || warn "checksum mismatch"
  if dc run --rm --no-deps -T --entrypoint pg_restore postgres --list <"$tmp/postgres.dump" >/dev/null; then
    ok "PostgreSQL dump is readable ($(dc run --rm --no-deps -T --entrypoint pg_restore postgres --list <"$tmp/postgres.dump" | grep -c ' TABLE DATA ') tables)"
  else
    warn "PostgreSQL dump is not readable"; rc=1
  fi
  if [[ -d "$tmp/qdrant" ]]; then
    local snap
    for snap in "$tmp"/qdrant/*.snapshot; do
      [[ -e "$snap" ]] || continue
      if tar -tf "$snap" >/dev/null 2>&1; then ok "Qdrant snapshot $(basename "$snap")"; else warn "corrupt snapshot $(basename "$snap")"; rc=1; fi
    done
  else
    ok "no vector snapshots in this backup (re-index knowledge after restoring)"
  fi
  grep -q '"keys": "managed-elsewhere"' "$tmp/manifest.json" && warn "this backup was taken without a keys file"
  rm -rf "$tmp"
  return "$rc"
}

if [[ -n "$VERIFY" ]]; then
  info "Verifying $VERIFY"
  verify_archive "$VERIFY" || die "backup verification failed"
  info "Backup verified"
  exit 0
fi

# ------------------------------------------------------------------ setup
OUT="${OUT:-$PROJECT_DIR/backups}"
mkdir -p "$OUT"
chmod 700 "$OUT"
OUT="$(cd "$OUT" && pwd)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
ARCHIVE="$OUT/localops-backup-$TS.tar.gz"
KEYS_FILE="$OUT/localops-keys-$TS.enc"

# The keys are never silently left out of a backup.
if [[ "$KEYS_ELSEWHERE" == 0 ]]; then
  PASSPHRASE="$(read_passphrase "$PASS_FILE" 1)"
  [[ ${#PASSPHRASE} -ge 12 ]] || die "the keys passphrase must be at least 12 characters"
elif [[ "$KEYS_ONLY" == 1 ]]; then
  die "--keys-only and --keys-managed-elsewhere contradict each other"
fi

# Key material: application keys + every generated password + the TLS private key.
keys_plaintext() {
  local key
  echo "# LocalOps keys, $(date -u +%Y-%m-%dT%H:%M:%SZ), version $(env_get LOCALOPS_VERSION)"
  for key in "${SECRET_KEYS[@]}"; do
    printf '%s=%s\n' "$key" "$(secret_get "$key")"
  done
  if [[ -f "$PROJECT_DIR/deployment/tls/server.key" ]]; then
    printf 'TLS_SERVER_KEY_B64=%s\n' "$(base64 -w0 <"$PROJECT_DIR/deployment/tls/server.key")"
  fi
}

write_keys() {
  info "Sealing the keys file"
  keys_plaintext | keys_tool seal-keys "$PASSPHRASE" >"$KEYS_FILE.tmp" || { rm -f "$KEYS_FILE.tmp"; die "sealing the keys failed"; }
  mv "$KEYS_FILE.tmp" "$KEYS_FILE"
  chmod 600 "$KEYS_FILE"
  ok "$KEYS_FILE"
}

if [[ "$KEYS_ONLY" == 1 ]]; then
  write_keys
  echo "Store the keys file and its passphrase apart from the backups."
  exit 0
fi

# ------------------------------------------------------------------ data
for svc in postgres qdrant; do
  [[ "$(dc ps "$svc" --format '{{.State}}' 2>/dev/null)" == running ]] || die "$svc is not running (start LocalOps first)"
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK" "$KEYS_FILE.tmp" "$ARCHIVE.tmp"' EXIT
chmod 755 "$WORK"

info "Dumping PostgreSQL ($DB)"
dc exec -T postgres pg_dump -U postgres -d "$DB" -Fc >"$WORK/postgres.dump"
ok "postgres.dump ($(du -h "$WORK/postgres.dump" | cut -f1))"
REVISION="$(dc exec -T postgres psql -U postgres -d "$DB" -Atc 'SELECT version_num FROM alembic_version' | tr -d '\r')"

if [[ "$VECTORS" == 1 ]]; then
  info "Snapshotting Qdrant collections"
  mkdir -p "$WORK/qdrant"
  dc run --rm --no-deps -T --user "$(id -u):$(id -g)" -v "$WORK/qdrant:/backup" \
    api python -m app.cli qdrant-snapshot --out /backup
fi

info "Copying configuration"
mkdir -p "$WORK/config/deployment"
cp "$PROJECT_DIR"/docker-compose*.yml "$WORK/config/"
cp -r "$PROJECT_DIR/deployment/nginx" "$WORK/config/deployment/"
[[ -f "$PROJECT_DIR/deployment/tls/server.crt" ]] && mkdir -p "$WORK/config/deployment/tls" &&
  cp "$PROJECT_DIR/deployment/tls/server.crt" "$WORK/config/deployment/tls/"
secret_pattern="$(IFS='|'; echo "${SECRET_KEYS[*]}")"
sed -E "s/^(${secret_pattern})=.*/\1=<redacted: see the keys file>/" "$PROJECT_DIR/.env" >"$WORK/config/env.redacted"

files_json=""
while IFS= read -r f; do
  files_json+="$(printf '    {"path": "%s", "size": %s, "sha256": "%s"},' "$f" "$(stat -c %s "$WORK/$f")" "$(sha256sum "$WORK/$f" | cut -d' ' -f1)")"$'\n'
done < <(cd "$WORK" && find . -type f -printf '%P\n' | LC_ALL=C sort)
files_json="${files_json%,$'\n'}"
cat >"$WORK/manifest.json" <<EOF
{
  "format": "localops-backup",
  "format_version": 1,
  "localops_version": "$(env_get LOCALOPS_VERSION)",
  "alembic_revision": "$REVISION",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "database": "$DB",
  "vectors": $([[ "$VECTORS" == 1 ]] && echo true || echo false),
  "keys": "$([[ "$KEYS_ELSEWHERE" == 1 ]] && echo managed-elsewhere || echo "localops-keys-$TS.enc")",
  "files": [
$files_json
  ]
}
EOF
# shellcheck disable=SC2094  # SHA256SUMS is excluded from the list it is written to
(cd "$WORK" && find . -type f ! -name SHA256SUMS -printf '%P\n' | LC_ALL=C sort | xargs -d '\n' sha256sum >SHA256SUMS)

info "Writing $ARCHIVE"
tar -C "$WORK" -czf "$ARCHIVE.tmp" .
mv "$ARCHIVE.tmp" "$ARCHIVE"
chmod 600 "$ARCHIVE"
ok "$ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"

if [[ "$KEYS_ELSEWHERE" == 0 ]]; then
  write_keys
else
  warn "no keys file written (--keys-managed-elsewhere): restoring needs the LOCALOPS_ENCRYPTION_KEY that was in use"
fi

# ------------------------------------------------------------------ retention
if ((KEEP > 0)); then
  for pattern in 'localops-backup-*.tar.gz' 'localops-keys-*.enc'; do
    mapfile -t old < <(find "$OUT" -maxdepth 1 -name "$pattern" -printf '%f\n' | LC_ALL=C sort -r | tail -n +$((KEEP + 1)))
    for f in "${old[@]}"; do rm -f "$OUT/$f" && echo "  pruned $f"; done
  done
fi

info "Backup complete"
[[ "$KEYS_ELSEWHERE" == 0 ]] && echo "  Keep $(basename "$KEYS_FILE") and its passphrase apart from the backup archive."
exit 0
