import hashlib
import json
import os
import secrets
import uuid

from .db import connect
from .secondary_signals import notify_secondary_outbox_event
from .worker import RETRY_DELAYS_MINUTES, expire_stale_leases, lease_seconds


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    duration = lease_seconds()
    cooldown = _cooldown_hours()
    with connect() as conn, conn.cursor() as cursor:
        expire_stale_leases(cursor)
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
                AND NOT EXISTS (
                  SELECT 1
                  FROM sales_automation.delivery_outbox active
                  WHERE active.channel = delivery_outbox.channel
                    AND lower(active.recipient_original) =
                        lower(delivery_outbox.recipient_original)
                    AND active.status = 'sending'
                )
                AND NOT EXISTS (
                  SELECT 1
                  FROM sales_automation.delivery_outbox prior
                  WHERE prior.channel = delivery_outbox.channel
                    AND lower(prior.recipient_original) =
                        lower(delivery_outbox.recipient_original)
                    AND prior.status = 'sent'
                    AND prior.sent_at > now() - (%s * interval '1 hour')
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
                worker_id = %s,
                lease_token_hash = %s,
                locked_at = now(),
                heartbeat_at = now(),
                lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            FROM candidate
            WHERE outbox.id = candidate.id
            RETURNING outbox.id, outbox.message_version_id, outbox.channel,
                      outbox.provider, outbox.recipient_original, outbox.payload,
                      outbox.idempotency_key, outbox.attempt_count,
                      outbox.lease_expires_at
            """,
            (channels, providers, cooldown, worker_id, token_hash, duration),
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
            (attempt_id, row[0], row[7]),
        )
        payload = row[5] if isinstance(row[5], dict) else json.loads(row[5])
        return {
            "delivery_id": str(row[0]),
            "message_version_id": str(row[1]),
            "lease_token": token,
            "lease_expires_at": row[8],
            "schema_version": "1.0",
            "idempotency_key": row[6],
            "channel": row[2],
            "provider": row[3],
            "recipient": row[4],
            "payload": payload,
            "metadata": {
                "lead_id": payload.get("lead_id"),
                "attempt_no": row[7],
            },
        }


def renew_delivery_lease(delivery_id, worker_id, lease_token):
    duration = lease_seconds()
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET heartbeat_at = now(),
                lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            WHERE id = %s
              AND status = 'sending'
              AND worker_id = %s
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            RETURNING lease_expires_at
            """,
            (duration, delivery_id, worker_id, _token_hash(lease_token)),
        )
        row = cursor.fetchone()
        return row[0] if row else None


def complete_delivery(
    delivery_id,
    worker_id,
    lease_token,
    provider_message_id=None,
    provider_thread_id=None,
):
    notification = None
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET status = 'sent',
                provider_message_id = %s,
                provider_thread_id = %s,
                sent_at = now(),
                worker_id = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                locked_at = NULL,
                last_error_code = NULL,
                last_error_message = NULL,
                updated_at = now()
            WHERE id = %s
              AND status = 'sending'
              AND worker_id = %s
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            RETURNING attempt_count, sent_at, message_version_id
            """,
            (
                provider_message_id,
                provider_thread_id,
                delivery_id,
                worker_id,
                _token_hash(lease_token),
            ),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cursor.execute(
            """
            UPDATE sales_automation.delivery_attempt
            SET status = 'sent',
                provider_message_id = %s,
                provider_thread_id = %s,
                completed_at = now()
            WHERE outbox_id = %s AND attempt_no = %s AND status = 'started'
            """,
            (provider_message_id, provider_thread_id, delivery_id, row[0]),
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


def fail_delivery(delivery_id, worker_id, lease_token, result, error_code, error_message):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT attempt_count, max_attempts
            FROM sales_automation.delivery_outbox
            WHERE id = %s
              AND status = 'sending'
              AND worker_id = %s
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            FOR UPDATE
            """,
            (delivery_id, worker_id, _token_hash(lease_token)),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        attempt_no, max_attempts = row
        delay = None
        if result == "retryable" and attempt_no < max_attempts:
            if attempt_no <= len(RETRY_DELAYS_MINUTES):
                delay = RETRY_DELAYS_MINUTES[attempt_no - 1]
                status = "retry_wait"
            else:
                status = "failed"
        elif result == "unknown":
            status = "unknown"
        else:
            status = "failed"

        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET status = %s,
                available_at = CASE
                  WHEN %s IS NULL THEN available_at
                  ELSE now() + (%s * interval '1 minute')
                END,
                last_error_code = %s,
                last_error_message = %s,
                worker_id = NULL,
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL,
                locked_at = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (
                status,
                delay,
                delay,
                error_code,
                (error_message or "")[:2000],
                delivery_id,
            ),
        )
        cursor.execute(
            """
            UPDATE sales_automation.delivery_attempt
            SET status = %s,
                error_code = %s,
                error_message = %s,
                completed_at = now()
            WHERE outbox_id = %s AND attempt_no = %s AND status = 'started'
            """,
            (status, error_code, (error_message or "")[:2000], delivery_id, attempt_no),
        )
        return {"status": status, "retry_after_minutes": delay}


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


def reap_expired_leases():
    with connect() as conn, conn.cursor() as cursor:
        return expire_stale_leases(cursor)
