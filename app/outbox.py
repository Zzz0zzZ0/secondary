import hashlib
import html
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .db import connect
from .secondary.message_policy import validation_errors
from .secondary.sender_identity import resolve_sender_identity
from .secondary_signals import notify_secondary_outbox_event
from .secondary.message_policy import message_route


EMAIL_PATTERN = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


def _body_html(body):
    return "".join(
        f"<p>{html.escape(paragraph.strip()).replace(chr(10), '<br>')}</p>"
        for paragraph in re.split(r"\n\s*\n", body)
        if paragraph.strip()
    )


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and EMAIL_PATTERN.fullmatch(value) is not None


def valid_linkedin(value):
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() for char in value):
        return False
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        hostname == "linkedin.com" or hostname.endswith(".linkedin.com")
    )


def _json(value):
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run: ./scripts/setup_python.sh") from exc
    return Jsonb(value)


def preflight():
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
              to_regclass('sales_automation.message_version')::text,
              to_regclass('sales_automation.message_approval')::text,
              to_regclass('sales_automation.delivery_outbox')::text,
              to_regclass('sales_automation.delivery_attempt')::text
            """
        )
        row = cursor.fetchone()
    names = (
        "message_version",
        "message_approval",
        "delivery_outbox",
        "delivery_attempt",
    )
    missing = [
        name for name, relation in zip(names, row or ()) if relation is None
    ]
    if row is None or len(row) != len(names):
        raise RuntimeError("Could not inspect the Outbox database schema")
    if missing:
        raise RuntimeError(
            f"Outbox database migrations are missing: {', '.join(missing)}"
        )
    return {"status": "ok", "tables": list(names)}


def import_run(run_dir: Path):
    run_dir = run_dir.resolve()
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"Missing run summary: {summary_path}")
    run_id = json.loads(summary_path.read_text(encoding="utf-8"))["run_id"]
    imported = skipped = 0

    with connect() as conn:
        with conn.cursor() as cursor:
            for review_path in sorted((run_dir / "records").glob("*/review.json")):
                record_dir = review_path.parent
                review = json.loads(review_path.read_text(encoding="utf-8"))
                crm = json.loads((record_dir / "input.json").read_text(encoding="utf-8"))
                output = review["original_output"]
                channel = output.get("output_type")
                contact = crm.get("contact") or {}
                recipient = contact.get("email") if channel == "email" else contact.get("linkedin_url")
                recipient_valid = (
                    valid_email(recipient)
                    if channel == "email"
                    else valid_linkedin(recipient) if channel == "linkedin" else False
                )
                if output.get("decision") != "generated" or not recipient_valid:
                    skipped += 1
                    continue
                message_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"twenty-hermes:{run_id}:{review['lead_id']}:{channel}:1",
                )
                cursor.execute(
                    """
                    INSERT INTO sales_automation.message_version
                      (id, run_id, lead_id, channel, version, recipient_original,
                       crm_snapshot, original_output)
                    VALUES (%s, %s, %s, %s, 1, %s, %s, %s)
                    ON CONFLICT (run_id, lead_id, channel, version) DO NOTHING
                    """,
                    (
                        message_id,
                        run_id,
                        review["lead_id"],
                        channel,
                        recipient,
                        _json(crm),
                        _json(output),
                    ),
                )
                imported += cursor.rowcount
    return imported, skipped


def list_messages(limit=20):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, lead_id, recipient_original, review_status, created_at
            FROM sales_automation.message_version
            ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        return cursor.fetchall()


def get_message(message_id):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, lead_id, recipient_original, crm_snapshot,
                   original_output, edited_output, review_status
            FROM sales_automation.message_version WHERE id = %s
            """,
            (message_id,),
        )
        return cursor.fetchone()


def _review_message(row):
    if row is None:
        return None
    effective_output = row[8] or row[7]
    crm_snapshot = dict(row[6] or {})
    crm_snapshot["message_route"] = message_route(crm_snapshot, None)
    return {
        "id": str(row[0]),
        "run_id": row[1],
        "lead_id": row[2],
        "channel": row[3],
        "version": row[4],
        "recipient_original": row[5],
        "crm_snapshot": crm_snapshot,
        "original_output": row[7],
        "edited_output": row[8],
        "effective_output": effective_output,
        "review_status": row[9],
        "created_at": row[10],
        "updated_at": row[11],
    }


def list_review_messages(limit=50, review_status="pending_review"):
    if review_status not in {"pending_review", "approved", "rejected"}:
        raise RuntimeError("Unsupported review status")
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, run_id, lead_id, channel, version, recipient_original,
                   crm_snapshot, original_output, edited_output, review_status,
                   created_at, updated_at
            FROM sales_automation.message_version
            WHERE review_status = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (review_status, limit),
        )
        return [_review_message(row) for row in cursor.fetchall()]


def get_review_message(message_id):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, run_id, lead_id, channel, version, recipient_original,
                   crm_snapshot, original_output, edited_output, review_status,
                   created_at, updated_at
            FROM sales_automation.message_version
            WHERE id = %s
            """,
            (message_id,),
        )
        return _review_message(cursor.fetchone())


def _pending_message(cursor, message_id):
    cursor.execute(
        """
        SELECT id, lead_id, recipient_original, crm_snapshot,
               original_output, edited_output, review_status, version, channel
        FROM sales_automation.message_version
        WHERE id = %s FOR UPDATE
        """,
        (message_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(f"Message version not found: {message_id}")
    if row[6] != "pending_review":
        raise RuntimeError(f"Message is already {row[6]}")
    return row


def save_message_edit(message_id, subject, body):
    with connect() as conn, conn.cursor() as cursor:
        row = _pending_message(cursor, message_id)
        channel = row[8]
        effective = json.loads(json.dumps(row[5] or row[4]))
        content = dict(effective.get("content") or {})
        if not isinstance(body, str) or not body.strip():
            raise RuntimeError("Edited message requires a non-empty body")
        if channel == "email":
            if not isinstance(subject, str) or not subject.strip():
                raise RuntimeError("Edited email requires a non-empty subject")
            if "\n" in subject or "\r" in subject:
                raise RuntimeError("Email subject must not contain line breaks")
            content["subject"] = subject.strip()
        elif channel == "linkedin":
            content["subject"] = None
        else:
            raise RuntimeError(f"Unsupported delivery channel: {channel}")
        content["body"] = body.strip()
        effective["content"] = content
        cursor.execute(
            """
            UPDATE sales_automation.message_version
            SET edited_output = %s, updated_at = now()
            WHERE id = %s
            """,
            (_json(effective), message_id),
        )
    return get_review_message(message_id)


def replace_message_draft(message_id, output):
    with connect() as conn, conn.cursor() as cursor:
        row = _pending_message(cursor, message_id)
        crm_snapshot = dict(row[3] or {})
        crm_snapshot["message_route"] = message_route(crm_snapshot, None)
        errors = validation_errors(output, crm_snapshot, str(row[1]))
        if errors:
            raise RuntimeError(
                "Regenerated message failed validation: " + "; ".join(errors)
            )
        if output.get("decision") != "generated":
            raise RuntimeError("Hermes did not return a generated message")
        if output.get("output_type") != row[8]:
            raise RuntimeError("Regenerated message channel changed")
        cursor.execute(
            """
            UPDATE sales_automation.message_version
            SET edited_output = %s, updated_at = now()
            WHERE id = %s
            """,
            (_json(output), message_id),
        )
    return get_review_message(message_id)


def approve_message(message_id, reviewer, subject=None, body=None, note=None):
    with connect() as conn:
        with conn.cursor() as cursor:
            row = _pending_message(cursor, message_id)
            original = row[4]
            channel = row[8]
            crm = row[3]
            sender_account_ref = None
            effective = json.loads(json.dumps(row[5] or original))
            content = dict(effective.get("content") or {})
            final_subject = subject if subject is not None else content.get("subject")
            final_body = body if body is not None else content.get("body")
            if not final_body:
                raise RuntimeError("Approved message requires a non-empty body")
            if channel == "email":
                if not final_subject:
                    raise RuntimeError("Approved email requires a non-empty subject")
                if "\n" in final_subject or "\r" in final_subject:
                    raise RuntimeError("Email subject must not contain line breaks")
                if not valid_email(row[2]):
                    raise RuntimeError("Recipient email is invalid")
                sender_identity = resolve_sender_identity(
                    (crm.get("sales") or {}).get("name")
                )
                if sender_identity is None or not valid_email(
                    sender_identity.get("account")
                ):
                    raise RuntimeError(
                        "Approved email requires a valid sender account mapping"
                    )
                sender_account_ref = f"email:{sender_identity['account']}"
            elif channel == "linkedin":
                final_subject = None
                if not valid_linkedin(row[2]):
                    raise RuntimeError("Recipient LinkedIn URL is invalid")
            else:
                raise RuntimeError(f"Unsupported delivery channel: {channel}")
            content["subject"] = final_subject
            content["body"] = final_body
            effective["content"] = content

            changed = effective != original
            cursor.execute(
                """
                UPDATE sales_automation.message_version
                SET edited_output = %s, review_status = 'approved', updated_at = now()
                WHERE id = %s
                """,
                (_json(effective) if changed else None, message_id),
            )
            approval_id = uuid.uuid4()
            cursor.execute(
                """
                INSERT INTO sales_automation.message_approval
                  (id, message_version_id, decision, approved_by, note)
                VALUES (%s, %s, 'approved', %s, %s)
                """,
                (approval_id, message_id, reviewer, note),
            )

            payload = {
                "lead_id": row[1],
                "to": row[2],
                "subject": final_subject,
                "body": final_body,
                "body_html": _body_html(final_body) if channel == "email" else None,
                "language": effective.get("language", "English"),
                "contact_name": (crm.get("contact") or {}).get("name"),
                "company_name": (crm.get("company") or {}).get("name"),
                "sales_name": (crm.get("sales") or {}).get("name"),
            }
            provider = channel
            canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            outbox_id = uuid.uuid4()
            idempotency_key = f"{provider}:{message_id}:v{row[7]}"
            cursor.execute(
                """
                INSERT INTO sales_automation.delivery_outbox
                  (id, message_version_id, channel, provider, recipient_original,
                   sender_account_ref, payload, payload_sha256, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    outbox_id,
                    message_id,
                    channel,
                    provider,
                    row[2],
                    sender_account_ref,
                    _json(payload),
                    digest,
                    idempotency_key,
                ),
            )
    notify_secondary_outbox_event(message_id, "approved")
    return outbox_id


def reject_message(message_id, reviewer, note=None):
    with connect() as conn, conn.cursor() as cursor:
        _pending_message(cursor, message_id)
        cursor.execute(
            """
            UPDATE sales_automation.message_version
            SET review_status = 'rejected', updated_at = now()
            WHERE id = %s
            """,
            (message_id,),
        )
        cursor.execute(
            """
            INSERT INTO sales_automation.message_approval
              (id, message_version_id, decision, approved_by, note)
            VALUES (%s, %s, 'rejected', %s, %s)
            """,
            (uuid.uuid4(), message_id, reviewer, note),
        )
    notify_secondary_outbox_event(message_id, "rejected")
    return get_review_message(message_id)


def reject_pending_messages_for_lead(lead_id, reviewer, note=None):
    """Reject every pending draft for a lead and emit one signal per draft."""
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.message_version
            SET review_status = 'rejected', updated_at = now()
            WHERE lead_id = %s AND review_status = 'pending_review'
            RETURNING id
            """,
            (lead_id,),
        )
        message_ids = [row[0] for row in cursor.fetchall()]
        for message_id in message_ids:
            cursor.execute(
                """
                INSERT INTO sales_automation.message_approval
                  (id, message_version_id, decision, approved_by, note)
                VALUES (%s, %s, 'rejected', %s, %s)
                """,
                (uuid.uuid4(), message_id, reviewer, note),
            )

    for message_id in message_ids:
        notify_secondary_outbox_event(message_id, "rejected")
    return message_ids


def list_outbox(limit=20):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, recipient_original, status, attempt_count, available_at, created_at
            FROM sales_automation.delivery_outbox
            ORDER BY created_at DESC LIMIT %s
            """,
            (limit,),
        )
        return cursor.fetchall()


def list_outbox_deliveries(limit=50):
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, message_version_id, channel, provider, recipient_original,
                   sender_account_ref, payload, status, attempt_count, max_attempts,
                   available_at, sent_at, last_error_code, last_error_message,
                   created_at, updated_at
            FROM sales_automation.delivery_outbox
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [
            {
                "id": str(row[0]),
                "message_version_id": str(row[1]),
                "channel": row[2],
                "provider": row[3],
                "recipient": row[4],
                "sender_account_ref": row[5],
                "payload": row[6],
                "status": row[7],
                "attempt_count": row[8],
                "max_attempts": row[9],
                "available_at": row[10],
                "sent_at": row[11],
                "last_error_code": row[12],
                "last_error_message": row[13],
                "created_at": row[14],
                "updated_at": row[15],
            }
            for row in cursor.fetchall()
        ]
