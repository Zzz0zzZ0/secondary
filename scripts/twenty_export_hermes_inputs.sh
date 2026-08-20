#!/usr/bin/env bash
set -euo pipefail

PSQL_BIN="/opt/homebrew/opt/libpq/bin/psql"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUT_PATH="${1:-$SCRIPT_DIR/../outputs/twenty_hermes_inputs.json}"
WORKSPACE_SCHEMA="${TWENTY_WORKSPACE_SCHEMA:-workspace_avv74ijhm70d2bh7o1toe4anv}"
EXPORT_LIMIT="${TWENTY_EXPORT_LIMIT:-500}"
AFTER_ID="${TWENTY_AFTER_ID:-}"
RECORD_ID="${TWENTY_RECORD_ID:-}"
BUSINESS_TIMEZONE="${TWENTY_BUSINESS_TIMEZONE:-Asia/Shanghai}"

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

if [[ ! "$WORKSPACE_SCHEMA" =~ ^workspace_[a-z0-9]+$ ]]; then
  printf 'TWENTY_WORKSPACE_SCHEMA has an invalid format: %s\n' "$WORKSPACE_SCHEMA" >&2
  exit 2
fi

if [[ ! "$EXPORT_LIMIT" =~ ^[1-9][0-9]{0,3}$ ]]; then
  printf 'TWENTY_EXPORT_LIMIT must be an integer between 1 and 9999\n' >&2
  exit 2
fi

if [[ ! -x "$PSQL_BIN" ]]; then
  printf 'psql not found at %s\n' "$PSQL_BIN" >&2
  exit 2
fi

PSQL_ARGS=(
  -X
  -q
  -t
  -A
  -v ON_ERROR_STOP=1
  -v export_limit="$EXPORT_LIMIT"
  -v after_id="$AFTER_ID"
  -v record_id="$RECORD_ID"
  -v business_timezone="$BUSINESS_TIMEZONE"
  -h "$TWENTY_DB_HOST"
  -p "$TWENTY_DB_PORT"
  -U "$TWENTY_DB_USER"
  -d "$TWENTY_DB_NAME"
)

if [[ -z "${TWENTY_DB_PASSWORD:-}" ]]; then
  PSQL_ARGS+=(-W)
fi

# Full secondary-lead scan. It is read-only and deliberately does not join
# inquiries or product demands because this workflow does not process inquiries.
PGPASSWORD="${TWENTY_DB_PASSWORD:-}" \
PGCONNECT_TIMEOUT="${TWENTY_DB_CONNECT_TIMEOUT:-5}" \
PGSSLMODE="${TWENTY_DB_SSLMODE:-prefer}" \
  "$PSQL_BIN" "${PSQL_ARGS[@]}" >"$OUTPUT_PATH" <<SQL
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';

WITH secondary_records AS (
  SELECT
    person.id::text AS record_id,
    jsonb_build_object(
      'schema_version', '1.0',
      'test_mode', true,
      'source_version', jsonb_build_object(
        'updated_at', GREATEST(
          person."createdAt",
          person."updatedAt",
          person."lastResponse",
          person."lastFollowUp",
          company."updatedAt",
          owner."updatedAt"
        ),
        'record_id', person.id,
        'activity_at', COALESCE(
          timezone(:'business_timezone', person."lastFollowUp"::timestamp),
          person."createdAt"
        ),
        'activity_at_source', CASE
          WHEN person."lastFollowUp" IS NOT NULL THEN 'lastFollowUp'
          ELSE 'createdAt'
        END
      ),
      'lead', jsonb_strip_nulls(jsonb_build_object(
        'id', person.id,
        'type', CASE
          WHEN person."lifeCycle"::text = 'NO_DEMAND'
            THEN 'no_current_demand'
          ELSE 'lead'
        END,
        'type_source', 'crm.person.lifeCycle',
        'life_cycle', person."lifeCycle"::text,
        'source', person.source::text,
        'raw_type', to_jsonb(person.leadtype),
        'internal_note', person.remark,
        'customer_message', NULL,
        'created_at', person."createdAt",
        'updated_at', person."updatedAt",
        'last_response_at', person."lastResponse",
        'last_follow_up_at', person."lastFollowUp",
        'reactivation_at', person."reactivationDate",
        'next_step', person."nextStep"
      )),
      'company', jsonb_strip_nulls(jsonb_build_object(
        'id', company.id,
        'name', company.name,
        'website', company."domainNamePrimaryLinkUrl",
        'linkedin_url', company."linkedinLinkPrimaryLinkUrl",
        'industry', company.industry::text,
        'research_text', company.background
      )),
      'contact', jsonb_strip_nulls(jsonb_build_object(
        'id', person.id,
        'name', NULLIF(
          trim(concat_ws(' ', person."nameFirstName", person."nameLastName")),
          ''
        ),
        'job_title', person."jobTitle",
        'email', contact_channel.email,
        'linkedin_url', contact_channel.linkedin_url
      )),
      'sales', jsonb_build_object(
        'name', COALESCE(
          NULLIF(
            trim(concat_ws(' ', owner."nameFirstName", owner."nameLastName")),
            ''
          ),
          NULLIF(person."createdByName", '')
        ),
        'signature', CASE
          WHEN COALESCE(
            NULLIF(
              trim(concat_ws(' ', owner."nameFirstName", owner."nameLastName")),
              ''
            ),
            NULLIF(person."createdByName", '')
          ) IS NOT NULL
          THEN concat(
            'Best regards,', chr(10),
            COALESCE(
              NULLIF(
                trim(concat_ws(' ', owner."nameFirstName", owner."nameLastName")),
                ''
              ),
              NULLIF(person."createdByName", '')
            ),
            chr(10),
            'Aceler International'
          )
        END,
        'source', CASE
          WHEN NULLIF(
            trim(concat_ws(' ', owner."nameFirstName", owner."nameLastName")),
            ''
          ) IS NOT NULL
            THEN 'company.accountOwner'
          WHEN NULLIF(person."createdByName", '') IS NOT NULL
            THEN 'person.createdByName'
          ELSE NULL
        END
      ),
      'output', jsonb_build_object(
        'type', CASE
          WHEN person.source::text = 'LINKEDIN' AND contact_channel.linkedin_url IS NOT NULL THEN 'linkedin'
          WHEN person.source::text IN ('EMAIL', 'YOU_XIANG', 'YI_LIU') AND contact_channel.email IS NOT NULL THEN 'email'
          WHEN person.source::text = 'LINKEDIN' AND contact_channel.email IS NOT NULL THEN 'email'
          WHEN person.source::text IN ('EMAIL', 'YOU_XIANG', 'YI_LIU') AND contact_channel.linkedin_url IS NOT NULL THEN 'linkedin'
          WHEN person.source::text = 'WHATSAPP' AND contact_channel.linkedin_url IS NOT NULL THEN 'linkedin'
          WHEN person.source::text = 'WHATSAPP' AND contact_channel.email IS NOT NULL THEN 'email'
          WHEN contact_channel.email IS NOT NULL THEN 'email'
          WHEN contact_channel.linkedin_url IS NOT NULL THEN 'linkedin'
          ELSE 'email'
        END,
        'default_language', 'English'
      ),
      'warnings', array_remove(ARRAY[
        CASE WHEN company.id IS NULL THEN 'COMPANY_NOT_FOUND' END,
        CASE WHEN NULLIF(btrim(company.background), '') IS NULL
          THEN 'COMPANY_CONTEXT_MISSING' END,
        CASE
          WHEN NULLIF(btrim(company.background), '') IS NOT NULL
            AND char_length(btrim(company.background)) < 20
          THEN 'COMPANY_CONTEXT_THIN'
        END,
        CASE
          WHEN contact_channel.email IS NULL
            AND contact_channel.linkedin_url IS NULL
          THEN 'CONTACT_CHANNEL_MISSING'
        END,
        CASE
          WHEN COALESCE(
            NULLIF(
              trim(concat_ws(' ', owner."nameFirstName", owner."nameLastName")),
              ''
            ),
            NULLIF(person."createdByName", '')
          ) IS NULL
          THEN 'SALES_OWNER_MISSING'
        END,
        CASE
          WHEN company.name ILIKE concat(
            trim(concat_ws(' ', person."nameFirstName", person."nameLastName")),
            '%'
          )
          THEN 'CONTACT_NAME_LOW_CONFIDENCE'
        END,
        CASE
          WHEN person."lastFollowUp" IS NULL
          THEN 'FOLLOW_UP_TIME_FALLBACK_CREATED_AT'
        END
      ]::text[], NULL)
    ) AS payload
  FROM "${WORKSPACE_SCHEMA}".person person
  CROSS JOIN LATERAL (
    SELECT
      COALESCE(
        NULLIF(person."emailsPrimaryEmail", ''),
        NULLIF(person."emailsAdditionalEmails"->>0, '')
      ) AS email,
      COALESCE(
        NULLIF(person."linkedinLinkPrimaryLinkUrl", ''),
        NULLIF(person."linkedinLinkSecondaryLinks"->0->>'url', '')
      ) AS linkedin_url
  ) contact_channel
  LEFT JOIN "${WORKSPACE_SCHEMA}".company company
    ON company.id = person."companyId"
   AND company."deletedAt" IS NULL
  LEFT JOIN "${WORKSPACE_SCHEMA}"."workspaceMember" owner
    ON owner.id = company."accountOwnerId"
   AND owner."deletedAt" IS NULL
  WHERE person."deletedAt" IS NULL
    AND person."lifeCycle"::text IN ('QUALIFIED', 'NO_DEMAND')
    AND (
      (:'record_id' <> '' AND person.id::text = :'record_id')
      OR
      (:'record_id' = '' AND person.id::text > :'after_id')
    )
  ORDER BY person.id::text
  LIMIT :export_limit
)
SELECT jsonb_pretty(
  COALESCE(jsonb_agg(payload ORDER BY record_id), '[]'::jsonb)
)
FROM secondary_records;

COMMIT;
SQL

printf 'Secondary-lead export written to %s\n' "$OUTPUT_PATH"
printf 'Batch limit: %s; ID cursor: %s\n' "$EXPORT_LIMIT" "${AFTER_ID:-<start>}"
