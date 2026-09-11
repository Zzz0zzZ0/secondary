import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.secondary_scheduler import SecondaryLeadScheduler
from app.secondary_scheduler import _crm_next_follow_up_at
from app.secondary_scheduler import _prefer_valid_contact_channel
from app.secondary_scheduler import _sales_follow_up_due_at


class SecondarySchedulerReviewTest(unittest.TestCase):
    def test_invalid_linkedin_route_falls_back_to_valid_email(self):
        record = {
            "contact": {
                "email": "buyer@example.com",
                "linkedin_url": "linkedin.com/in/not-an-absolute-url",
            },
            "output": {"type": "linkedin", "default_language": "English"},
        }

        _prefer_valid_contact_channel(record)

        self.assertEqual("email", record["output"]["type"])

    def test_full_scan_converts_and_retires_leads_missing_from_crm_scope(self):
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            current = {
                "lead": {"id": "current-lead", "created_at": now.isoformat()},
                "source_version": {
                    "record_id": "current-lead",
                    "updated_at": now.isoformat(),
                    "activity_at": now.isoformat(),
                },
            }
            scheduler._export_batch = Mock(return_value=[current])
            with scheduler._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at, source_record_id,
                       crm_snapshot_json, status, latest_message_version_id,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'waiting_review', ?, ?, ?, ?)
                    """,
                    (
                        "missing-lead",
                        "source-1",
                        now.isoformat(),
                        "missing-lead",
                        "message-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'needs_analysis', 'pending', ?, ?)
                    """,
                    (
                        "note-1",
                        "missing-lead",
                        "notes-source-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            with (
                patch(
                    "app.secondary_scheduler.reject_pending_messages_for_lead"
                ) as reject_messages,
                patch(
                    "app.secondary_scheduler.dismiss_pending_actions_for_lead"
                ) as dismiss_actions,
            ):
                counts = scheduler.scan()

            self.assertEqual(1, counts["converted"])
            reject_messages.assert_called_once_with(
                "missing-lead",
                "secondary-scheduler",
                "Lead left the secondary CRM scope; pending review retired.",
            )
            dismiss_actions.assert_called_once_with(
                "missing-lead",
                "secondary-scheduler",
                "Lead left the secondary CRM scope; pending action dismissed.",
            )
            with scheduler._connect() as connection:
                row = connection.execute(
                    """
                    SELECT status, next_action_at, latest_message_version_id
                    FROM secondary_lead_state
                    WHERE lead_id = 'missing-lead'
                    """
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT event_type, details_json FROM secondary_lead_event
                    WHERE lead_id = 'missing-lead'
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
                notes_status = connection.execute(
                    """
                    SELECT status FROM notes_follow_up_state
                    WHERE latest_note_id = 'note-1'
                    """
                ).fetchone()["status"]
            self.assertEqual(
                ("converted", None, None),
                tuple(row),
            )
            self.assertEqual("left_secondary_lead_scope", event["event_type"])
            self.assertEqual(
                "full_scan",
                json.loads(event["details_json"])["source"],
            )
            self.assertEqual("superseded", notes_status)

    def test_incomplete_full_scan_does_not_retire_missing_leads(self):
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={"HERMES_SECONDARY_SCAN_BATCH_SIZE": "1"},
                now=lambda: now,
            )
            current = {
                "lead": {"id": "current-lead", "created_at": now.isoformat()},
                "source_version": {
                    "record_id": "current-lead",
                    "updated_at": now.isoformat(),
                    "activity_at": now.isoformat(),
                },
            }
            scheduler._export_batch = Mock(
                side_effect=[[current], RuntimeError("CRM export interrupted")]
            )
            with scheduler._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at, source_record_id,
                       crm_snapshot_json, status, latest_message_version_id,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'waiting_review', ?, ?, ?, ?)
                    """,
                    (
                        "missing-lead",
                        "source-1",
                        now.isoformat(),
                        "missing-lead",
                        "message-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            with (
                patch(
                    "app.secondary_scheduler.reject_pending_messages_for_lead"
                ) as reject_messages,
                patch(
                    "app.secondary_scheduler.dismiss_pending_actions_for_lead"
                ) as dismiss_actions,
            ):
                with self.assertRaisesRegex(RuntimeError, "CRM export interrupted"):
                    scheduler.scan()

            reject_messages.assert_not_called()
            dismiss_actions.assert_not_called()
            with scheduler._connect() as connection:
                status = connection.execute(
                    """
                    SELECT status FROM secondary_lead_state
                    WHERE lead_id = 'missing-lead'
                    """
                ).fetchone()["status"]
            self.assertEqual("waiting_review", status)

    def test_lead_detail_includes_read_notes_context(self):
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            record = {
                "lead_id": "lead-1",
                "notes": [
                    {
                        "note_id": "note-1",
                        "direction": "SHOU",
                        "email_at": now.isoformat(),
                        "subject": "Re: Proposal",
                        "body": "Thank you. We will contact you if needed.",
                    }
                ],
            }
            with scheduler._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at, source_record_id,
                       crm_snapshot_json, status, last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'settling', ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        "source-1",
                        now.isoformat(),
                        "lead-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'needs_analysis', 'pending', ?, ?)
                    """,
                    (
                        "note-1",
                        "lead-1",
                        "notes-source-1",
                        now.isoformat(),
                        json.dumps(record),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            detail = scheduler.lead_detail("lead-1")

            self.assertEqual(1, detail["notes_follow_up"]["note_count"])
            self.assertEqual("pending", detail["notes_follow_up"]["status"])
            self.assertEqual(
                "note-1",
                detail["notes_follow_up"]["latest_note_id"],
            )
            self.assertEqual(record["notes"], detail["notes_follow_up"]["notes"])

    def test_run_cycle_invokes_notes_route_only_when_due(self):
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={"HERMES_NOTES_SCAN_MINUTES": "10"},
                now=lambda: now,
            )
            scheduler.classify_due = Mock(return_value=[])
            scheduler.dispatch_due = Mock(return_value=[])
            scheduler.notes_processor.run = Mock(
                return_value={"status": "ok", "processed": 1}
            )
            with scheduler._connect() as connection:
                scheduler._set_state(
                    connection,
                    "next_full_scan_at",
                    "2026-08-22T00:00:00+00:00",
                )

            first = scheduler.run_cycle(trigger="test")
            second = scheduler.run_cycle(trigger="test")

            self.assertEqual({"status": "ok", "processed": 1}, first["notes"])
            self.assertIsNone(second["notes"])
            scheduler.notes_processor.run.assert_called_once_with()

    def test_notes_scan_runs_before_main_classification_and_dispatch(self):
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            calls = []
            scheduler.classify_due = Mock(
                side_effect=lambda _limit: calls.append("classify") or []
            )
            scheduler.notes_processor.run = Mock(
                side_effect=lambda: calls.append("notes") or {"status": "ok"}
            )
            scheduler.dispatch_due = Mock(
                side_effect=lambda: calls.append("dispatch") or []
            )
            with scheduler._connect() as connection:
                scheduler._set_state(
                    connection,
                    "next_full_scan_at",
                    "2026-08-22T00:00:00+00:00",
                )

            scheduler.run_cycle(trigger="test")

            self.assertEqual(["notes", "dispatch", "classify"], calls)

    def test_main_classification_skips_lead_owned_by_current_notes(self):
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
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
                       crm_snapshot_json, status, classification_due_at,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'settling', ?, ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        "source-1",
                        now.isoformat(),
                        "lead-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'needs_analysis', 'pending', ?, ?)
                    """,
                    (
                        "note-1",
                        "lead-1",
                        "notes-source-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            self.assertIsNone(scheduler._claim_due_classification())

    def test_main_dispatch_skips_lead_owned_by_current_notes(self):
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
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
                       crm_snapshot_json, lead_type, status, next_action_at,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'unknown_demand', 'scheduled', ?, ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        "source-1",
                        now.isoformat(),
                        "lead-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'needs_analysis', 'pending', ?, ?)
                    """,
                    (
                        "note-1",
                        "lead-1",
                        "notes-source-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            self.assertIsNone(scheduler._claim_due_action())

    def test_waiting_customer_note_allows_scheduled_maintenance(self):
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
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
                       crm_snapshot_json, lead_type, status, next_action_at,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'unknown_demand', 'scheduled', ?, ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        "source-1",
                        now.isoformat(),
                        "lead-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'waiting_customer', 'skipped', ?, ?)
                    """,
                    (
                        "note-1",
                        "lead-1",
                        "notes-source-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            claimed = scheduler._claim_due_action()

            self.assertIsNotNone(claimed)
            self.assertEqual("lead-1", claimed["lead_id"])

    def test_notes_owned_lead_remains_in_its_business_queue(self):
        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
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
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'unknown_demand', 'needs_review', ?, ?, ?)
                    """,
                    (
                        "lead-1",
                        "source-1",
                        now.isoformat(),
                        "lead-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'needs_analysis', 'pending', ?, ?)
                    """,
                    (
                        "note-1",
                        "lead-1",
                        "notes-source-1",
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

            counts = scheduler.status()["counts"]
            manual_queue = scheduler.queue(10, ["needs_review"])

            self.assertNotIn("notes_follow_up", counts)
            self.assertEqual(1, counts["needs_review"])
            self.assertEqual("lead-1", manual_queue[0]["lead_id"])
            self.assertEqual(1, manual_queue[0]["notes_owned"])
            with self.assertRaises(RuntimeError):
                scheduler.queue(10, ["notes_follow_up"])

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

    def test_crm_next_follow_up_is_an_explicit_optional_override(self):
        record = {
            "lead": {"next_follow_up_at": "2026-09-01T08:00:00+00:00"},
        }
        self.assertEqual(
            "2026-09-01T08:00:00+00:00",
            _crm_next_follow_up_at(record),
        )
        self.assertIsNone(_crm_next_follow_up_at({"lead": {}}))

    def test_follow_up_frequency_slows_to_long_term_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
            )
            expected_ranges = {
                0: (3, 7),
                1: (30, 45),
                2: (60, 90),
                3: (90, 120),
                8: (90, 120),
            }
            for sequence, (low, high) in expected_ranges.items():
                days = scheduler._interval_days(
                    "unknown_demand", "lead-1", sequence
                )
                self.assertGreaterEqual(days, low)
                self.assertLessEqual(days, high)

    def test_sales_reply_without_reliable_time_schedules_from_classification(self):
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

            expected_next_action = (
                now
                + timedelta(
                    days=scheduler._interval_days("unknown_demand", "lead-1", 0)
                )
            ).isoformat()
            self.assertEqual("scheduled", result["status"])
            self.assertEqual(expected_next_action, result["next_action_at"])
            with scheduler._connect() as connection:
                stored = connection.execute(
                    """
                    SELECT status, next_action_at, last_error
                    FROM secondary_lead_state
                    WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
            self.assertEqual("scheduled", stored["status"])
            self.assertEqual(expected_next_action, stored["next_action_at"])
            self.assertIsNone(stored["last_error"])

    def test_waiting_customer_uses_last_sales_time_for_maintenance(self):
        now = datetime(2026, 8, 8, tzinfo=timezone.utc)
        record = {
            "lead": {"id": "lead-1"},
            "source_version": {
                "activity_at": "2026-08-01T00:00:00+00:00",
                "activity_at_source": "lastFollowUp",
            },
        }
        candidate = {
            "lead_id": "lead-1",
            "lead_type": "unknown_demand",
            "confidence": 0.9,
            "reason": "需求仍未知。",
            "evidence": [],
            "sales_follow_up_context": {
                "status": "none",
                "evidence_quote": None,
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
                        "lead-1", "source-1", now.isoformat(), "lead-1",
                        json.dumps(record), now.isoformat(), now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, '{}', 'waiting_customer', 'skipped', ?, ?)
                    """,
                    (
                        "note-1", "lead-1", "notes-source-1",
                        now.isoformat(), now.isoformat(), now.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM secondary_lead_state WHERE lead_id = 'lead-1'"
                ).fetchone()

            result = scheduler._classify_one(row)
            days = scheduler._interval_days("unknown_demand", "lead-1", 0)

            self.assertEqual("scheduled", result["status"])
            self.assertEqual(
                (datetime(2026, 8, 1, tzinfo=timezone.utc) + timedelta(days=days)).isoformat(),
                result["next_action_at"],
            )
            with scheduler._connect() as connection:
                scheduled = connection.execute(
                    "SELECT * FROM secondary_lead_state WHERE lead_id = 'lead-1'"
                ).fetchone()
            message_input = scheduler._message_input(scheduled)
            self.assertEqual("conversation_follow_up", message_input["message_route"])
            self.assertEqual(
                "crm.note.direction",
                message_input["sales_follow_up_context"]["source"],
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

    def test_superseded_message_requeues_lead_without_human_attention(self):
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

            self.assertTrue(
                scheduler.handle_outbox_signal("message-1", "superseded")
            )

            with scheduler._connect() as connection:
                row = connection.execute(
                    """
                    SELECT status, classification_due_at,
                           latest_message_version_id, last_generated_at
                    FROM secondary_lead_state
                    WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT event_type
                    FROM secondary_lead_event
                    WHERE lead_id = 'lead-1'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            self.assertEqual("settling", row["status"])
            self.assertEqual(now.isoformat(), row["classification_due_at"])
            self.assertIsNone(row["latest_message_version_id"])
            self.assertIsNone(row["last_generated_at"])
            self.assertEqual("outbox_superseded", event["event_type"])

    def test_sales_reply_enters_maintenance_after_automated_follow_up(self):
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
            days = scheduler._interval_days("unknown_demand", "lead-1", 1)
            self.assertEqual("scheduled", row["status"])
            self.assertEqual(
                (now + timedelta(days=days)).isoformat(),
                row["next_action_at"],
            )
            self.assertEqual(1, row["follow_up_count"])


if __name__ == "__main__":
    unittest.main()
