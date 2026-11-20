#!/usr/bin/env bash
# Verify an extracted LocalOps offline bundle before installing it.
#   scripts/verify-bundle.sh [--no-host-checks]
# Checks every file against checksums/SHA256SUMS, that every image named in
# manifest.json is present, and (unless --no-host-checks) that this host can run it:
# Linux x86_64, Docker >= 24, Docker Compose v2 >= 2.24 and enough free disk space.
# Exit status is non-zero on any problem.

# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HOST_CHECKS=1
[[ "${1:-}" == "--no-host-checks" ]] && HOST_CHECKS=0

require_cmd sha256sum awk grep sed
cd "$BUNDLE_DIR"
[[ -f manifest.json && -f checksums/SHA256SUMS ]] || die "$BUNDLE_DIR is not a LocalOps bundle (manifest.json or checksums/SHA256SUMS missing)"

failures=0
fail() { printf '  %sFAIL%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; failures=$((failures + 1)); }

version="$(sed -n 's/^  "version": "\(.*\)",$/\1/p' manifest.json)"
info "Verifying LocalOps bundle ${version:-?} in $BUNDLE_DIR"

# ------------------------------------------------------------------ integrity
if sha256sum --quiet --strict -c checksums/SHA256SUMS; then
  ok "all $(wc -l <checksums/SHA256SUMS) files match checksums/SHA256SUMS"
else
  fail "checksum verification failed: the bundle is incomplete or was modified in transit"
fi

# ------------------------------------------------------------------ completeness
mapfile -t image_files < <(grep -o '"file": "[^"]*"' manifest.json | sed 's/"file": "\(.*\)"/\1/')
((${#image_files[@]} > 0)) || fail "manifest.json lists no images"
for file in "${image_files[@]}"; do
  [[ -s "$file" ]] || fail "missing image archive $file"
  grep -q "  $file\$" checksums/SHA256SUMS || fail "$file is not covered by SHA256SUMS"
done
for required in config/docker-compose.yml config/docker-compose.bundle.yml config/.env.example \
  config/deployment/nginx/nginx.conf config/deployment/nginx/nginx-tls.conf \
  config/deployment/postgres/init/01-roles.sh scripts/install.sh models/manifest.json; do
  [[ -f "$required" ]] || fail "missing $required"
done
((failures == 0)) && ok "${#image_files[@]} images and all required files present"

# ------------------------------------------------------------------ host
version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n 1)" == "$2" ]]; }

if [[ "$HOST_CHECKS" == 1 ]]; then
  [[ "$(uname -s)" == "Linux" ]] || fail "this bundle needs Linux (found $(uname -s))"
  [[ "$(uname -m)" == "x86_64" ]] || fail "this bundle is for x86_64 (found $(uname -m))"
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    dv="$(docker version -f '{{.Server.Version}}' 2>/dev/null || echo 0)"
    version_ge "$dv" 24.0 && ok "Docker $dv" || fail "Docker $dv is too old (need 24.0 or newer)"
    cv="$(docker compose version --short 2>/dev/null || echo 0)"
    cv="${cv#v}"
    version_ge "$cv" 2.24 && ok "Docker Compose $cv" || fail "Docker Compose v2.24 or newer is required (found ${cv:-none})"
    root="$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
    need_kb=$(($(du -sk images | cut -f1) * 3 + 5 * 1024 * 1024))
    avail_kb="$(df -Pk "$root" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [[ -n "$avail_kb" ]] && ((avail_kb < need_kb)); then
      fail "only $((avail_kb / 1024 / 1024)) GiB free under $root; about $((need_kb / 1024 / 1024)) GiB needed"
    else
      ok "disk space under $root"
    fi
  else
    fail "Docker is not installed or not running (or this user cannot access it)"
  fi
fi

if ((failures > 0)); then
  die "bundle verification failed ($failures problem(s))"
fi
info "Bundle verified"
