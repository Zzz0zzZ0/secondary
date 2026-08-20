import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.secondary_scheduler import SALES_FOLLOW_UP_TIME_UNVERIFIED
from app.secondary_scheduler import SecondaryLeadScheduler
from app.secondary_scheduler import _sales_follow_up_due_at


class SecondarySchedulerReviewTest(unittest.TestCase):
    def test_sales_follow_up_uses_reliable_last_follow_up_time(self):
        record = {
            "source_version": {
                "activity_at": "2026-08-08T00:00:00+00:00",
                "activity_at_source": "lastFollowUp",
            }
        }
        self.assertEqual(
            "2026-08-13T00:00:00+00:00",
            _sales_follow_up_due_at(record, 5),
        )
        record["source_version"]["activity_at_source"] = "createdAt"
        self.assertIsNone(_sales_follow_up_due_at(record, 5))

    def test_sales_reply_without_reliable_time_requires_review(self):
        now = datetime(2026, 8, 8, tzinfo=timezone.utc)
        record = {
            "lead": {"id": "lead-1", "internal_note": "销售已回复客户。"},
            "source_version": {
                "activity_at": "2026-08-01T00:00:00+00:00",
                "activity_at_source": "createdAt",
            },
        }
        candidate = {
            "lead_id": "lead-1",
            "lead_type": "unknown_demand",
            "confidence": 0.9,
            "reason": "CRM备注明确说明销售已回复。",
            "evidence": ["销售已回复客户。"],
            "sales_follow_up_context": {
                "status": "sales_replied",
                "evidence_quote": "销售已回复客户。",
            },
            "contact_permission": {
                "status": "allowed",
                "evidence_quote": None,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            scheduler.classification_runner.run = (
                lambda _record, _run_dir: (candidate, None)
            )
            with scheduler._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at, source_record_id,
                       crm_snapshot_json, status, follow_up_count,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'classifying', 0, ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        "source-1",
                        now.isoformat(),
                        "lead-1",
                        json.dumps(record),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM secondary_lead_state WHERE lead_id = 'lead-1'"
                ).fetchone()

            result = scheduler._classify_one(row)

            self.assertEqual("needs_review", result["status"])
            self.assertIsNone(result["next_action_at"])
            with scheduler._connect() as connection:
                stored = connection.execute(
                    """
                    SELECT status, next_action_at, last_error
                    FROM secondary_lead_state
                    WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
            self.assertEqual("needs_review", stored["status"])
            self.assertIsNone(stored["next_action_at"])
            self.assertEqual(
                SALES_FOLLOW_UP_TIME_UNVERIFIED,
                stored["last_error"],
            )

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

    def test_sales_reply_allows_only_one_automated_follow_up(self):
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
                       crm_snapshot_json, lead_type, status, follow_up_count,
                       latest_message_version_id, last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', ?, 'waiting_delivery', 0, ?, ?, ?, ?)
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
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO secondary_lead_classification
                      (id, lead_id, source_hash, lead_type, confidence, reason,
                       evidence_json, policy_version, result_json, classified_at)
                    VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)
                    """,
                    (
                        "classification-1",
                        "lead-1",
                        "source-1",
                        "unknown_demand",
                        0.9,
                        "销售已经回复客户。",
                        "secondary-lead-v7",
                        '{"sales_follow_up_context":{"status":"sales_replied","evidence_quote":"已回复"}}',
                        now.isoformat(),
                    ),
                )

            self.assertTrue(scheduler.handle_outbox_signal("message-1", "sent"))

            with scheduler._connect() as connection:
                row = connection.execute(
                    """
                    SELECT status, next_action_at, follow_up_count
                    FROM secondary_lead_state
                    WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
            self.assertEqual("paused", row["status"])
            self.assertIsNone(row["next_action_at"])
            self.assertEqual(1, row["follow_up_count"])


if __name__ == "__main__":
    unittest.main()
