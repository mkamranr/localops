#!/usr/bin/env bash
# Start LocalOps (all services) and wait until they are healthy.
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
[[ -f "$PROJECT_DIR/.env" ]] || die "no .env in $PROJECT_DIR (run install.sh first)"
info "Starting LocalOps in $PROJECT_DIR"
if [[ "$(env_get COMPOSE_FILE)" == *docker-compose.bundle.yml* ]]; then
  dc up -d --no-build
else
  dc up -d
fi
wait_healthy "${1:-300}"
ok "all services healthy"
