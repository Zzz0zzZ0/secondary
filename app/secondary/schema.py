import sqlite3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scheduler_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS secondary_lead_state (
  lead_id TEXT PRIMARY KEY,
  source_hash TEXT NOT NULL,
  source_updated_at TEXT NOT NULL,
  source_record_id TEXT NOT NULL,
  crm_snapshot_json TEXT NOT NULL,
  lead_type TEXT
    CHECK (
      lead_type IS NULL OR lead_type IN (
        'no_current_demand', 'unknown_demand',
        'referred', 'below_moq'
      )
    ),
  classification_confidence REAL,
  classification_reason TEXT,
  policy_version TEXT,
  status TEXT NOT NULL
    CHECK (status IN (
      'settling', 'classifying', 'scheduled', 'generating',
      'waiting_review', 'waiting_delivery', 'needs_review',
      'needs_contact', 'paused', 'converted', 'failed'
    )),
  classification_due_at TEXT,
  next_action_at TEXT,
  generation_retry_at TEXT,
  follow_up_count INTEGER NOT NULL DEFAULT 0,
  latest_analysis_job_id TEXT,
  latest_message_version_id TEXT,
  latest_run_id TEXT,
  last_classified_at TEXT,
  last_generated_at TEXT,
  last_seen_at TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS secondary_lead_classification_due_idx
  ON secondary_lead_state (status, classification_due_at);

CREATE INDEX IF NOT EXISTS secondary_lead_action_due_idx
  ON secondary_lead_state (status, next_action_at);

CREATE INDEX IF NOT EXISTS secondary_lead_type_idx
  ON secondary_lead_state (lead_type, status, next_action_at);

CREATE TABLE IF NOT EXISTS secondary_lead_classification (
  id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL
    REFERENCES secondary_lead_state(lead_id),
  source_hash TEXT NOT NULL,
  lead_type TEXT NOT NULL
    CHECK (lead_type IN (
      'no_current_demand', 'unknown_demand',
      'referred', 'below_moq'
    )),
  confidence REAL NOT NULL
    CHECK (confidence >= 0 AND confidence <= 1),
  reason TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  policy_version TEXT NOT NULL,
  result_json TEXT NOT NULL,
  usage_json TEXT,
  classified_at TEXT NOT NULL,
  UNIQUE (lead_id, source_hash)
);

CREATE TABLE IF NOT EXISTS secondary_lead_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_at TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS secondary_lead_event_lead_idx
  ON secondary_lead_event (lead_id, event_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS notes_follow_up_state (
  latest_note_id TEXT PRIMARY KEY,
  lead_id TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  source_created_at TEXT NOT NULL,
  crm_snapshot_json TEXT NOT NULL,
  structural_state TEXT NOT NULL
    CHECK (structural_state IN (
      'needs_analysis', 'waiting_customer', 'manual_review'
    )),
  status TEXT NOT NULL
    CHECK (status IN (
      'pending', 'processing', 'retry_wait', 'completed',
      'baseline', 'skipped', 'failed', 'superseded'
    )),
  decision TEXT,
  publication_type TEXT
    CHECK (
      publication_type IS NULL OR publication_type IN ('message', 'action')
    ),
  publication_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  analysis_json TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS notes_follow_up_due_idx
  ON notes_follow_up_state (status, next_attempt_at, source_created_at DESC);

CREATE INDEX IF NOT EXISTS notes_follow_up_lead_idx
  ON notes_follow_up_state (lead_id, source_created_at DESC);

CREATE TABLE IF NOT EXISTS scheduler_run (
  id TEXT PRIMARY KEY,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('running', 'completed', 'failed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  result_json TEXT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS scheduler_run_started_idx
  ON scheduler_run (started_at DESC);
"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    if "generation_retry_at" not in {row[1] for row in connection.execute("PRAGMA table_info(secondary_lead_state)")}:
        connection.execute("ALTER TABLE secondary_lead_state ADD COLUMN generation_retry_at TEXT")
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("notes_follow_up_state",),
    ).fetchone()
    table_sql = row[0] if row else ""
    if "'baseline'" in table_sql:
        return
    connection.execute(
        "ALTER TABLE notes_follow_up_state RENAME TO notes_follow_up_state_old"
    )
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        """
        INSERT INTO notes_follow_up_state
          (latest_note_id, lead_id, source_hash, source_created_at,
           crm_snapshot_json, structural_state, status, decision,
           publication_type, publication_id, attempts, next_attempt_at,
           analysis_json, last_error, created_at, updated_at)
        SELECT latest_note_id, lead_id, source_hash, source_created_at,
               crm_snapshot_json, structural_state, status, decision,
               publication_type, publication_id, attempts, next_attempt_at,
               analysis_json, last_error, created_at, updated_at
        FROM notes_follow_up_state_old
        """
    )
    connection.execute("DROP TABLE notes_follow_up_state_old")
    connection.executescript(SCHEMA_SQL)
