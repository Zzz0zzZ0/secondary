import unittest
from unittest.mock import patch

from app import conversation_actions


class FakeCursor:
    def __init__(self, rows=None, rowcount=1):
        self.rows = list(rows or [])
        self.rowcount = rowcount
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def cursor(self):
        return self.cursor_value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class ConversationActionsTest(unittest.TestCase):
    def test_create_is_deterministic_and_never_writes_message_or_outbox(self):
        cursor = FakeCursor()
        with patch.object(
            conversation_actions, "connect", return_value=FakeConnection(cursor)
        ):
            result = conversation_actions.create_review_action(
                "run-1",
                "lead-1",
                "internal_task",
                {"company": {"name": "Example"}},
                {"reason": "Prepare the requested quotation internally."},
            )

        self.assertEqual("pending", result["status"])
        self.assertTrue(result["created"])
        sql = "\n".join(query for query, _ in cursor.calls)
        self.assertIn("conversation_action", sql)
        self.assertNotIn("message_version", sql)
        self.assertNotIn("delivery_outbox", sql)

    def test_only_internal_task_and_manual_review_are_accepted(self):
        for action_type in ("reply", "referral", "no_action"):
            with self.assertRaises(RuntimeError):
                conversation_actions.create_review_action(
                    "run-1", "lead-1", action_type, {}, {"reason": "x"}
                )

    def test_resolve_accepts_only_terminal_statuses(self):
        with self.assertRaises(RuntimeError):
            conversation_actions.resolve_review_action(
                "action-1", "pending", "review-ui"
            )

    def test_dismiss_stale_actions_keeps_the_current_note(self):
        cursor = FakeCursor(rows=[("stale-action",)])
        with patch.object(
            conversation_actions, "connect", return_value=FakeConnection(cursor)
        ):
            dismissed = conversation_actions.dismiss_pending_actions_for_lead(
                "lead-1",
                "notes-poller",
                "Superseded by current CRM Notes.",
                keep_note_id="note-current",
                keep_email_at="2026-08-20T10:00:00+00:00",
            )

        self.assertEqual(["stale-action"], dismissed)
        query, params = cursor.calls[0]
        self.assertIn("status = 'dismissed'", query)
        self.assertIn("IS DISTINCT FROM", query)
        self.assertIn("%s::text IS NULL", query)
        self.assertEqual("lead-1", params[2])
        self.assertEqual("note-current", params[3])
        self.assertEqual("2026-08-20T10:00:00+00:00", params[5])


if __name__ == "__main__":
    unittest.main()
