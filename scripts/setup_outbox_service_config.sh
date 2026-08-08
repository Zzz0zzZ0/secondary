#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_PATH="${OUTBOX_SERVICE_CONFIG_PATH:-$PROJECT_DIR/config/outbox-service.env}"

if [[ -e "$OUTPUT_PATH" ]]; then
  printf 'Outbox service configuration already exists: %s\n' "$OUTPUT_PATH" >&2
  exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  printf 'openssl is required to generate service tokens.\n' >&2
  exit 2
fi

EMAIL_TOKEN="$(openssl rand -hex 32)"
LINKEDIN_TOKEN="$(openssl rand -hex 32)"
PRODUCER_TOKEN="$(openssl rand -hex 32)"

umask 077
{
  printf 'OUTBOX_CONSUMER_TOKENS_JSON='\''{"%s":["email:email"],"%s":["linkedin:linkedin"]}'\''\n' \
    "$EMAIL_TOKEN" "$LINKEDIN_TOKEN"
  printf 'OUTBOX_PRODUCER_TOKEN=%s\n' "$PRODUCER_TOKEN"
  printf 'OUTBOX_CONTACT_COOLDOWN_HOURS=24\n'
  printf 'OUTBOX_API_HOST=0.0.0.0\n'
  printf 'OUTBOX_API_PORT=8010\n'
} >"$OUTPUT_PATH"
chmod 600 "$OUTPUT_PATH"
printf 'Outbox service tokens saved to %s with mode 600.\n' "$OUTPUT_PATH"
