#!/usr/bin/env bash
# Show LocalOps service state, health and version.
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
echo "LocalOps $(env_get LOCALOPS_VERSION) in $PROJECT_DIR"
dc ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Health}}\t{{.Status}}'
if dc ps api --format '{{.Health}}' 2>/dev/null | grep -q '^healthy$'; then
  echo
  echo "Readiness:"
  dc exec -T api python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/ready", timeout=5).read().decode())' ||
    warn "readiness check failed (a dependency is degraded)"
fi
