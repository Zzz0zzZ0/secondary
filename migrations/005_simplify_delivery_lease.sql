-- Stop legacy send-once and outbox-reaper processes before applying this migration.
DROP INDEX IF EXISTS sales_automation.delivery_outbox_worker_lease_idx;

ALTER TABLE sales_automation.delivery_outbox
  DROP COLUMN IF EXISTS worker_id,
  DROP COLUMN IF EXISTS heartbeat_at,
  DROP COLUMN IF EXISTS locked_at;
