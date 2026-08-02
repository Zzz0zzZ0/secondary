ALTER TABLE sales_automation.delivery_outbox
  ADD COLUMN IF NOT EXISTS worker_id text,
  ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS heartbeat_at timestamptz;

CREATE INDEX IF NOT EXISTS delivery_outbox_expired_lease_idx
  ON sales_automation.delivery_outbox (lease_expires_at)
  WHERE status = 'sending' AND lease_expires_at IS NOT NULL;
