import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

from app import delivery_service


class FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.calls = []
        self.fetchone_results = list(fetchone_results or [])
        self.fetchall_results = list(fetchall_results or [])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return self.fetchone_results.pop(0)

    def fetchall(self):
        return self.fetchall_results.pop(0)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def cursor(self):
        return self._cursor


class CompleteDeliveryTest(unittest.TestCase):
    def test_complete_only_uses_delivery_id_and_worker_id(self):
        sent_at = datetime.now(timezone.utc)
        message_version_id = uuid4()
        cursor = FakeCursor(fetchone_results=[(1, sent_at, message_version_id)])

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            with patch.object(delivery_service, "notify_secondary_outbox_event"):
                result = delivery_service.complete_delivery(
                    uuid4(),
                    "worker-1",
                )

        sql = "\n".join(query for query, _ in cursor.calls)
        params = [value for _, values in cursor.calls for value in values]
        self.assertNotIn("worker_id", sql)
        self.assertNotIn("heartbeat_at", sql)
        self.assertNotIn("locked_at", sql)
        self.assertNotIn("worker-1", params)
        self.assertNotIn("lease_token_hash", sql)
        self.assertNotIn("lease_expires_at", sql)
        self.assertEqual({"status": "sent", "sent_at": sent_at}, result)


class DeliveryStateTest(unittest.TestCase):
    def test_claim_does_not_create_a_lease(self):
        delivery_id = uuid4()
        message_version_id = uuid4()
        cursor = FakeCursor(
            fetchone_results=[(
                delivery_id,
                message_version_id,
                "email",
                "email",
                "customer@example.com",
                "email:chloe@okgminerals.com",
                {"lead_id": "lead-1"},
                "idempotency-1",
                1,
            )],
            fetchall_results=[[]],
        )

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            item = delivery_service.claim_delivery(
                "worker-1",
                ["email"],
                ["email"],
            )

        sql = "\n".join(query for query, _ in cursor.calls)
        params = [
            value
            for _, values in cursor.calls
            if values is not None
            for value in values
        ]
        self.assertNotIn("worker_id", sql)
        self.assertNotIn("heartbeat_at", sql)
        self.assertNotIn("locked_at", sql)
        self.assertNotIn("worker-1", params)
        self.assertIsNone(item["lease_expires_at"])
        self.assertEqual(str(delivery_id), item["delivery_id"])
        self.assertEqual(
            "email:chloe@okgminerals.com",
            item["sender_account_ref"],
        )

    def test_system_test_claim_bypasses_recipient_cooldown_and_mutex(self):
        cursor = FakeCursor(fetchone_results=[None], fetchall_results=[[]])

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            delivery_service.claim_delivery("worker-1", ["email"], ["email"])

        sql = "\n".join(query for query, _ in cursor.calls)
        self.assertEqual(2, sql.count("payload->>'lead_id' LIKE 'SYSTEM-TEST-%%'"))

    def test_heartbeat_is_a_compatibility_noop(self):
        cursor = FakeCursor(fetchone_results=[(1,)])

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            result = delivery_service.heartbeat_delivery(
                uuid4(),
                "worker-1",
                "lease-token",
            )

        sql, params = cursor.calls[0]
        self.assertNotIn("worker_id", sql)
        self.assertNotIn("heartbeat_at", sql)
        self.assertNotIn("worker-1", params)
        self.assertNotIn("lease_token_hash", sql)
        self.assertNotIn("lease_expires_at", sql)
        self.assertTrue(result)

    def test_failure_only_uses_delivery_id_and_worker_id(self):
        cursor = FakeCursor(fetchone_results=[(1,)])

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            result = delivery_service.fail_delivery(
                uuid4(),
                "worker-1",
            )

        sql = "\n".join(query for query, _ in cursor.calls)
        params = [value for _, values in cursor.calls for value in values]
        self.assertNotIn("worker_id", sql)
        self.assertNotIn("heartbeat_at", sql)
        self.assertNotIn("locked_at", sql)
        self.assertNotIn("worker-1", params)
        self.assertNotIn("lease_token_hash", sql)
        self.assertNotIn("lease_expires_at", sql)
        self.assertIn("CONSUMER_FAILED", sql)
        self.assertEqual({"status": "failed"}, result)

    def test_queue_status_does_not_expire_sending_tasks(self):
        cursor = FakeCursor(fetchall_results=[[("queued", 2), ("sending", 1)]])

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            result = delivery_service.queue_status()

        status_sql = cursor.calls[0][0]
        self.assertNotIn("LEASE_EXPIRED", status_sql)
        self.assertNotIn("lease_token_hash", status_sql)
        self.assertNotIn("lease_expires_at", status_sql)
        self.assertEqual({"queued": 2, "sending": 1}, result)


if __name__ == "__main__":
    unittest.main()
