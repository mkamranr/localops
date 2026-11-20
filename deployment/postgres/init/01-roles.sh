#!/usr/bin/env bash
# Runs once, on first initialisation of the PostgreSQL data volume.
# Creates the LocalOps schema-owner role (migrations) and the runtime app role
# (no superuser, no BYPASSRLS, so row-level security always applies to it).
set -euo pipefail

# Docker secrets: LOCALOPS_*_PASSWORD_FILE takes precedence over the plain variable.
for var in LOCALOPS_OWNER_PASSWORD LOCALOPS_APP_PASSWORD; do
  file_var="${var}_FILE"
  if [[ -n "${!file_var:-}" ]]; then
    export "$var"="$(cat "${!file_var}")"
  fi
done

: "${LOCALOPS_OWNER_PASSWORD:?LOCALOPS_OWNER_PASSWORD is required}"
: "${LOCALOPS_APP_PASSWORD:?LOCALOPS_APP_PASSWORD is required}"
OWNER_USER="${DB_OWNER_USER:-localops_owner}"
APP_USER="${DB_APP_USER:-localops_app}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v owner_user="$OWNER_USER" -v owner_pw="$LOCALOPS_OWNER_PASSWORD" \
  -v app_user="$APP_USER" -v app_pw="$LOCALOPS_APP_PASSWORD" \
  -v db_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE :"owner_user" LOGIN PASSWORD :'owner_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE :"app_user"   LOGIN PASSWORD :'app_pw'   NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

ALTER DATABASE :"db_name" OWNER TO :"owner_user";
ALTER SCHEMA public OWNER TO :"owner_user";
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO :"owner_user", :"app_user";
GRANT USAGE ON SCHEMA public TO :"app_user";
SQL
echo "LocalOps database roles created: $OWNER_USER (owner), $APP_USER (runtime)"
