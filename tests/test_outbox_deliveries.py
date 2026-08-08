import unittest
from unittest.mock import patch

from app import outbox


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self.cursor_value


class OutboxDeliveriesTest(unittest.TestCase):
    def test_lists_message_content_for_read_only_review_ui(self):
        row = (
            "delivery-1",
            "message-1",
            "email",
            "email",
            "customer@example.com",
            "email:chloe@example.com",
            {"subject": "Hello", "body": "Message body"},
            "queued",
            1,
            5,
            "2026-08-03T10:00:00+00:00",
            None,
            None,
            None,
            "2026-08-03T09:00:00+00:00",
            "2026-08-03T09:00:00+00:00",
        )
        cursor = FakeCursor([row])
        with patch.object(outbox, "connect", return_value=FakeConnection(cursor)):
            records = outbox.list_outbox_deliveries(100)

        self.assertEqual(records[0]["recipient"], "customer@example.com")
        self.assertEqual(records[0]["payload"]["body"], "Message body")
        self.assertEqual(records[0]["sender_account_ref"], "email:chloe@example.com")
        self.assertIn("payload", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1], (100,))


if __name__ == "__main__":
    unittest.main()
