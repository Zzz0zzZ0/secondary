import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.secondary_scheduler import SecondaryLeadScheduler


class SecondarySchedulerReviewTest(unittest.TestCase):
    def test_retry_attention_requeues_contact_and_failed_leads(self):
        now = datetime(2026, 8, 8, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            with scheduler._connect() as connection:
                for lead_id, status in (("contact-lead", "needs_contact"), ("failed-lead", "failed")):
                    connection.execute(
                        """
                        INSERT INTO secondary_lead_state
                          (lead_id, source_hash, source_updated_at, source_record_id,
                           crm_snapshot_json, lead_type, status, last_error,
                           last_seen_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            lead_id,
                            f"source-{lead_id}",
                            now.isoformat(),
                            lead_id,
                            "unknown_demand",
                            status,
                            "old error",
                            now.isoformat(),
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )

            for lead_id in ("contact-lead", "failed-lead"):
                result = scheduler.retry_attention(lead_id, "reviewer", "manual retry")
                self.assertEqual("scheduled", result["status"])
                self.assertEqual(now.isoformat(), result["next_action_at"])

            with scheduler._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT lead_id, status, next_action_at, last_error
                    FROM secondary_lead_state
                    ORDER BY lead_id
                    """
                ).fetchall()
                events = connection.execute(
                    """
                    SELECT lead_id, event_type
                    FROM secondary_lead_event
                    ORDER BY id
                    """
                ).fetchall()
            self.assertEqual(
                [("contact-lead", "scheduled", now.isoformat(), None),
                 ("failed-lead", "scheduled", now.isoformat(), None)],
                [tuple(row) for row in rows],
            )
            self.assertEqual(
                [("contact-lead", "manual_retry_queued"),
                 ("failed-lead", "manual_retry_queued")],
                [tuple(row) for row in events],
            )

    def test_rejected_message_returns_lead_to_classification_review(self):
        now = datetime(2026, 8, 8, tzinfo=timezone.utc)
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
                       crm_snapshot_json, lead_type, status,
                       latest_message_version_id, last_classified_at,
                       last_generated_at, last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', ?, 'waiting_review', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        "source-1",
                        now.isoformat(),
                        "lead-1",
                        "unknown_demand",
                        "message-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            self.assertTrue(scheduler.handle_outbox_signal("message-1", "rejected"))

            with scheduler._connect() as connection:
                row = connection.execute(
                    """
                    SELECT status, latest_message_version_id, last_generated_at
                    FROM secondary_lead_state
                    WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
            self.assertEqual("needs_review", row["status"])
            self.assertIsNone(row["latest_message_version_id"])
            self.assertIsNone(row["last_generated_at"])


if __name__ == "__main__":
    unittest.main()
