#!/usr/bin/env bash
# Security checks against a running LocalOps installation.
#
#   scripts/security-check.sh [--host HOST] [--airgap]
#
# Container hardening (non-root users, no-new-privileges, dropped capabilities,
# read-only root filesystems), exposed ports, TLS and security headers, internal
# endpoints not reachable through the proxy, login rate limiting, and with --airgap,
# that the application containers have no route to the internet.
# Exit status is the number of failed checks (0 = all passed).

# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

HOST="127.0.0.1" AIRGAP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --airgap) AIRGAP=1; shift ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
done
require_cmd docker curl

failures=0
pass() { ok "$*"; }
fail() { printf '  %sFAIL%s %s\n' "$C_RED" "$C_OFF" "$*"; failures=$((failures + 1)); }
check() { local desc="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi; }

cid() { dc ps -q "$1" 2>/dev/null | head -n 1; }
inspect() { docker inspect -f "$2" "$(cid "$1")"; }

info "Container hardening"
for svc in postgres redis qdrant api worker frontend nginx; do
  id="$(cid "$svc")"
  [[ -n "$id" ]] || { fail "$svc is not running"; continue; }
  opts="$(inspect "$svc" '{{json .HostConfig.SecurityOpt}}')"
  [[ "$opts" == *no-new-privileges* ]] && pass "$svc: no-new-privileges" || fail "$svc: no-new-privileges missing"
  drop="$(inspect "$svc" '{{json .HostConfig.CapDrop}}')"
  [[ "$drop" == *ALL* ]] && pass "$svc: capabilities dropped" || fail "$svc: capabilities not dropped"
  [[ "$(inspect "$svc" '{{.HostConfig.Privileged}}')" == false ]] || fail "$svc: runs privileged"
done
for svc in api worker frontend nginx; do
  [[ "$(inspect "$svc" '{{.HostConfig.ReadonlyRootfs}}')" == true ]] && pass "$svc: read-only root filesystem" || fail "$svc: writable root filesystem"
done
for svc in api worker frontend nginx; do
  uid="$(dc exec -T "$svc" id -u 2>/dev/null | tr -d '\r')"
  [[ -n "$uid" && "$uid" != 0 ]] && pass "$svc: runs as uid $uid" || fail "$svc: runs as root"
done
for svc in postgres redis; do
  # The entrypoint starts as root only to fix volume ownership, then drops privileges.
  user="$(dc top "$svc" 2>/dev/null | awk 'NR > 1 && /postgres:|redis-server/ {print $1; exit}')"
  [[ -n "$user" && "$user" != root ]] && pass "$svc: server process runs as $user" || fail "$svc: server process runs as ${user:-unknown}"
done

info "Exposed ports"
for svc in postgres redis qdrant api worker frontend; do
  ports="$(inspect "$svc" '{{json .HostConfig.PortBindings}}')"
  [[ "$ports" == "{}" || "$ports" == "null" ]] && pass "$svc: no published ports" || fail "$svc publishes $ports"
done

info "Edge proxy"
http_port="$(env_get LOCALOPS_HTTP_PORT)"; http_port="${http_port:-8300}"
https_port="$(env_get LOCALOPS_HTTPS_PORT)"; https_port="${https_port:-8443}"
if [[ "$(env_get COMPOSE_FILE)" == *docker-compose.tls.yml* ]]; then
  base="https://$HOST:$https_port"
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://$HOST:$http_port/")"
  [[ "$code" == 301 ]] && pass "HTTP redirects to HTTPS" || fail "HTTP returned $code instead of a redirect"
  check "TLS 1.0/1.1 refused" bash -c "! curl -sk --tls-max 1.1 -o /dev/null '$base/'"
else
  base="http://$HOST:$http_port"
  fail "TLS is not enabled (docker-compose.tls.yml)"
fi
headers="$(curl -sk -D - -o /dev/null "$base/")"
for h in Strict-Transport-Security X-Content-Type-Options X-Frame-Options Content-Security-Policy Referrer-Policy; do
  if [[ "$h" == Strict-Transport-Security && "$base" == http://* ]]; then continue; fi
  grep -qi "^$h:" <<<"$headers" && pass "header $h" || fail "header $h missing"
done
grep -qi '^server: nginx/' <<<"$headers" && fail "nginx version disclosed" || pass "server version hidden"
metrics="$(curl -sk "$base/metrics")"
[[ "$metrics" != *python_gc_objects* && "$metrics" != *process_cpu_seconds* ]] && pass "/metrics not exposed" || fail "/metrics reachable through the proxy"
for path in /api/v1/docs /api/v1/openapi.json; do
  code="$(curl -sk -o /dev/null -w '%{http_code}' "$base$path")"
  [[ "$code" == 404 ]] && pass "$path disabled" || fail "$path returned $code"
done
limited=0
user="ratelimit-$RANDOM$RANDOM"
for _ in $(seq 1 25); do
  code="$(curl -sk -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
    -d "{\"tenant\":\"system\",\"username\":\"$user\",\"password\":\"wrong-password-1\"}" "$base/api/v1/auth/login")"
  [[ "$code" == 429 ]] && { limited=1; break; }
done
[[ "$limited" == 1 ]] && pass "login is rate limited" || fail "25 failed logins were not rate limited"

if [[ "$AIRGAP" == 1 ]]; then
  info "No internet egress"
  for svc in api worker; do
    if dc exec -T "$svc" python -c 'import socket; socket.create_connection(("1.1.1.1", 443), 4)' >/dev/null 2>&1; then
      fail "$svc can reach the internet (1.1.1.1:443)"
    else
      pass "$svc: no route to the internet"
    fi
    if dc exec -T "$svc" python -c 'import socket; socket.getaddrinfo("example.com", 443)' >/dev/null 2>&1; then
      fail "$svc can resolve public DNS names"
    else
      pass "$svc: public DNS not resolvable"
    fi
  done
fi

echo
if ((failures == 0)); then
  info "All security checks passed"
else
  printf '%s%d security check(s) failed%s\n' "$C_RED" "$failures" "$C_OFF"
fi
exit "$failures"
