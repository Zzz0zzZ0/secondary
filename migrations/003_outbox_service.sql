ALTER TABLE sales_automation.delivery_outbox
  ADD COLUMN IF NOT EXISTS lease_token_hash text;

CREATE INDEX IF NOT EXISTS delivery_outbox_worker_lease_idx
  ON sales_automation.delivery_outbox (worker_id, lease_expires_at)
  WHERE status = 'sending';
