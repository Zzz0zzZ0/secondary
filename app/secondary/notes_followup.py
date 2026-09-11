"""Formal CRM Notes follow-up adapter owned by the secondary-lead poller.

The language decisions and draft generation remain in the existing Notes core.
This module only owns discovery, durable idempotency, retries, and queue routing.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.secondary.referrals import is_recommender
from notes_trial import notes_trial as notes_core


def find_existing_review(
    lead_id: str, latest_note_id: str, email_at: Optional[str]
) -> Optional[dict[str, str]]:
    """Find an already-visible review item for the same CRM Note."""
    from app.db import connect

    with connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id::text
            FROM sales_automation.message_version
            WHERE lead_id = %s AND review_status = 'pending_review'
              AND crm_snapshot->'source_version'->>'note_id' = %s
              AND crm_snapshot->'source_version'->>'email_at'
                    IS NOT DISTINCT FROM %s
              AND crm_snapshot->'review_context'->>'semantic_policy_version' = %s
              AND crm_snapshot->'review_context'->>'draft_policy_version' = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (
                lead_id,
                latest_note_id,
                email_at,
                notes_core.NOTES_SEMANTIC_POLICY_VERSION,
                notes_core.NOTES_DRAFT_POLICY_VERSION,
            ),
        )
        row = cursor.fetchone()
        if row:
            return {"type": "message", "id": str(row[0])}
        cursor.execute(
            """
            SELECT id::text
            FROM sales_automation.conversation_action
            WHERE lead_id = %s AND status = 'pending'
              AND crm_snapshot->'source_version'->>'note_id' = %s
              AND crm_snapshot->'source_version'->>'email_at'
                    IS NOT DISTINCT FROM %s
              AND crm_snapshot->'review_context'->>'semantic_policy_version' = %s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (
                lead_id,
                latest_note_id,
                email_at,
                notes_core.NOTES_SEMANTIC_POLICY_VERSION,
            ),
        )
        row = cursor.fetchone()
        return {"type": "action", "id": str(row[0])} if row else None


def retire_stale_reviews(
    lead_id: str,
    latest_note_id: str,
    email_at: Optional[str],
) -> list[str]:
    """Retire only drafts that were not generated from the current CRM Note."""
    from app.conversation_actions import dismiss_pending_actions_for_lead
    from app.outbox import reject_pending_messages_for_lead

    current = find_existing_review(lead_id, latest_note_id, email_at)
    keep_message = current is not None and current["type"] == "message"
    keep_action = current is not None and current["type"] == "action"
    message_ids = [
        str(message_id)
        for message_id in reject_pending_messages_for_lead(
            lead_id,
            "notes-poller",
            "Superseded because current CRM Notes own this conversation.",
            keep_note_id=latest_note_id if keep_message else None,
            keep_email_at=email_at if keep_message else None,
            signal_event="superseded",
        )
    ]
    action_ids = dismiss_pending_actions_for_lead(
        lead_id,
        "notes-poller",
        "Superseded because a newer CRM Note owns this conversation.",
        keep_note_id=latest_note_id if keep_action else None,
        keep_email_at=email_at if keep_action else None,
    )
    return message_ids + action_ids


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validated_int(
    environment: dict[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


class NotesFollowUpProcessor:
    """Discover and process current Notes conversations exactly once per version."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        environment: Optional[dict[str, str]] = None,
        now: Optional[Callable[[], datetime]] = None,
        read_records: Optional[Callable[[int, Optional[str]], list[dict[str, Any]]]] = None,
        analyze: Optional[
            Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
        ] = None,
        publish: Optional[
            Callable[
                [dict[str, Any], dict[str, Any], Optional[dict[str, Any]]],
                dict[str, Any],
            ]
        ] = None,
        find_existing: Optional[
            Callable[[str, str, Optional[str]], Optional[dict[str, str]]]
        ] = None,
        retire_stale: Optional[
            Callable[[str, str, Optional[str]], list[str]]
        ] = None,
        sync_message_review: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.connect = connect
        self.environment = dict(environment or {})
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.read_records = read_records or notes_core.read_records
        self.analyze = analyze or notes_core.safe_analyze
        self.publish = publish or notes_core.publish_review
        self.find_existing = find_existing or find_existing_review
        self.retire_stale = retire_stale or retire_stale_reviews
        self.sync_message_review = sync_message_review or (
            lambda lead_id, message_id: None
        )

    @staticmethod
    def _source_note(
        record: dict[str, Any], state: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        latest = state.get("latest")
        if isinstance(latest, dict):
            return latest
        notes = record.get("notes") or []
        if not notes:
            return None
        return max(
            notes,
            key=lambda note: (
                str(note.get("created_at") or ""),
                str(note.get("note_id") or ""),
            ),
        )

    @staticmethod
    def _manual_review_analysis(
        record: dict[str, Any], state: dict[str, Any]
    ) -> dict[str, Any]:
        note = NotesFollowUpProcessor._source_note(record, state)
        candidates = notes_core.evidence_candidates(note) if note else []
        semantic: dict[str, Any] = {
            "contact_permission": "uncertain",
            "decision": "manual_review",
            "confidence": 0.0,
            "reason": str(state.get("reason") or "Notes 结构无法可靠判断"),
            "review_required": True,
        }
        if note:
            semantic.update(
                {
                    "lead_id": record["lead_id"],
                    "latest_note_id": note["note_id"],
                    "resume_at": None,
                }
            )
        if candidates:
            semantic["evidence_index"] = 0
            semantic["evidence_quote"] = candidates[0]["text"]
        return {"semantic": semantic, "draft": None}

    def _discover(
        self,
        record: dict[str, Any],
        state: dict[str, Any],
        baseline: bool = False,
    ) -> Optional[str]:
        source_note = self._source_note(record, state)
        if source_note is None or not source_note.get("note_id"):
            return None
        note_id = str(source_note["note_id"])
        lead_id = str(record["lead_id"])
        source_created_at = str(
            source_note.get("created_at") or source_note.get("email_at") or ""
        )
        semantic_record = dict(record)
        semantic_record.pop("sales_name", None)
        semantic_record.pop("lead_source", None)  # Routing metadata does not change the customer message.
        source_hash = hashlib.sha256(
            _canonical(
                {
                    "record": semantic_record,
                    "semantic_policy": notes_core.NOTES_SEMANTIC_POLICY_VERSION,
                    "draft_policy": notes_core.NOTES_DRAFT_POLICY_VERSION,
                }
            ).encode("utf-8")
        ).hexdigest()
        legacy_source_hash = hashlib.sha256(
            _canonical(
                {
                    "record": record,
                    "semantic_policy": notes_core.NOTES_SEMANTIC_POLICY_VERSION,
                    "draft_policy": notes_core.NOTES_DRAFT_POLICY_VERSION,
                }
            ).encode("utf-8")
        ).hexdigest()
        previous_draft_hashes = {
            hashlib.sha256(
                _canonical(
                    {
                        "record": candidate_record,
                        "semantic_policy": notes_core.NOTES_SEMANTIC_POLICY_VERSION,
                        "draft_policy": "notes-draft-v1",
                    }
                ).encode("utf-8")
            ).hexdigest()
            for candidate_record in (semantic_record, record)
        }
        initial_status = "skipped" if state["state"] == "waiting_customer" else (
            "baseline" if baseline else "pending"
        )
        now_text = _iso(self.now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notes_follow_up_state
                SET status = 'superseded', updated_at = ?
                WHERE lead_id = ? AND latest_note_id != ?
                  AND status != 'superseded'
                """,
                (now_text, lead_id, note_id),
            )
            existing = connection.execute(
                "SELECT source_hash FROM notes_follow_up_state WHERE latest_note_id = ?",
                (note_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO notes_follow_up_state
                      (latest_note_id, lead_id, source_hash, source_created_at,
                       crm_snapshot_json, structural_state, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        note_id,
                        lead_id,
                        source_hash,
                        source_created_at,
                        _canonical(record),
                        state["state"],
                        initial_status,
                        now_text,
                        now_text,
                    ),
                )
            elif existing["source_hash"] not in {
                source_hash,
                legacy_source_hash,
                *previous_draft_hashes,
            }:
                connection.execute(
                    """
                    UPDATE notes_follow_up_state
                    SET source_hash = ?, source_created_at = ?,
                        crm_snapshot_json = ?, structural_state = ?, status = ?,
                        decision = NULL, publication_type = NULL,
                        publication_id = NULL, attempts = 0,
                        next_attempt_at = NULL, analysis_json = NULL,
                        last_error = NULL, updated_at = ?
                    WHERE latest_note_id = ?
                    """,
                    (
                        source_hash,
                        source_created_at,
                        _canonical(record),
                        state["state"],
                        initial_status,
                        now_text,
                        note_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE notes_follow_up_state
                    SET source_hash = ?, source_created_at = ?,
                        crm_snapshot_json = ?, structural_state = ?,
                        updated_at = ?
                    WHERE latest_note_id = ?
                    """,
                    (
                        source_hash,
                        source_created_at,
                        _canonical(record),
                        state["state"],
                        now_text,
                        note_id,
                    ),
                )
                if baseline:
                    connection.execute(
                        """
                        UPDATE notes_follow_up_state
                        SET status = 'baseline', next_attempt_at = NULL,
                            last_error = NULL, updated_at = ?
                        WHERE latest_note_id = ?
                          AND status IN ('pending', 'retry_wait', 'failed')
                        """,
                        (now_text, note_id),
                    )
        return note_id

    def _due_rows(self, note_ids: list[str], limit: int) -> list[sqlite3.Row]:
        if not note_ids:
            return []
        placeholders = ",".join("?" for _ in note_ids)
        with self.connect() as connection:
            return connection.execute(
                f"""
                SELECT * FROM notes_follow_up_state
                WHERE latest_note_id IN ({placeholders})
                  AND (
                    status = 'pending'
                    OR (status = 'retry_wait' AND next_attempt_at <= ?)
                  )
                ORDER BY source_created_at DESC, latest_note_id
                LIMIT ?
                """,
                (*note_ids, _iso(self.now()), limit),
            ).fetchall()

    def _process_one(
        self,
        row: sqlite3.Row,
        record: dict[str, Any],
        state: dict[str, Any],
    ) -> tuple[str, Optional[str], bool, bool]:
        note_id = row["latest_note_id"]
        now_text = _iso(self.now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE notes_follow_up_state
                SET status = 'processing', attempts = attempts + 1,
                    next_attempt_at = NULL, updated_at = ?
                WHERE latest_note_id = ? AND status IN ('pending', 'retry_wait')
                """,
                (now_text, note_id),
            ).rowcount
        if not changed:
            return "unchanged", None, False, False
        hermes_called = False
        try:
            with self.connect() as connection:
                classified = connection.execute(
                    "SELECT c.result_json FROM secondary_lead_state s JOIN secondary_lead_classification c ON c.lead_id=s.lead_id AND c.source_hash=s.source_hash WHERE s.lead_id=?",
                    (str(record["lead_id"]),),
                ).fetchone()
            classification = json.loads(classified["result_json"]) if classified else None
            if is_recommender(record, classification):
                analysis = {"semantic": {"decision": "no_action", "contact_permission": "allowed", "reason": "推荐人不发信，使用被推荐人独立队列并核对其 Notes"}, "draft": None}
                with self.connect() as connection:
                    connection.execute("UPDATE notes_follow_up_state SET status='completed', decision='no_action', analysis_json=?, publication_type=NULL, publication_id=NULL, next_attempt_at=NULL, last_error=NULL WHERE latest_note_id=?", (_canonical(analysis), note_id))
                return "no_action", None, False, False
            source_note = self._source_note(record, state)
            expected_email_at = (
                str(source_note.get("email_at"))
                if isinstance(source_note, dict) and source_note.get("email_at")
                else None
            )
            existing = self.find_existing(
                str(record["lead_id"]), str(note_id), expected_email_at
            )
            if existing:
                with self.connect() as connection:
                    connection.execute(
                        """
                        UPDATE notes_follow_up_state
                        SET status = 'completed', decision = 'existing_review',
                            publication_type = ?, publication_id = ?,
                            last_error = NULL, updated_at = ?
                        WHERE latest_note_id = ? AND status = 'processing'
                        """,
                        (
                            existing["type"],
                            existing["id"],
                            _iso(self.now()),
                            note_id,
                        ),
                    )
                if existing["type"] == "message":
                    self.sync_message_review(
                        str(record["lead_id"]), existing["id"]
                    )
                return "existing_review", existing["type"], False, False
            channel = notes_core.outbound_channel(record, source_note)
            if channel == "linkedin":
                contact_valid = notes_core._valid_linkedin(
                    record.get("contact_linkedin_url")
                )
                channel_label = "LinkedIn地址"
            else:
                contact_valid = notes_core._valid_email(record.get("contact_email"))
                channel_label = "邮箱"
            if state["state"] == "manual_review":
                analysis = self._manual_review_analysis(record, state)
            elif not contact_valid:
                analysis = self._manual_review_analysis(
                    record,
                    {
                        **state,
                        "reason": f"联系人{channel_label}不可用，无法生成客户回复",
                    },
                )
            else:
                hermes_called = True
                analysis = self.analyze(record, state)
            semantic = analysis.get("semantic") if isinstance(analysis, dict) else None
            decision = semantic.get("decision") if isinstance(semantic, dict) else None
            if not isinstance(decision, str):
                raise RuntimeError("Notes analysis did not return a decision")
            publication = (
                {"status": "not_published"}
                if decision == "no_action"
                else self.publish(record, state, analysis)
            )
            publication_type = None
            publication_id = None
            if publication.get("message_version_id"):
                publication_type = "message"
                publication_id = str(publication["message_version_id"])
            elif publication.get("action_id"):
                publication_type = "action"
                publication_id = str(publication["action_id"])
            elif decision != "no_action":
                raise RuntimeError("Notes decision did not enter a review queue")
            if publication_type == "message" and publication_id is not None:
                self.sync_message_review(
                    str(record["lead_id"]), publication_id
                )
            with self.connect() as connection:
                connection.execute(
                    """
                    UPDATE notes_follow_up_state
                    SET status = 'completed', decision = ?,
                        publication_type = ?, publication_id = ?,
                        analysis_json = ?, last_error = NULL, updated_at = ?
                    WHERE latest_note_id = ? AND status = 'processing'
                    """,
                    (
                        decision,
                        publication_type,
                        publication_id,
                        _canonical(analysis),
                        _iso(self.now()),
                        note_id,
                    ),
                )
            return (
                decision,
                publication_type,
                publication_type is not None,
                hermes_called,
            )
        except Exception as exc:
            max_attempts = _validated_int(
                self.environment,
                "HERMES_NOTES_MAX_ATTEMPTS",
                3,
                1,
                10,
            )
            attempts = int(row["attempts"] or 0) + 1
            status = "failed" if attempts >= max_attempts else "retry_wait"
            retry_at = (
                None
                if status == "failed"
                else _iso(self.now() + timedelta(minutes=20))
            )
            with self.connect() as connection:
                connection.execute(
                    """
                    UPDATE notes_follow_up_state
                    SET status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                    WHERE latest_note_id = ? AND status = 'processing'
                    """,
                    (status, retry_at, str(exc)[-2000:], _iso(self.now()), note_id),
                )
            return status, None, False, hermes_called

    def run(self, limit: Optional[int] = None) -> dict[str, Any]:
        scan_limit = _validated_int(
            self.environment,
            "HERMES_NOTES_SCAN_LIMIT",
            500,
            1,
            500,
        )
        batch_limit = limit or _validated_int(
            self.environment,
            "HERMES_NOTES_BATCH_SIZE",
            3,
            1,
            20,
        )
        if not 1 <= batch_limit <= 20:
            raise RuntimeError("Notes batch limit must be between 1 and 20")
        process_existing = self.environment.get(
            "HERMES_NOTES_PROCESS_EXISTING", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        with self.connect() as connection:
            bootstrap_row = connection.execute(
                "SELECT value FROM scheduler_state WHERE key = 'notes_bootstrap_completed'"
            ).fetchone()
        bootstrap = bootstrap_row is None and not process_existing
        records = self.read_records(scan_limit, None)
        current: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        skipped = 0
        retired_stale_reviews = 0
        adopted_existing = 0
        for record in records:
            state = notes_core.structural_state(record.get("notes") or [])
            unchanged_waiting = False
            if state["state"] in {"waiting_customer", "needs_analysis"}:
                with self.connect() as connection:
                    previous = connection.execute(
                        "SELECT crm_snapshot_json FROM notes_follow_up_state "
                        "WHERE lead_id = ? AND status = 'skipped'",
                        (str(record["lead_id"]),),
                    ).fetchone()
                if previous is not None:
                    old_record = json.loads(previous["crm_snapshot_json"])
                    current_record = dict(record)
                    for key in ("sales_name", "lead_source"):
                        old_record.pop(key, None)
                        current_record.pop(key, None)
                    unchanged_waiting = old_record == current_record
            source_note = self._source_note(record, state) or {}
            if not source_note.get("note_id"):
                continue
            note_id = str(source_note["note_id"])
            retired_stale_reviews += 0 if unchanged_waiting else len(
                self.retire_stale(
                    str(record["lead_id"]),
                    note_id,
                    (
                        str(source_note["email_at"])
                        if source_note.get("email_at")
                        else None
                    ),
                )
            )
            # Persist this version only after retirement succeeds, so failures retry.
            self._discover(record, state, baseline=bootstrap)
            current[note_id] = (record, state)
            expected_email_at = (
                str(source_note["email_at"])
                if source_note.get("email_at")
                else None
            )
            existing = self.find_existing(
                str(record["lead_id"]), note_id, expected_email_at
            )
            with self.connect() as connection:
                stored = connection.execute(
                    """
                    SELECT status, decision, publication_type, publication_id
                    FROM notes_follow_up_state
                    WHERE latest_note_id = ?
                    """,
                    (note_id,),
                ).fetchone()
            if existing is not None:
                already_adopted = (
                    stored is not None
                    and stored["status"] == "completed"
                    and stored["decision"] == "existing_review"
                    and stored["publication_type"] == existing["type"]
                    and stored["publication_id"] == existing["id"]
                )
                if not already_adopted:
                    with self.connect() as connection:
                        connection.execute(
                            """
                            UPDATE notes_follow_up_state
                            SET status = 'completed', decision = 'existing_review',
                                publication_type = ?, publication_id = ?,
                                next_attempt_at = NULL, last_error = NULL,
                                updated_at = ?
                            WHERE latest_note_id = ?
                            """,
                            (
                                existing["type"],
                                existing["id"],
                                _iso(self.now()),
                                note_id,
                            ),
                        )
                    adopted_existing += 1
                if existing["type"] == "message":
                    self.sync_message_review(
                        str(record["lead_id"]), existing["id"]
                    )
            if state["state"] == "waiting_customer":
                skipped += 1
        superseded_absent = 0
        if len(records) < scan_limit:
            current_note_ids = set(current)
            with self.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT latest_note_id
                    FROM notes_follow_up_state
                    WHERE status IN ('pending', 'processing', 'retry_wait')
                    """
                ).fetchall()
                absent = [
                    row["latest_note_id"]
                    for row in rows
                    if row["latest_note_id"] not in current_note_ids
                ]
                if absent:
                    placeholders = ",".join("?" for _ in absent)
                    superseded_absent = connection.execute(
                        f"""
                        UPDATE notes_follow_up_state
                        SET status = 'superseded', next_attempt_at = NULL,
                            last_error = NULL, updated_at = ?
                        WHERE latest_note_id IN ({placeholders})
                          AND status IN ('pending', 'processing', 'retry_wait')
                        """,
                        (_iso(self.now()), *absent),
                    ).rowcount
        if bootstrap_row is None:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO scheduler_state (key, value)
                    VALUES ('notes_bootstrap_completed', ?)
                    ON CONFLICT (key) DO UPDATE SET value = excluded.value
                    """,
                    (_iso(self.now()),),
                )
        outcomes: dict[str, int] = {
            "processed": 0,
            "messages": 0,
            "actions": 0,
            "no_action": 0,
            "retry_wait": 0,
            "failed": 0,
            "adopted": adopted_existing,
            "hermes_calls": 0,
        }
        for row in self._due_rows(list(current), batch_limit):
            record, state = current[row["latest_note_id"]]
            decision, publication_type, _created, hermes_called = self._process_one(
                row, record, state
            )
            outcomes["processed"] += 1
            if hermes_called:
                outcomes["hermes_calls"] += 1
            if decision == "existing_review":
                outcomes["adopted"] += 1
            elif publication_type == "message":
                outcomes["messages"] += 1
            elif publication_type == "action":
                outcomes["actions"] += 1
            elif decision in outcomes:
                outcomes[decision] += 1
        return {
            "status": "ok",
            "seen": len(records),
            "bootstrap": bootstrap,
            "baselined": sum(
                1
                for _record, state in current.values()
                if bootstrap and state["state"] != "waiting_customer"
            ),
            "waiting_customer": skipped,
            "retired_stale_reviews": retired_stale_reviews,
            "superseded_absent": superseded_absent,
            "analyzed": outcomes["hermes_calls"],
            "published": outcomes["messages"] + outcomes["actions"],
            **outcomes,
        }
