import json
import os
import secrets
import uuid

from .db import connect
from .secondary_signals import notify_secondary_outbox_event


def _cooldown_hours():
    raw = os.getenv(
        "OUTBOX_CONTACT_COOLDOWN_HOURS",
        os.getenv("EMAIL_CONTACT_COOLDOWN_HOURS", "24"),
    )
    value = int(raw)
    if value < 0 or value > 8760:
        raise RuntimeError("OUTBOX_CONTACT_COOLDOWN_HOURS must be between 0 and 8760")
    return value


def claim_delivery(worker_id, channels, providers):
    # worker_id and lease_token remain in the API contract for existing consumers.
    token = secrets.token_urlsafe(32)
    cooldown = _cooldown_hours()
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            WITH candidate AS (
              SELECT id
              FROM sales_automation.delivery_outbox
              WHERE channel = ANY(%s)
                AND provider = ANY(%s)
                AND status IN ('queued', 'retry_wait')
                AND available_at <= now()
                AND attempt_count < max_attempts
                AND (
                  delivery_outbox.payload->>'lead_id' LIKE 'SYSTEM-TEST-%%'
                  OR NOT EXISTS (
                    SELECT 1
                    FROM sales_automation.delivery_outbox active
                    WHERE active.channel = delivery_outbox.channel
                      AND lower(active.recipient_original) =
                          lower(delivery_outbox.recipient_original)
                      AND active.status = 'sending'
                  )
                )
                AND (
                  delivery_outbox.payload->>'lead_id' LIKE 'SYSTEM-TEST-%%'
                  OR NOT EXISTS (
                    SELECT 1
                    FROM sales_automation.delivery_outbox prior
                    WHERE prior.channel = delivery_outbox.channel
                      AND lower(prior.recipient_original) =
                          lower(delivery_outbox.recipient_original)
                      AND prior.status = 'sent'
                      AND prior.sent_at > now() - (%s * interval '1 hour')
                  )
                )
                AND pg_try_advisory_xact_lock(
                  hashtextextended(
                    delivery_outbox.channel || ':' ||
                    lower(delivery_outbox.recipient_original),
                    0
                  )
                )
              ORDER BY created_at
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE sales_automation.delivery_outbox outbox
            SET status = 'sending',
                attempt_count = attempt_count + 1,
                updated_at = now()
            FROM candidate
            WHERE outbox.id = candidate.id
            RETURNING outbox.id, outbox.message_version_id, outbox.channel,
                      outbox.provider, outbox.recipient_original,
                      outbox.sender_account_ref, outbox.payload,
                      outbox.idempotency_key, outbox.attempt_count
            """,
            (channels, providers, cooldown),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        attempt_id = uuid.uuid4()
        cursor.execute(
            """
            INSERT INTO sales_automation.delivery_attempt
              (id, outbox_id, attempt_no, status)
            VALUES (%s, %s, %s, 'started')
            """,
            (attempt_id, row[0], row[8]),
        )
        payload = row[6] if isinstance(row[6], dict) else json.loads(row[6])
        return {
            "delivery_id": str(row[0]),
            "message_version_id": str(row[1]),
            "lease_token": token,
            "lease_expires_at": None,
            "schema_version": "1.0",
            "idempotency_key": row[7],
            "channel": row[2],
            "provider": row[3],
            "recipient": row[4],
            "sender_account_ref": row[5],
            "payload": payload,
            "metadata": {
                "lead_id": payload.get("lead_id"),
                "attempt_no": row[8],
            },
        }


def heartbeat_delivery(delivery_id, worker_id, lease_token):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM sales_automation.delivery_outbox
            WHERE id = %s
              AND status = 'sending'
            """,
            (delivery_id,),
        )
        row = cursor.fetchone()
        return row is not None


def complete_delivery(
    delivery_id,
    worker_id,
):
    notification = None
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET status = 'sent',
                sent_at = now(),
                last_error_code = NULL,
                last_error_message = NULL,
                updated_at = now()
            WHERE id = %s
              AND status = 'sending'
            RETURNING attempt_count, sent_at, message_version_id
            """,
            (delivery_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            UPDATE sales_automation.delivery_attempt
            SET status = 'sent',
                completed_at = now()
            WHERE outbox_id = %s AND attempt_no = %s AND status = 'started'
            """,
            (delivery_id, row[0]),
        )
        notification = (row[2], row[1])
        response = {"status": "sent", "sent_at": row[1]}
    if notification:
        notify_secondary_outbox_event(
            notification[0],
            "sent",
            notification[1],
        )
    return response


def fail_delivery(delivery_id, worker_id):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET status = 'failed',
                last_error_code = 'CONSUMER_FAILED',
                last_error_message = 'Consumer reported delivery failure',
                updated_at = now()
            WHERE id = %s
              AND status = 'sending'
            RETURNING attempt_count
            """,
            (delivery_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        attempt_no = row[0]
        cursor.execute(
            """
            UPDATE sales_automation.delivery_attempt
            SET status = 'failed',
                error_code = 'CONSUMER_FAILED',
                error_message = 'Consumer reported delivery failure',
                completed_at = now()
            WHERE outbox_id = %s AND attempt_no = %s AND status = 'started'
            """,
            (delivery_id, attempt_no),
        )
        return {"status": "failed"}


def queue_status():
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT status, count(*)
            FROM sales_automation.delivery_outbox
            GROUP BY status
            ORDER BY status
            """
        )
        return {status: count for status, count in cursor.fetchall()}
