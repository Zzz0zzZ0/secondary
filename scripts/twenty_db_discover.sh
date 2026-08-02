#!/usr/bin/env bash
set -euo pipefail

PSQL_BIN="/opt/homebrew/opt/libpq/bin/psql"
OUTPUT_PATH="${1:-./twenty_db_schema_report.txt}"

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
  "$PSQL_BIN" "${PSQL_ARGS[@]}" >"$OUTPUT_PATH" <<'SQL'
BEGIN READ ONLY;
SET LOCAL statement_timeout = '20s';

\echo '=== CONNECTION ==='
SELECT
  current_database() AS database_name,
  current_user AS database_user,
  current_setting('transaction_read_only') AS transaction_read_only;

\echo '=== NON-SYSTEM SCHEMAS ==='
SELECT
  n.nspname AS schema_name,
  count(c.oid) FILTER (WHERE c.relkind IN ('r', 'p', 'v', 'm')) AS relation_count
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname NOT LIKE 'pg_toast%'
  AND n.nspname NOT LIKE 'pg_temp%'
GROUP BY n.nspname
ORDER BY n.nspname;

\echo '=== CANDIDATE TABLES AND VIEWS ==='
SELECT
  table_schema,
  table_name,
  table_type
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND (
    table_name ~* '(person|people|company|compan|lead|clue|inquir|opportun|workspace|object|field|metadata)'
    OR table_schema ~* '(workspace|tenant)'
  )
ORDER BY table_schema, table_name
LIMIT 500;

\echo '=== CANDIDATE COLUMNS ==='
SELECT
  table_schema,
  table_name,
  ordinal_position,
  column_name,
  data_type
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND (
    table_name ~* '(person|people|company|compan|lead|clue|inquir|opportun|workspace|object|field|metadata)'
    OR column_name ~* '(person|company|lead|clue|inquir|background|remark|linkedin|email|owner)'
  )
ORDER BY table_schema, table_name, ordinal_position
LIMIT 2000;

COMMIT;
SQL

printf 'Schema report written to %s\n' "$OUTPUT_PATH"
