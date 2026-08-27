#!/usr/bin/env bash
set -euo pipefail

PSQL_BIN="/opt/homebrew/opt/libpq/bin/psql"
OUTPUT_PATH="${1:-./twenty_db_business_map.txt}"
WORKSPACE_SCHEMA="${TWENTY_WORKSPACE_SCHEMA:-}"

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
require_env TWENTY_WORKSPACE_SCHEMA

if [[ ! "$WORKSPACE_SCHEMA" =~ ^workspace_[a-z0-9]+$ ]]; then
  printf 'TWENTY_WORKSPACE_SCHEMA has an invalid format: %s\n' "$WORKSPACE_SCHEMA" >&2
  exit 2
fi

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
  "$PSQL_BIN" "${PSQL_ARGS[@]}" >"$OUTPUT_PATH" <<SQL
BEGIN READ ONLY;
SET LOCAL statement_timeout = '20s';

\echo '=== BUSINESS TABLE COLUMNS ==='
SELECT
  table_name,
  ordinal_position,
  column_name,
  data_type,
  udt_name
FROM information_schema.columns
WHERE table_schema = '${WORKSPACE_SCHEMA}'
  AND table_name IN ('person', 'company', '_inquiry', '_productDemand', '_product')
ORDER BY table_name, ordinal_position;

\echo '=== TWENTY FIELD METADATA AND OPTIONS ==='
SELECT
  om."nameSingular" AS object_name,
  om."targetTableName" AS table_name,
  fm.name AS field_name,
  fm.label AS field_label,
  fm.type AS field_type,
  fm."isCustom" AS is_custom,
  fm.options
FROM core."fieldMetadata" fm
JOIN core."objectMetadata" om ON om.id = fm."objectMetadataId"
WHERE om."targetTableName" IN ('person', 'company', '_inquiry', '_productDemand', '_product')
  AND fm."isActive" = true
ORDER BY om."targetTableName", fm."isCustom", fm.name;

\echo '=== FOREIGN KEYS ==='
SELECT
  tc.table_name,
  kcu.column_name,
  ccu.table_name AS referenced_table,
  ccu.column_name AS referenced_column
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
 AND tc.constraint_schema = kcu.constraint_schema
JOIN information_schema.constraint_column_usage ccu
  ON ccu.constraint_name = tc.constraint_name
 AND ccu.constraint_schema = tc.constraint_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema = '${WORKSPACE_SCHEMA}'
  AND tc.table_name IN ('person', 'company', '_inquiry', '_productDemand', '_product')
ORDER BY tc.table_name, kcu.column_name;

\echo '=== RECORD COUNTS (NO CUSTOMER CONTENT) ==='
SELECT 'person' AS table_name, count(*) AS active_rows
FROM "${WORKSPACE_SCHEMA}".person WHERE "deletedAt" IS NULL
UNION ALL
SELECT 'company', count(*)
FROM "${WORKSPACE_SCHEMA}".company WHERE "deletedAt" IS NULL
UNION ALL
SELECT '_inquiry', count(*)
FROM "${WORKSPACE_SCHEMA}"._inquiry WHERE "deletedAt" IS NULL
UNION ALL
SELECT '_productDemand', count(*)
FROM "${WORKSPACE_SCHEMA}"."_productDemand" WHERE "deletedAt" IS NULL
UNION ALL
SELECT '_product', count(*)
FROM "${WORKSPACE_SCHEMA}"._product WHERE "deletedAt" IS NULL;

\echo '=== PERSON LEAD TYPE DISTRIBUTION (NO CUSTOMER CONTENT) ==='
SELECT "leadtype"::text AS lead_type, count(*) AS record_count
FROM "${WORKSPACE_SCHEMA}".person
WHERE "deletedAt" IS NULL
GROUP BY "leadtype"::text
ORDER BY record_count DESC, lead_type;

\echo '=== INQUIRY STATUS DISTRIBUTION (NO CUSTOMER CONTENT) ==='
SELECT status::text AS inquiry_status, count(*) AS record_count
FROM "${WORKSPACE_SCHEMA}"._inquiry
WHERE "deletedAt" IS NULL
GROUP BY status::text
ORDER BY record_count DESC, inquiry_status;

COMMIT;
SQL

printf 'Business mapping report written to %s\n' "$OUTPUT_PATH"
