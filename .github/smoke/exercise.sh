#!/usr/bin/env bash
# Air-gap smoke test: drive a running installation through its public API.
#   exercise.sh seed   BASE_URL ADMIN_PASSWORD   # local models, credentials, knowledge
#   exercise.sh verify BASE_URL ADMIN_PASSWORD   # the same data is back after a restore
set -Eeuo pipefail

MODE="$1" BASE="$2" PASSWORD="$3"
CURL=(curl -sSk --fail-with-body --max-time 60)

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

login() {
  "${CURL[@]}" -H 'Content-Type: application/json' \
    -d "$(jq -n --arg p "$PASSWORD" '{tenant: "system", username: "admin", password: $p}')" \
    "$BASE/api/v1/auth/login" | jq -r .access_token
}

step "Login"
TOKEN="$(login)"
[[ -n "$TOKEN" && "$TOKEN" != null ]] || fail "login failed"
AUTH=(-H "Authorization: Bearer $TOKEN")
api() { "${CURL[@]}" "${AUTH[@]}" "$@"; }

search() {
  api -H 'Content-Type: application/json' -d '{"query": "zebracorn failover procedure"}' \
    "$BASE/api/v1/knowledge/search"
}

wait_for_search() {
  local result
  for _ in $(seq 1 40); do
    result="$(search)"
    if [[ "$(jq -r .mode <<<"$result")" == hybrid ]] &&
      jq -e '.hits | map(.title) | index("Smoke runbook")' <<<"$result" >/dev/null; then
      echo "$result" | jq -c '{mode, top: .hits[0].title}'
      return 0
    fi
    sleep 3
  done
  echo "$result" >&2
  fail "knowledge search did not return the document through hybrid (vector) search"
}

if [[ "$MODE" == seed ]]; then
  step "Local model endpoints answer (chat + embeddings)"
  models="$(api "$BASE/api/v1/models")"
  [[ "$(jq length <<<"$models")" -ge 2 ]] || fail "expected seeded chat and embedding endpoints: $models"
  for id in $(jq -r '.[].id' <<<"$models"); do
    result="$(api -X POST "$BASE/api/v1/models/$id/test")"
    echo "$result" | jq -c '{ok, model_available, reply}'
    [[ "$(jq -r .ok <<<"$result")" == true ]] || fail "model endpoint $id test failed"
  done

  step "Store an integration with encrypted credentials"
  api -H 'Content-Type: application/json' -d '{
      "name": "Smoke Linux hosts", "integration_type": "linux_ssh",
      "configuration": {"username": "ops", "port": 22, "timeout_seconds": 5},
      "credentials": {"password": "smoke-secret-password"},
      "targets": [{"host": "192.0.2.10", "name": "smoke-01"}]}' \
    "$BASE/api/v1/integrations" | jq -c '{id, name}'

  step "Add and index a knowledge document"
  api -H 'Content-Type: application/json' -d '{
      "title": "Smoke runbook", "document_type": "runbook",
      "content": "# Zebracorn failover\n\nThe zebracorn failover procedure: drain the primary, promote the replica, verify replication lag."}' \
    "$BASE/api/v1/knowledge" | jq -c '{id, title}'
  wait_for_search
  echo "seed OK"

elif [[ "$MODE" == verify ]]; then
  step "Integration and its credentials survived the restore"
  api "$BASE/api/v1/integrations" | jq -e '(.items // .) | map(.name) | index("Smoke Linux hosts")' >/dev/null ||
    fail "integration missing after restore"
  step "Knowledge document and vectors survived the restore"
  api "$BASE/api/v1/knowledge" | jq -e '.items | map(.title) | index("Smoke runbook")' >/dev/null ||
    fail "knowledge document missing after restore"
  wait_for_search
  echo "verify OK"
else
  fail "unknown mode $MODE"
fi
