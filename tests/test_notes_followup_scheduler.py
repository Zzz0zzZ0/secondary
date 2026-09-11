import copy
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from app.secondary.notes_followup import (
    NotesFollowUpProcessor,
    find_existing_review,
    retire_stale_reviews,
)
from app.secondary.schema import initialize_schema
from notes_trial import notes_trial as notes_core


class NotesFollowUpSchedulerTest(unittest.TestCase):
    """Contract tests for the Notes route owned by the formal Poller."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        initialize_schema(self.connection)
        self.records = {
            "no-action-lead": self._record(
                "no-action-lead", "note-no-action", "SHOU"
            ),
            "waiting-lead": self._record(
                "waiting-lead", "note-waiting", "FA"
            ),
            "reply-lead": self._record("reply-lead", "note-reply", "SHOU"),
        }
        self.analyze = Mock(side_effect=self._analyze)
        self.publish = Mock(side_effect=self._publish)
        self.publication_ids = iter(("message-1", "message-2"))

    @staticmethod
    def _record(lead_id, note_id, direction):
        return {
            "lead_id": lead_id,
            "contact_name": "Mr. Buyer",
            "contact_email": f"{lead_id}@example.com",
            "company_name": "Example Materials",
            "life_cycle": "QUALIFIED",
            "notes": [
                {
                    "note_id": note_id,
                    "direction": direction,
                    "email_at": "2026-08-20T10:00:00+00:00",
                    "created_at": "2026-08-20T10:01:00+00:00",
                    "subject": "Re: Specs",
                    "sender_name": "Buddy",
                    "sender_account": "buddy@okgmineral.com",
                    "body": "## 最新邮件原文\nPlease send the current specification.",
                }
            ],
        }

    def _read_records(self, limit, record_id=None):
        records = list(self.records.values())
        if record_id is not None:
            records = [record for record in records if record["lead_id"] == record_id]
        return copy.deepcopy(records[:limit])

    def _analyze(self, record, state):
        latest = state["latest"]
        if record["lead_id"] == "no-action-lead":
            return {
                "semantic": {
                    "contact_permission": "allowed",
                    "decision": "no_action",
                    "reason": "客户无需继续跟进。",
                    "evidence_quote": "Please send the current specification.",
                },
                "draft": None,
            }
        return {
            "semantic": {
                "contact_permission": "allowed",
                "decision": "reply",
                "reason": "客户请求规格资料，需要人工审阅回复。",
                "evidence_quote": "Please send the current specification.",
            },
            "draft": {
                "lead_id": record["lead_id"],
                "latest_note_id": latest["note_id"],
                "language": "English",
                "content": {
                    "subject": "Re: Specs",
                    "subject_zh": "回复：规格",
                    "body": "Hello Mr. Buyer,\n\nBuddy",
                    "body_zh": "您好，Buyer先生：\n\nBuddy",
                },
            },
        }

    def _publish(self, record, state, analysis):
        return {
            "lead_id": record["lead_id"],
            "status": "pending_review",
            "message_version_id": next(self.publication_ids),
        }

    def _processor(self, find_existing=None, sync_message_review=None):
        return NotesFollowUpProcessor(
            connect=lambda: self.connection,
            environment={"HERMES_NOTES_PROCESS_EXISTING": "true"},
            now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
            read_records=self._read_records,
            analyze=self.analyze,
            publish=self.publish,
            find_existing=find_existing or (
                lambda lead_id, note_id, email_at: None
            ),
            retire_stale=lambda lead_id, note_id, email_at: [],
            sync_message_review=sync_message_review or (
                lambda lead_id, message_id: None
            ),
        )

    def test_sales_display_name_change_does_not_repeat_semantic_analysis(self):
        self.records = {"no-action-lead": self.records["no-action-lead"]}
        self.records["no-action-lead"]["sales_name"] = "Cathy Wang"
        processor = self._processor()

        processor.run(limit=1)
        self.records["no-action-lead"]["sales_name"] = "Yuki Zhang"
        processor.run(limit=1)

        self.assertEqual(1, self.analyze.call_count)

    def test_draft_policy_upgrade_does_not_reanalyze_every_completed_note(self):
        self.records = {"no-action-lead": self.records["no-action-lead"]}
        processor = self._processor()

        with unittest.mock.patch.object(
            notes_core, "NOTES_DRAFT_POLICY_VERSION", "notes-draft-v1"
        ):
            processor.run(limit=1)
        processor.run(limit=1)

        self.assertEqual(1, self.analyze.call_count)

    def test_every_current_note_retires_only_stale_review_drafts(self):
        self.records = {"waiting-lead": self.records["waiting-lead"]}
        retire_stale = Mock(return_value=["stale-message"])
        processor = NotesFollowUpProcessor(
            connect=lambda: self.connection,
            environment={"HERMES_NOTES_PROCESS_EXISTING": "true"},
            now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
            read_records=self._read_records,
            analyze=self.analyze,
            publish=self.publish,
            find_existing=lambda lead_id, note_id, email_at: None,
            retire_stale=retire_stale,
        )

        result = processor.run(limit=1)

        retire_stale.assert_called_once_with(
            "waiting-lead",
            "note-waiting",
            "2026-08-20T10:00:00+00:00",
        )
        self.assertEqual(1, result["retired_stale_reviews"])
        self.analyze.assert_not_called()

    def test_no_action_latest_note_is_not_analyzed_twice(self):
        self.records = {"no-action-lead": self.records["no-action-lead"]}
        processor = self._processor()

        first = processor.run(limit=1)
        second = processor.run(limit=1)

        self.assertEqual(1, self.analyze.call_count)
        self.assertEqual(0, self.publish.call_count)
        self.assertEqual(1, first["analyzed"])
        self.assertEqual(0, second["analyzed"])

    def test_waiting_customer_is_skipped_without_hermes(self):
        self.records = {
            "no-action-lead": self.records["no-action-lead"],
            "waiting-lead": self.records["waiting-lead"],
        }
        processor = self._processor()

        result = processor.run(limit=2)

        self.assertEqual(1, result["waiting_customer"])
        self.assertEqual(1, self.analyze.call_count)
        self.assertEqual("no-action-lead", self.analyze.call_args.args[0]["lead_id"])

    def test_unchanged_waiting_note_preserves_later_scheduled_followup(self):
        self.records = {"waiting-lead": self.records["waiting-lead"]}
        processor = self._processor()
        processor.retire_stale = Mock(return_value=["draft"])

        self.assertEqual(1, processor.run()["retired_stale_reviews"])
        processor.retire_stale.reset_mock()
        self.records["waiting-lead"]["sales_name"] = "Updated display name"
        self.assertEqual(0, processor.run()["retired_stale_reviews"])
        processor.retire_stale.assert_not_called()

        # An edited card, a new sent message, and a new customer reply must still retire drafts.
        for field, value in (("body", "Updated conversation"), ("note_id", "new-note"), ("direction", "SHOU")):
            with self.subTest(field=field):
                processor.retire_stale.reset_mock()
                self.records["waiting-lead"]["notes"][0][field] = value
                self.assertEqual(1, processor.run()["retired_stale_reviews"])
                processor.retire_stale.assert_called_once()

        self.records["waiting-lead"]["notes"][0].update(
            note_id="new-outgoing", direction="FA",
        )
        processor.retire_stale = Mock(side_effect=[RuntimeError("Outbox unavailable"), []])
        with self.assertRaisesRegex(RuntimeError, "Outbox unavailable"):
            processor.run()
        processor.run()
        self.assertEqual(2, processor.retire_stale.call_count)

    def test_new_waiting_customer_note_supersedes_older_completed_state(self):
        self.records = {"waiting-lead": self.records["waiting-lead"]}
        self.connection.execute(
            """
            INSERT INTO notes_follow_up_state
              (latest_note_id, lead_id, source_hash, source_created_at,
               crm_snapshot_json, structural_state, status, decision,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, '{}', 'needs_analysis', 'completed',
                    'no_action', ?, ?)
            """,
            (
                "older-note",
                "waiting-lead",
                "older-source",
                "2026-08-19T10:00:00+00:00",
                "2026-08-19T10:00:00+00:00",
                "2026-08-19T10:00:00+00:00",
            ),
        )

        self._processor().run(limit=1)

        rows = self.connection.execute(
            """
            SELECT latest_note_id, status
            FROM notes_follow_up_state
            WHERE lead_id = 'waiting-lead'
            ORDER BY source_created_at
            """
        ).fetchall()
        self.assertEqual(
            [("older-note", "superseded"), ("note-waiting", "skipped")],
            [tuple(row) for row in rows],
        )

    def test_full_scan_supersedes_pending_note_absent_from_current_crm_scope(self):
        processor = self._processor()
        record = self.records["reply-lead"]
        state = notes_core.structural_state(record["notes"])
        processor._discover(record, state)
        self.records = {}

        result = processor.run(limit=1)

        row = self.connection.execute(
            "select status from notes_follow_up_state where lead_id='reply-lead'"
        ).fetchone()
        self.assertEqual("superseded", row["status"])
        self.assertEqual(1, result["superseded_absent"])
        self.analyze.assert_not_called()

    def test_reply_is_published_once_and_publication_id_is_persisted(self):
        processor = self._processor()

        first = processor.run(limit=3)
        second = processor.run(limit=3)

        self.assertEqual(2, self.analyze.call_count)
        self.assertEqual(1, self.publish.call_count)
        self.assertEqual(1, first["published"])
        self.assertEqual(0, second["published"])
        row = self.connection.execute(
            """
            SELECT latest_note_id, status, publication_id
            FROM notes_follow_up_state
            WHERE lead_id = 'reply-lead'
            """
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("note-reply", row["latest_note_id"])
        self.assertEqual("completed", row["status"])
        self.assertEqual("message-1", row["publication_id"])

    def test_existing_review_for_same_note_is_adopted_without_hermes(self):
        self.records = {"reply-lead": self.records["reply-lead"]}
        sync_message_review = Mock()
        processor = self._processor(
            find_existing=lambda lead_id, note_id, email_at: {
                "type": "message",
                "id": "existing-message",
            },
            sync_message_review=sync_message_review,
        )

        result = processor.run(limit=1)

        self.analyze.assert_not_called()
        self.publish.assert_not_called()
        self.assertEqual(1, result["adopted"])
        self.assertEqual(0, result["published"])
        row = self.connection.execute(
            "SELECT status, decision, publication_id FROM notes_follow_up_state"
        ).fetchone()
        self.assertEqual("completed", row["status"])
        self.assertEqual("existing_review", row["decision"])
        self.assertEqual("existing-message", row["publication_id"])
        sync_message_review.assert_called_once_with(
            "reply-lead", "existing-message"
        )

    def test_scan_adopts_all_existing_reviews_before_batch_processing(self):
        self.records = {
            "no-action-lead": self.records["no-action-lead"],
            "reply-lead": self.records["reply-lead"],
        }
        sync_message_review = Mock()
        processor = self._processor(
            find_existing=lambda lead_id, note_id, email_at: (
                {"type": "message", "id": "existing-message"}
                if lead_id == "reply-lead"
                else None
            ),
            sync_message_review=sync_message_review,
        )

        result = processor.run(limit=1)

        row = self.connection.execute(
            """
            SELECT status, decision, publication_id
            FROM notes_follow_up_state
            WHERE lead_id = 'reply-lead'
            """
        ).fetchone()
        self.assertEqual("completed", row["status"])
        self.assertEqual("existing_review", row["decision"])
        self.assertEqual("existing-message", row["publication_id"])
        self.assertEqual(1, result["adopted"])
        sync_message_review.assert_called_once_with(
            "reply-lead", "existing-message"
        )

    def test_existing_review_requires_matching_current_email_at(self):
        from unittest.mock import MagicMock, patch

        expected_email_at = "2026-08-20T10:00:00+00:00"

        class Connection:
            def __init__(self, existing_email_at):
                self.existing_email_at = existing_email_at
                self.cursor_object = MagicMock()
                self.cursor_object.__enter__.return_value = self.cursor_object
                self.cursor_object.__exit__.return_value = False

                def execute(query, _params):
                    if "message_version" in query:
                        self.cursor_object.fetchone.return_value = (
                            ("existing-message",)
                            if self.existing_email_at == expected_email_at
                            else None
                        )
                    else:
                        self.cursor_object.fetchone.return_value = None

                self.cursor_object.execute.side_effect = execute

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def cursor(self):
                return self.cursor_object

        for existing_email_at, expected in (
            (expected_email_at, expected_email_at),
            (None, expected_email_at),
        ):
            with self.subTest(existing_email_at=existing_email_at):
                connection = Connection(existing_email_at)
                with patch("app.db.connect", return_value=connection):
                    result = find_existing_review("reply-lead", "note-reply", expected)

                if existing_email_at == expected:
                    self.assertEqual(
                        {"type": "message", "id": "existing-message"}, result
                    )
                else:
                    self.assertIsNone(result)
                params = connection.cursor_object.execute.call_args_list[0].args[1]
                self.assertEqual(("notes-semantic-v3", "notes-draft-v3"), params[-2:])

    def test_policy_upgrade_retires_same_note_review_before_reprocessing(self):
        from unittest.mock import patch

        with patch(
            "app.secondary.notes_followup.find_existing_review", return_value=None
        ), patch(
            "app.outbox.reject_pending_messages_for_lead",
            return_value=["old-message"],
        ) as reject, patch(
            "app.conversation_actions.dismiss_pending_actions_for_lead",
            return_value=[],
        ) as dismiss:
            retired = retire_stale_reviews(
                "reply-lead",
                "note-reply",
                "2026-08-20T10:00:00+00:00",
            )

        self.assertEqual(["old-message"], retired)
        self.assertIsNone(reject.call_args.kwargs["keep_note_id"])
        self.assertIsNone(dismiss.call_args.kwargs["keep_note_id"])
        self.assertEqual("superseded", reject.call_args.kwargs["signal_event"])

    def test_missing_contact_email_routes_to_action_without_hermes(self):
        self.records = {"reply-lead": self.records["reply-lead"]}
        self.records["reply-lead"]["contact_email"] = None
        self.publish = Mock(
            return_value={"status": "pending", "action_id": "action-contact"}
        )
        processor = self._processor()

        result = processor.run(limit=1)

        self.analyze.assert_not_called()
        self.assertEqual(1, result["actions"])
        analysis = self.publish.call_args.args[2]
        self.assertEqual("manual_review", analysis["semantic"]["decision"])
        self.assertEqual(
            "联系人邮箱不可用，无法生成客户回复",
            analysis["semantic"]["reason"],
        )

    def test_linkedin_note_does_not_require_contact_email(self):
        self.records = {"reply-lead": self.records["reply-lead"]}
        record = self.records["reply-lead"]
        record["contact_email"] = None
        record["contact_linkedin_url"] = (
            "https://www.linkedin.com/in/reply-lead/"
        )
        record["notes"][0]["channel"] = "linkedin"
        processor = self._processor()

        result = processor.run(limit=1)

        self.assertEqual(1, result["hermes_calls"])
        self.analyze.assert_called_once()

    def test_default_first_run_baselines_history_without_hermes(self):
        processor = NotesFollowUpProcessor(
            connect=lambda: self.connection,
            environment={},
            now=lambda: datetime(2026, 8, 21, tzinfo=timezone.utc),
            read_records=self._read_records,
            analyze=self.analyze,
            publish=self.publish,
            find_existing=lambda lead_id, note_id, email_at: None,
            retire_stale=lambda lead_id, note_id, email_at: [],
        )

        result = processor.run(limit=3)

        self.assertTrue(result["bootstrap"])
        self.assertEqual(2, result["baselined"])
        self.assertEqual(0, result["processed"])
        self.analyze.assert_not_called()
        statuses = {
            row["lead_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT lead_id, status FROM notes_follow_up_state"
            )
        }
        self.assertEqual("baseline", statuses["no-action-lead"])
        self.assertEqual("baseline", statuses["reply-lead"])
        self.assertEqual("skipped", statuses["waiting-lead"])


if __name__ == "__main__":
    unittest.main()
