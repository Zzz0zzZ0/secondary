import unittest
from unittest.mock import patch
from uuid import uuid4

from app import outbox


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self._cursor


def pending_message(channel, recipient, sales_name):
    output = {
        "language": "English",
        "content": {
            "subject": "Subject" if channel == "email" else None,
            "body": "Message body",
        },
    }
    return (
        uuid4(),
        "lead-1",
        recipient,
        {"sales": {"name": sales_name}},
        output,
        None,
        "pending_review",
        1,
        channel,
    )


class OutboxSenderAccountTest(unittest.TestCase):
    def approve(self, row):
        cursor = FakeCursor(row)
        with patch.object(outbox, "connect", return_value=FakeConnection(cursor)):
            with patch.object(outbox, "_json", side_effect=lambda value: value):
                with patch.object(outbox, "notify_secondary_outbox_event"):
                    outbox.approve_message(row[0], "reviewer")
        return cursor

    def delivery_insert_params(self, cursor):
        for query, params in cursor.calls:
            if "INSERT INTO sales_automation.delivery_outbox" in query:
                return params
        self.fail("delivery_outbox insert was not executed")

    def test_email_approval_stores_mapped_sender_and_html_body(self):
        cursor = self.approve(
            pending_message("email", "customer@example.com", "倩文 于")
        )

        params = self.delivery_insert_params(cursor)

        self.assertEqual("email:chloe@okgminerals.com", params[5])
        self.assertEqual("<p>Message body</p>", params[6]["body_html"])

    def test_linkedin_approval_leaves_sender_reference_empty(self):
        cursor = self.approve(
            pending_message(
                "linkedin",
                "https://www.linkedin.com/in/customer",
                "倩文 于",
            )
        )

        params = self.delivery_insert_params(cursor)

        self.assertIsNone(params[5])

    def test_email_approval_rejects_missing_sender_mapping(self):
        cursor = FakeCursor(
            pending_message("email", "customer@example.com", "未映射 销售")
        )

        with self.assertRaisesRegex(
            RuntimeError, "valid sender account mapping"
        ):
            with patch.object(outbox, "connect", return_value=FakeConnection(cursor)):
                with patch.object(outbox, "_json", side_effect=lambda value: value):
                    with patch.object(outbox, "notify_secondary_outbox_event"):
                        outbox.approve_message(cursor.row[0], "reviewer")


if __name__ == "__main__":
    unittest.main()
