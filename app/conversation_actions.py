import json
import uuid

from .db import connect


ACTION_TYPES = {"internal_task", "manual_review"}
ACTION_STATUSES = {"pending", "resolved", "dismissed"}


def _json(value):
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise RuntimeError("Missing dependency. Run: ./scripts/setup_python.sh") from exc
    return Jsonb(value)


def _row(row):
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "run_id": row[1],
        "lead_id": row[2],
        "action_type": row[3],
        "crm_snapshot": row[4],
        "analysis": row[5],
        "status": row[6],
        "handled_by": row[7],
        "resolution_note": row[8],
        "created_at": row[9],
        "updated_at": row[10],
    }


def create_review_action(run_id, lead_id, action_type, crm_snapshot, analysis):
    """Create or refresh one pending non-delivery action for a Notes result."""
    if action_type not in ACTION_TYPES:
        raise RuntimeError("Only internal_task and manual_review can enter action review")
    if not all(isinstance(value, dict) for value in (crm_snapshot, analysis)):
        raise RuntimeError("CRM snapshot and analysis must be objects")
    crm_snapshot = json.loads(json.dumps(crm_snapshot))
    analysis = json.loads(json.dumps(analysis))
    action_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"twenty-hermes:conversation-action:{run_id}:{lead_id}",
    )
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO sales_automation.conversation_action
              (id, run_id, lead_id, action_type, crm_snapshot, analysis)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, lead_id) DO UPDATE
            SET action_type = EXCLUDED.action_type,
                crm_snapshot = EXCLUDED.crm_snapshot,
                analysis = EXCLUDED.analysis,
                updated_at = now()
            WHERE sales_automation.conversation_action.status = 'pending'
            """,
            (
                action_id,
                str(run_id),
                str(lead_id),
                action_type,
                _json(crm_snapshot),
                _json(analysis),
            ),
        )
        changed = cursor.rowcount > 0
    return {
        "action_id": str(action_id),
        "lead_id": str(lead_id),
        "status": "pending" if changed else "unchanged",
        "created": changed,
    }


def list_review_actions(limit=50, status="pending"):
    if status not in ACTION_STATUSES:
        raise RuntimeError("Unsupported action status")
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, run_id, lead_id, action_type, crm_snapshot, analysis,
                   status, handled_by, resolution_note, created_at, updated_at
            FROM sales_automation.conversation_action
            WHERE status = %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (status, limit),
        )
        return [_row(row) for row in cursor.fetchall()]


def resolve_review_action(action_id, status, reviewer, note=None):
    if status not in {"resolved", "dismissed"}:
        raise RuntimeError("Action decision must be resolved or dismissed")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise RuntimeError("Action reviewer is required")
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.conversation_action
            SET status = %s, handled_by = %s, resolution_note = %s,
                updated_at = now()
            WHERE id = %s AND status = 'pending'
            RETURNING id, run_id, lead_id, action_type, crm_snapshot, analysis,
                      status, handled_by, resolution_note, created_at, updated_at
            """,
            (status, reviewer.strip(), note, action_id),
        )
        record = _row(cursor.fetchone())
    if record is None:
        raise RuntimeError("Conversation action was not found or is already handled")
    return record


def dismiss_pending_actions_for_lead(
    lead_id,
    reviewer,
    note=None,
    keep_note_id=None,
    keep_email_at=None,
):
    """Dismiss pending actions except the one grounded in the current CRM Note."""
    with connect() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE sales_automation.conversation_action
            SET status = 'dismissed', handled_by = %s, resolution_note = %s,
                updated_at = now()
            WHERE lead_id = %s AND status = 'pending'
              AND (
                %s::text IS NULL
                OR crm_snapshot->'source_version'->>'note_id'
                     IS DISTINCT FROM %s::text
                OR crm_snapshot->'source_version'->>'email_at'
                     IS DISTINCT FROM %s::text
              )
            RETURNING id
            """,
            (
                reviewer,
                note,
                str(lead_id),
                keep_note_id,
                keep_note_id,
                keep_email_at,
            ),
        )
        return [str(row[0]) for row in cursor.fetchall()]
