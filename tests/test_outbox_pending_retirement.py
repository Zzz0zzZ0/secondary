import unittest
from unittest.mock import patch

from app import outbox


class FakeCursor:
    def __init__(self, records):
        self.records = records
        self.calls = []
        self.returning = []
        self.approvals = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if "UPDATE sales_automation.message_version" in query:
            lead_id = params[0]
            keep_note_id = params[1]
            self.returning = []
            for record in self.records:
                if (
                    record["lead_id"] == lead_id
                    and record["status"] == "pending_review"
                    and (
                        keep_note_id is None
                        or record.get("note_id") != keep_note_id
                    )
                ):
                    record["status"] = "rejected"
                    self.returning.append((record["id"],))
        elif "INSERT INTO sales_automation.message_approval" in query:
            self.approvals.append(params)

    def fetchall(self):
        return self.returning


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self.cursor_value


class PendingMessageRetirementTest(unittest.TestCase):
    def setUp(self):
        self.cursor = FakeCursor(
            [
                {"id": "pending-1", "lead_id": "lead-1", "status": "pending_review", "note_id": None},
                {"id": "pending-2", "lead_id": "lead-1", "status": "pending_review", "note_id": "note-current", "email_at": "2026-08-20T10:00:00+00:00"},
                {"id": "approved-1", "lead_id": "lead-1", "status": "approved"},
                {"id": "other-1", "lead_id": "lead-2", "status": "pending_review"},
            ]
        )
        self.connection = FakeConnection(self.cursor)

    def test_retires_all_pending_versions_and_notifies_each(self):
        with patch.object(outbox, "connect", return_value=self.connection):
            with patch.object(outbox, "notify_secondary_outbox_event") as notify:
                retired = outbox.reject_pending_messages_for_lead(
                    "lead-1", "review-ui", "superseded by CRM update"
                )

        self.assertEqual(["pending-1", "pending-2"], retired)
        self.assertEqual(2, len(self.cursor.approvals))
        self.assertEqual(
            ["review-ui", "review-ui"],
            [params[2] for params in self.cursor.approvals],
        )
        self.assertEqual(
            [
                ("pending-1", "rejected"),
                ("pending-2", "rejected"),
            ],
            [call.args for call in notify.call_args_list],
        )
        self.assertEqual("approved", self.cursor.records[2]["status"])
        self.assertEqual("pending_review", self.cursor.records[3]["status"])

    def test_repeated_call_has_no_new_audit_or_notifications(self):
        with patch.object(outbox, "connect", return_value=self.connection):
            with patch.object(outbox, "notify_secondary_outbox_event") as notify:
                first = outbox.reject_pending_messages_for_lead("lead-1", "review-ui")
                notify.reset_mock()
                second = outbox.reject_pending_messages_for_lead("lead-1", "review-ui")

        self.assertEqual(["pending-1", "pending-2"], first)
        self.assertEqual([], second)
        self.assertEqual(2, len(self.cursor.approvals))
        notify.assert_not_called()

    def test_can_keep_review_generated_from_current_note(self):
        with patch.object(outbox, "connect", return_value=self.connection):
            with patch.object(outbox, "notify_secondary_outbox_event") as notify:
                retired = outbox.reject_pending_messages_for_lead(
                    "lead-1",
                    "notes-poller",
                    "Superseded by current CRM Notes.",
                    keep_note_id="note-current",
                    keep_email_at="2026-08-20T10:00:00+00:00",
                )

        self.assertEqual(["pending-1"], retired)
        self.assertEqual("pending_review", self.cursor.records[1]["status"])
        self.assertEqual([("pending-1", "rejected")], [call.args for call in notify.call_args_list])


if __name__ == "__main__":
    unittest.main()
