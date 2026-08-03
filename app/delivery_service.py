import hashlib
import json
import os
import secrets
import uuid

from .db import connect
from .secondary_signals import notify_secondary_outbox_event


RETRY_DELAYS_MINUTES = [1, 5, 30, 120]
DEFAULT_LEASE_SECONDS = 300


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def lease_seconds():
    raw_value = os.getenv("OUTBOX_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("OUTBOX_LEASE_SECONDS must be an integer") from exc
    if value < 30 or value > 3600:
        raise RuntimeError("OUTBOX_LEASE_SECONDS must be between 30 and 3600")
    return value


def expire_stale_leases(cursor):
    cursor.execute(
        """
        WITH expired AS (
          UPDATE sales_automation.delivery_outbox
          SET status = 'unknown',
              last_error_code = 'LEASE_EXPIRED',
              last_error_message = 'Worker lease expired before a delivery result was recorded',
              lease_token_hash = NULL,
              lease_expires_at = NULL,
              updated_at = now()
          WHERE status = 'sending'
            AND lease_expires_at IS NOT NULL
            AND lease_expires_at <= now()
          RETURNING id
        )
        UPDATE sales_automation.delivery_attempt attempt
        SET status = 'unknown', error_code = 'LEASE_EXPIRED',
            error_message = 'Worker lease expired before a delivery result was recorded',
            completed_at = now()
        FROM expired
        WHERE attempt.outbox_id = expired.id
          AND attempt.status = 'started'
        RETURNING attempt.id
        """
    )
    return len(cursor.fetchall())


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
    # worker_id remains in the API contract; the lease token is the ownership proof.
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
                lease_token_hash = %s,
                lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            FROM candidate
            WHERE outbox.id = candidate.id
            RETURNING outbox.id, outbox.message_version_id, outbox.channel,
                      outbox.provider, outbox.recipient_original,
                      outbox.sender_account_ref, outbox.payload,
                      outbox.idempotency_key, outbox.attempt_count,
                      outbox.lease_expires_at
            """,
            (channels, providers, cooldown, token_hash, duration),
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
            "lease_expires_at": row[9],
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


def renew_delivery_lease(delivery_id, worker_id, lease_token):
    duration = lease_seconds()
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            WHERE id = %s
              AND status = 'sending'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            RETURNING lease_expires_at
            """,
            (duration, delivery_id, _token_hash(lease_token)),
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
    # Kept in the function signature for existing consumers; intentionally not persisted.
    notification = None
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET status = 'sent',
                sent_at = now(),
                lease_token_hash = NULL,
                lease_expires_at = NULL,
                last_error_code = NULL,
                last_error_message = NULL,
                updated_at = now()
            WHERE id = %s
              AND status = 'sending'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            RETURNING attempt_count, sent_at, message_version_id
            """,
            (
                delivery_id,
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


def fail_delivery(delivery_id, worker_id, lease_token, result, error_code, error_message):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT attempt_count, max_attempts
            FROM sales_automation.delivery_outbox
            WHERE id = %s
              AND status = 'sending'
              AND lease_token_hash = %s
              AND lease_expires_at > now()
            FOR UPDATE
            """,
            (delivery_id, _token_hash(lease_token)),
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
                lease_token_hash = NULL,
                lease_expires_at = NULL,
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
        expire_stale_leases(cursor)
        cursor.execute(
            """
            SELECT status, count(*)
            FROM sales_automation.delivery_outbox
            GROUP BY status
            ORDER BY status
            """
        )
        return {status: count for status, count in cursor.fetchall()}
