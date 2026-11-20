#!/usr/bin/env bash
# Stop LocalOps. Data volumes are kept; nothing is deleted.
# shellcheck source=scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_cmd docker
info "Stopping LocalOps in $PROJECT_DIR"
dc stop
ok "stopped (data is kept; start again with scripts/start.sh)"
