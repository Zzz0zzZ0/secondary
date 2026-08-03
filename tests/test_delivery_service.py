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
    def test_provider_ids_are_accepted_but_not_persisted(self):
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
                    "lease-token",
                    provider_message_id="provider-message",
                    provider_thread_id="provider-thread",
                )

        sql = "\n".join(query for query, _ in cursor.calls)
        params = [value for _, values in cursor.calls for value in values]
        self.assertNotIn("provider_message_id", sql)
        self.assertNotIn("provider_thread_id", sql)
        self.assertNotIn("worker_id", sql)
        self.assertNotIn("heartbeat_at", sql)
        self.assertNotIn("locked_at", sql)
        self.assertNotIn("provider-message", params)
        self.assertNotIn("provider-thread", params)
        self.assertNotIn("worker-1", params)
        self.assertIn("lease_token_hash = %s", sql)
        self.assertIn("lease_expires_at > now()", sql)
        self.assertEqual({"status": "sent", "sent_at": sent_at}, result)


class LeaseTest(unittest.TestCase):
    def test_claim_uses_token_as_the_only_lease_owner(self):
        delivery_id = uuid4()
        message_version_id = uuid4()
        expires_at = datetime.now(timezone.utc)
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
                expires_at,
            )],
            fetchall_results=[[]],
        )

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            with patch.object(
                delivery_service.secrets,
                "token_urlsafe",
                return_value="lease-token",
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
        self.assertEqual("lease-token", item["lease_token"])
        self.assertEqual(str(delivery_id), item["delivery_id"])
        self.assertEqual(
            "email:chloe@okgminerals.com",
            item["sender_account_ref"],
        )

    def test_heartbeat_renews_by_token_without_worker_state(self):
        expires_at = datetime.now(timezone.utc)
        cursor = FakeCursor(fetchone_results=[(expires_at,)])

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            result = delivery_service.renew_delivery_lease(
                uuid4(),
                "worker-1",
                "lease-token",
            )

        sql, params = cursor.calls[0]
        self.assertNotIn("worker_id", sql)
        self.assertNotIn("heartbeat_at", sql)
        self.assertNotIn("worker-1", params)
        self.assertIn("lease_token_hash = %s", sql)
        self.assertIn("lease_expires_at > now()", sql)
        self.assertEqual(expires_at, result)

    def test_failure_is_recorded_by_token_without_worker_state(self):
        cursor = FakeCursor(fetchone_results=[(1, 5)])

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            result = delivery_service.fail_delivery(
                uuid4(),
                "worker-1",
                "lease-token",
                "retryable",
                "TEMPORARY",
                "try again",
            )

        sql = "\n".join(query for query, _ in cursor.calls)
        params = [value for _, values in cursor.calls for value in values]
        self.assertNotIn("worker_id", sql)
        self.assertNotIn("heartbeat_at", sql)
        self.assertNotIn("locked_at", sql)
        self.assertNotIn("worker-1", params)
        self.assertIn("lease_token_hash = %s", sql)
        self.assertIn("lease_expires_at > now()", sql)
        self.assertEqual(
            {"status": "retry_wait", "retry_after_minutes": 1},
            result,
        )

    def test_queue_status_marks_expired_leases_unknown_lazily(self):
        cursor = FakeCursor(
            fetchall_results=[[], [("queued", 2), ("unknown", 1)]],
        )

        with patch.object(
            delivery_service,
            "connect",
            return_value=FakeConnection(cursor),
        ):
            result = delivery_service.queue_status()

        expiration_sql = cursor.calls[0][0]
        self.assertIn("status = 'unknown'", expiration_sql)
        self.assertIn("LEASE_EXPIRED", expiration_sql)
        self.assertNotIn("worker_id", expiration_sql)
        self.assertNotIn("heartbeat_at", expiration_sql)
        self.assertNotIn("locked_at", expiration_sql)
        self.assertEqual({"queued": 2, "unknown": 1}, result)


if __name__ == "__main__":
    unittest.main()
