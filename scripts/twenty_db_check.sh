#!/usr/bin/env bash
set -euo pipefail

PSQL_BIN="/opt/homebrew/opt/libpq/bin/psql"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    printf 'Missing required environment variable: %s\n' "$name" >&2
    exit 2
  fi
}

require_env TWENTY_DB_HOST
require_env TWENTY_DB_PORT
require_env TWENTY_DB_NAME
require_env TWENTY_DB_USER

if [[ ! -x "$PSQL_BIN" ]]; then
  printf 'psql not found at %s\n' "$PSQL_BIN" >&2
  exit 2
fi

PSQL_ARGS=(
  -X
  -v ON_ERROR_STOP=1
  -h "$TWENTY_DB_HOST"
  -p "$TWENTY_DB_PORT"
  -U "$TWENTY_DB_USER"
  -d "$TWENTY_DB_NAME"
)

if [[ -z "${TWENTY_DB_PASSWORD:-}" ]]; then
  PSQL_ARGS+=(-W)
fi

PGPASSWORD="${TWENTY_DB_PASSWORD:-}" \
PGCONNECT_TIMEOUT="${TWENTY_DB_CONNECT_TIMEOUT:-5}" \
PGSSLMODE="${TWENTY_DB_SSLMODE:-prefer}" \
  "$PSQL_BIN" "${PSQL_ARGS[@]}" <<'SQL'
BEGIN READ ONLY;
SET LOCAL statement_timeout = '10s';
SELECT
  current_database() AS database_name,
  current_user AS database_user,
  inet_server_addr() AS server_address,
  inet_server_port() AS server_port,
  current_setting('transaction_read_only') AS transaction_read_only,
  version() AS postgres_version;
COMMIT;
SQL

