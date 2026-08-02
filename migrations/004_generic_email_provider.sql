UPDATE sales_automation.delivery_outbox
SET provider = 'email',
    updated_at = now()
WHERE channel = 'email'
  AND provider = 'gmail_api'
  AND status IN ('queued', 'sending', 'retry_wait', 'failed');
