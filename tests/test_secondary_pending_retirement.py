import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.secondary_scheduler import SecondaryLeadScheduler


class SecondaryPendingRetirementTest(unittest.TestCase):
    def test_source_change_rejects_pending_drafts_before_resettling(self):
        now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        record = {
            "lead": {"id": "lead-1"},
            "source_version": {
                "record_id": "lead-1",
                "updated_at": now.isoformat(),
                "activity_at": now.isoformat(),
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            with scheduler._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at, source_record_id,
                       crm_snapshot_json, status, latest_analysis_job_id,
                       latest_message_version_id,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, 'old-source', ?, ?, '{}', 'waiting_review', ?, ?, ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        now.isoformat(),
                        "lead-1",
                        "job-1",
                        "message-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO analysis_job
                      (id, lead_id, source_hash, source_updated_at,
                       source_record_id, status, input_path, run_root,
                       created_at, updated_at)
                    VALUES (?, ?, 'old-message-source', ?, ?, 'retry_wait',
                            ?, ?, ?, ?)
                    """,
                    (
                        "job-1",
                        "lead-1",
                        now.isoformat(),
                        "lead-1",
                        str(Path(directory) / "input.json"),
                        str(Path(directory) / "runs"),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            with patch(
                "app.secondary_scheduler.reject_pending_messages_for_lead"
            ) as reject_pending:
                result = scheduler._upsert_discovered(record)

            self.assertEqual("updated", result)
            reject_pending.assert_called_once_with(
                "lead-1",
                "secondary-scheduler",
                "CRM source changed before review; superseded draft rejected.",
            )
            with scheduler._connect() as connection:
                state = connection.execute(
                    """
                    SELECT status, latest_message_version_id
                    FROM secondary_lead_state WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
                job = connection.execute(
                    "SELECT status, last_error FROM analysis_job WHERE id = 'job-1'"
                ).fetchone()
            self.assertEqual("settling", state["status"])
            self.assertIsNone(state["latest_message_version_id"])
            self.assertEqual("failed", job["status"])
            self.assertEqual("Superseded by CRM source change", job["last_error"])


if __name__ == "__main__":
    unittest.main()
