import hashlib
import json
import os
import socket
import uuid

from .db import connect
from . import gmail_sender


RETRY_DELAYS_MINUTES = [1, 5, 30, 120]
DEFAULT_LEASE_SECONDS = 300


def lease_seconds():
    raw_value = os.getenv("OUTBOX_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("OUTBOX_LEASE_SECONDS must be an integer") from exc
    if value < 30 or value > 3600:
        raise RuntimeError("OUTBOX_LEASE_SECONDS must be between 30 and 3600")
    return value


def worker_identity():
    configured = os.getenv("OUTBOX_WORKER_ID")
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}"


def expire_stale_leases(cursor):
    cursor.execute(
        """
        WITH expired AS (
          UPDATE sales_automation.delivery_outbox
          SET status = 'unknown',
              last_error_code = 'LEASE_EXPIRED',
              last_error_message = 'Worker lease expired before a delivery result was recorded',
              worker_id = NULL, lease_token_hash = NULL,
              lease_expires_at = NULL, heartbeat_at = NULL,
              locked_at = NULL, updated_at = now()
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


def claim_one(worker_id=None):
    cooldown_hours = int(os.getenv("EMAIL_CONTACT_COOLDOWN_HOURS", "24"))
    if cooldown_hours < 0 or cooldown_hours > 8760:
        raise RuntimeError("EMAIL_CONTACT_COOLDOWN_HOURS must be between 0 and 8760")
    worker_id = worker_id or worker_identity()
    duration = lease_seconds()
    with connect() as conn:
        with conn.cursor() as cursor:
            expire_stale_leases(cursor)
            cursor.execute(
                """
                WITH candidate AS (
                  SELECT id
                  FROM sales_automation.delivery_outbox
                  WHERE channel = 'email'
                    AND provider = 'email'
                    AND status IN ('queued', 'retry_wait')
                    AND available_at <= now()
                    AND attempt_count < max_attempts
                    AND NOT EXISTS (
                      SELECT 1 FROM sales_automation.delivery_outbox active
                      WHERE active.channel = delivery_outbox.channel
                        AND lower(active.recipient_original) =
                            lower(delivery_outbox.recipient_original)
                        AND active.status = 'sending'
                    )
                    AND NOT EXISTS (
                      SELECT 1 FROM sales_automation.delivery_outbox prior
                      WHERE prior.channel = 'email'
                        AND lower(prior.recipient_original) = lower(delivery_outbox.recipient_original)
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
                SET status = 'sending', attempt_count = attempt_count + 1,
                    worker_id = %s, locked_at = now(), heartbeat_at = now(),
                    lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                FROM candidate
                WHERE outbox.id = candidate.id
                RETURNING outbox.id, outbox.payload, outbox.payload_sha256, outbox.attempt_count
                """,
                (cooldown_hours, worker_id, duration),
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
                (attempt_id, row[0], row[3]),
            )
            return row[0], row[1], row[2], row[3], attempt_id, worker_id


def renew_lease(outbox_id, worker_id):
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
              AND lease_expires_at > now()
            """,
            (duration, outbox_id, worker_id),
        )
        return cursor.rowcount == 1


def _http_status(exc):
    google_status = getattr(getattr(exc, "resp", None), "status", None)
    if google_status is not None:
        return google_status
    return getattr(getattr(exc, "response", None), "status_code", None)


def classify_error(exc, attempt_no):
    status = _http_status(exc)
    if status == 429 or (status is not None and status >= 500):
        if attempt_no <= len(RETRY_DELAYS_MINUTES):
            return "retry_wait", f"HTTP_{status}", RETRY_DELAYS_MINUTES[attempt_no - 1]
        return "failed", f"HTTP_{status}", None
    if status in (401, 403):
        return "failed", f"HTTP_{status}", None
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "Timeout", "ConnectTimeout", "ReadTimeout", "ConnectionError"
    }:
        return "unknown", type(exc).__name__, None
    return "failed", type(exc).__name__, None


def complete_success(outbox_id, attempt_id, result, worker_id):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.delivery_outbox
            SET status = 'sent', provider_message_id = %s, provider_thread_id = %s,
                sent_at = now(), worker_id = NULL, lease_token_hash = NULL,
                lease_expires_at = NULL,
                heartbeat_at = NULL, locked_at = NULL,
                last_error_code = NULL, last_error_message = NULL,
                updated_at = now()
            WHERE id = %s AND status = 'sending' AND worker_id = %s
            """,
            (result.get("id"), result.get("threadId"), outbox_id, worker_id),
        )
        if cursor.rowcount != 1:
            cursor.execute(
                """
                UPDATE sales_automation.delivery_attempt
                SET status = 'unknown', error_code = 'LEASE_LOST_AFTER_SUBMIT',
                    error_message = 'Provider returned success after worker ownership was lost',
                    completed_at = now()
                WHERE id = %s AND status = 'started'
                """,
                (attempt_id,),
            )
            return False
        cursor.execute(
            """
            UPDATE sales_automation.delivery_attempt
            SET status = 'sent', provider_message_id = %s, provider_thread_id = %s,
                completed_at = now()
            WHERE id = %s
            """,
            (result.get("id"), result.get("threadId"), attempt_id),
        )
        return True


def complete_error(outbox_id, attempt_id, exc, attempt_no, worker_id):
    status, code, delay = classify_error(exc, attempt_no)
    message = str(exc)[:2000]
    with connect() as conn, conn.cursor() as cursor:
        if delay is None:
            cursor.execute(
                """
                UPDATE sales_automation.delivery_outbox
                SET status = %s, last_error_code = %s, last_error_message = %s,
                    worker_id = NULL, lease_token_hash = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL,
                    locked_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'sending' AND worker_id = %s
                """,
                (status, code, message, outbox_id, worker_id),
            )
        else:
            cursor.execute(
                """
                UPDATE sales_automation.delivery_outbox
                SET status = %s, last_error_code = %s, last_error_message = %s,
                    available_at = now() + (%s * interval '1 minute'),
                    worker_id = NULL, lease_token_hash = NULL,
                    lease_expires_at = NULL, heartbeat_at = NULL,
                    locked_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'sending' AND worker_id = %s
                """,
                (status, code, message, delay, outbox_id, worker_id),
            )
        if cursor.rowcount != 1:
            cursor.execute(
                """
                UPDATE sales_automation.delivery_attempt
                SET status = 'unknown', error_code = 'LEASE_LOST',
                    error_message = 'Worker ownership was lost before the error was recorded',
                    completed_at = now()
                WHERE id = %s AND status = 'started'
                """,
                (attempt_id,),
            )
            return "unknown", "LEASE_LOST"
        cursor.execute(
            """
            UPDATE sales_automation.delivery_attempt
            SET status = %s, error_code = %s, error_message = %s, completed_at = now()
            WHERE id = %s
            """,
            (status, code, message, attempt_id),
        )
    return status, code


def send_once():
    current_worker = worker_identity()
    claimed = claim_one(current_worker)
    if claimed is None:
        return None
    outbox_id, payload, expected_digest, attempt_no, attempt_id, worker_id = claimed
    try:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        actual_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual_digest != expected_digest:
            raise RuntimeError("Outbox payload hash mismatch")
        if not renew_lease(outbox_id, worker_id):
            return {"id": str(outbox_id), "status": "unknown", "error_code": "LEASE_LOST"}
        result = gmail_sender.send(payload)
    except Exception as exc:
        status, code = complete_error(outbox_id, attempt_id, exc, attempt_no, worker_id)
        return {"id": str(outbox_id), "status": status, "error_code": code}
    if not complete_success(outbox_id, attempt_id, result, worker_id):
        return {
            "id": str(outbox_id),
            "status": "unknown",
            "error_code": "LEASE_LOST_AFTER_SUBMIT",
        }
    return {"id": str(outbox_id), "status": "sent", "provider_result": result}


def dry_run_one():
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, payload FROM sales_automation.delivery_outbox
            WHERE channel = 'email' AND provider = 'email'
              AND status IN ('queued', 'retry_wait') AND available_at <= now()
            ORDER BY created_at LIMIT 1
            """
        )
        row = cursor.fetchone()
    if row is None:
        return None
    return {"id": str(row[0]), "prepared_payload": json.loads(gmail_sender.preview(row[1]))}
