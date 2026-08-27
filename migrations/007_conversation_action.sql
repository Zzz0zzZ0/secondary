CREATE TABLE IF NOT EXISTS sales_automation.conversation_action (
  id uuid PRIMARY KEY,
  run_id text NOT NULL,
  lead_id text NOT NULL,
  action_type text NOT NULL
    CHECK (action_type IN ('internal_task', 'manual_review')),
  crm_snapshot jsonb NOT NULL,
  analysis jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'resolved', 'dismissed')),
  handled_by text,
  resolution_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (run_id, lead_id)
);

CREATE INDEX IF NOT EXISTS conversation_action_queue_idx
  ON sales_automation.conversation_action (status, updated_at DESC);
