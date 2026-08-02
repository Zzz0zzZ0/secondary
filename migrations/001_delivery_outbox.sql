CREATE SCHEMA IF NOT EXISTS sales_automation;

CREATE TABLE IF NOT EXISTS sales_automation.message_version (
  id uuid PRIMARY KEY,
  run_id text NOT NULL,
  lead_id text NOT NULL,
  channel text NOT NULL CHECK (channel IN ('email', 'linkedin')),
  version integer NOT NULL DEFAULT 1 CHECK (version > 0),
  recipient_original text,
  crm_snapshot jsonb NOT NULL,
  original_output jsonb NOT NULL,
  edited_output jsonb,
  review_status text NOT NULL DEFAULT 'pending_review'
    CHECK (review_status IN ('pending_review', 'approved', 'rejected')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, lead_id, channel, version)
);

CREATE TABLE IF NOT EXISTS sales_automation.message_approval (
  id uuid PRIMARY KEY,
  message_version_id uuid NOT NULL REFERENCES sales_automation.message_version(id),
  decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
  approved_by text NOT NULL,
  approved_at timestamptz NOT NULL DEFAULT now(),
  note text
);

CREATE TABLE IF NOT EXISTS sales_automation.delivery_outbox (
  id uuid PRIMARY KEY,
  message_version_id uuid NOT NULL REFERENCES sales_automation.message_version(id),
  channel text NOT NULL CHECK (channel IN ('email', 'linkedin')),
  provider text NOT NULL,
  recipient_original text NOT NULL,
  sender_account_ref text,
  payload jsonb NOT NULL,
  payload_sha256 text NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'sending', 'sent', 'retry_wait', 'failed', 'unknown', 'cancelled')),
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 5,
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_at timestamptz,
  provider_message_id text,
  provider_thread_id text,
  last_error_code text,
  last_error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS delivery_outbox_claim_idx
  ON sales_automation.delivery_outbox (channel, status, available_at, created_at);

CREATE TABLE IF NOT EXISTS sales_automation.delivery_attempt (
  id uuid PRIMARY KEY,
  outbox_id uuid NOT NULL REFERENCES sales_automation.delivery_outbox(id),
  attempt_no integer NOT NULL,
  status text NOT NULL CHECK (status IN ('started', 'sent', 'retry_wait', 'failed', 'unknown')),
  provider_message_id text,
  provider_thread_id text,
  error_code text,
  error_message text,
  started_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (outbox_id, attempt_no)
);

