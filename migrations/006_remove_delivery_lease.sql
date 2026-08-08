DROP INDEX IF EXISTS sales_automation.delivery_outbox_expired_lease_idx;

ALTER TABLE sales_automation.delivery_outbox
  DROP COLUMN IF EXISTS lease_token_hash,
  DROP COLUMN IF EXISTS lease_expires_at;
