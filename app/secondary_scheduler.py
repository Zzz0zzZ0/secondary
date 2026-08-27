import argparse
import copy
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .outbox import (
    preflight as outbox_preflight,
    reject_pending_messages_for_lead,
    valid_email,
    valid_linkedin,
)
from .conversation_actions import dismiss_pending_actions_for_lead
from .secondary.classification_runner import ClassificationRunner
from .secondary.domain import (
    LEAD_TYPES,
    LEAD_TYPE_LABELS,
    MESSAGE_SUBTYPES,
    QUEUE_STATUSES,
    SHORT_CYCLE_TYPES,
    interval_days,
)
from .secondary.message_policy import prepare_message_record
from .secondary.notes_followup import NotesFollowUpProcessor
from .secondary.schema import initialize_schema
from .message_jobs import (
    DEFAULT_PIPELINE_SCRIPT,
    DEFAULT_STATE_DIR,
    PROJECT_DIR,
    MessageJobProcessor,
    isoformat,
    parse_timestamp,
    utc_now,
)


DEFAULT_CLASSIFICATION_SKILL_DIR = (
    PROJECT_DIR / "skill" / "classify-secondary-lead"
)
DEFAULT_EXPORT_SCRIPT = PROJECT_DIR / "scripts" / "twenty_export_hermes_inputs.sh"
DEFAULT_DB_CHECK_SCRIPT = PROJECT_DIR / "scripts" / "twenty_db_check.sh"
CLASSIFICATION_POLICY_VERSION = "secondary-lead-v7"
SALES_FOLLOW_UP_TIME_UNVERIFIED = "SALES_FOLLOW_UP_TIME_UNVERIFIED"
MESSAGE_JOB_STALE_AFTER = timedelta(minutes=30)


def _needs_policy_reclassification(row: sqlite3.Row) -> bool:
    return row["policy_version"] != CLASSIFICATION_POLICY_VERSION


def _sales_follow_up_status(classification: Optional[Dict[str, Any]]) -> str:
    context = (
        classification.get("sales_follow_up_context")
        if isinstance(classification, dict)
        else None
    )
    status = context.get("status") if isinstance(context, dict) else None
    return status if status in {"sales_replied", "information_sent"} else "none"


def _sales_follow_up_due_at(
    record: Dict[str, Any],
    days: int,
) -> Optional[str]:
    source_version = record.get("source_version") or {}
    if source_version.get("activity_at_source") != "lastFollowUp":
        return None
    activity_at = source_version.get("activity_at")
    if not activity_at:
        return None
    return isoformat(parse_timestamp(str(activity_at)) + timedelta(days=days))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _business_snapshot(record: Dict[str, Any]) -> Dict[str, Any]:
    snapshot = copy.deepcopy(record)
    source_version = snapshot.get("source_version")
    if isinstance(source_version, dict):
        source_version.pop("updated_at", None)
    lead = snapshot.get("lead")
    if isinstance(lead, dict):
        lead.pop("updated_at", None)
    return snapshot


def _source_hash(record: Dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_json(_business_snapshot(record)).encode("utf-8")
    ).hexdigest()


def _validated_int(
    environment: Dict[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(environment.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


class SecondaryLeadScheduler:
    def __init__(
        self,
        state_dir: Path = DEFAULT_STATE_DIR,
        export_script: Path = DEFAULT_EXPORT_SCRIPT,
        pipeline_script: Path = DEFAULT_PIPELINE_SCRIPT,
        classification_skill_dir: Path = DEFAULT_CLASSIFICATION_SKILL_DIR,
        environment: Optional[Dict[str, str]] = None,
        now: Callable[[], datetime] = utc_now,
    ):
        self.environment = dict(environment if environment is not None else os.environ)
        self.now = now
        self.state_dir = state_dir.resolve()
        self.export_script = export_script.resolve()
        self.classification_skill_dir = classification_skill_dir.resolve()
        message_environment = self.environment.copy()
        message_environment["HERMES_POLL_OUTBOX_AUTO_IMPORT"] = "true"
        self.message_processor = MessageJobProcessor(
            state_dir=self.state_dir,
            pipeline_script=pipeline_script,
            environment=message_environment,
            now=now,
        )
        self.db_path = self.message_processor.db_path
        self.classification_dir = self.state_dir / "classifications"
        self.classification_dir.mkdir(parents=True, exist_ok=True)
        self.classification_dir.chmod(0o700)
        self.classification_runner = ClassificationRunner(
            self.classification_skill_dir,
            PROJECT_DIR,
            self.environment,
        )
        self.control_socket_path = self.state_dir / "secondary-scheduler.sock"
        self._initialize_database()
        self.notes_processor = NotesFollowUpProcessor(
            connect=self._connect,
            environment=self.environment,
            now=self.now,
            sync_message_review=self._bind_notes_review,
        )

    def maintain(self) -> Dict[str, int]:
        return {
            "settling_dates_repaired": self._repair_settling_due_dates(),
            "classifications_requeued": (
                self._requeue_outdated_review_classifications()
            ),
            "message_jobs_recovered": self._recover_interrupted_message_jobs(),
        }

    def review_configuration(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "mode": "manual",
            "auto_reviewer": None,
        }

    def _notify_scheduler(self) -> bool:
        if not self.control_socket_path.exists():
            return False
        control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            control.sendto(b"wakeup", str(self.control_socket_path))
            return True
        except OSError:
            return False
        finally:
            control.close()

    def _connect(self) -> sqlite3.Connection:
        return self.message_processor.connect()

    def _bind_notes_review(self, lead_id: str, message_version_id: str) -> None:
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE secondary_lead_state
                SET status = 'waiting_review', next_action_at = NULL,
                    latest_message_version_id = ?, last_error = NULL,
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (message_version_id, now_text, lead_id),
            ).rowcount
            if changed:
                self._event(
                    connection,
                    lead_id,
                    "notes_review_bound",
                    {"message_version_id": message_version_id},
                )

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            initialize_schema(connection)

    def _state(
        self,
        connection: sqlite3.Connection,
        key: str,
    ) -> Optional[str]:
        row = connection.execute(
            "SELECT value FROM scheduler_state WHERE key = ?",
            (key,),
        ).fetchone()
        return row["value"] if row else None

    def _set_state(
        self,
        connection: sqlite3.Connection,
        key: str,
        value: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO scheduler_state (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _event(
        self,
        connection: sqlite3.Connection,
        lead_id: str,
        event_type: str,
        details: Optional[Dict[str, Any]] = None,
        event_at: Optional[str] = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO secondary_lead_event
              (lead_id, event_type, event_at, details_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                lead_id,
                event_type,
                event_at or isoformat(self.now()),
                _canonical_json(details or {}),
            ),
        )

    def _scan_environment(
        self,
        cursor_id: str,
        limit: int,
        record_id: str = "",
    ) -> Dict[str, str]:
        environment = self.environment.copy()
        environment.update(
            {
                "TWENTY_EXPORT_LIMIT": str(limit),
                "TWENTY_AFTER_ID": cursor_id,
                "TWENTY_RECORD_ID": record_id,
                "OUTBOX_AUTO_IMPORT": "false",
            }
        )
        return environment

    def _export_batch(
        self,
        cursor_id: str,
        limit: int,
        record_id: str = "",
    ) -> List[Dict[str, Any]]:
        if not self.export_script.is_file():
            raise RuntimeError(f"CRM export script is missing: {self.export_script}")
        with tempfile.NamedTemporaryFile(
            prefix="secondary-scan-",
            suffix=".json",
            dir=self.state_dir,
            delete=False,
        ) as temporary:
            output_path = Path(temporary.name)
        try:
            result = subprocess.run(
                [str(self.export_script), str(output_path)],
                cwd=PROJECT_DIR,
                env=self._scan_environment(
                    cursor_id,
                    limit,
                    record_id,
                ),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(f"Secondary CRM scan failed: {detail[-2000:]}")
            records = json.loads(output_path.read_text(encoding="utf-8"))
        finally:
            output_path.unlink(missing_ok=True)
        if not isinstance(records, list) or any(
            not isinstance(record, dict) for record in records
        ):
            raise RuntimeError("Secondary CRM scan must return an array of objects")
        if len(records) > limit:
            raise RuntimeError("Secondary CRM scan returned more than its batch limit")
        return records

    def _record_identity(
        self,
        record: Dict[str, Any],
    ) -> tuple[str, str, str, str]:
        lead_id = str((record.get("lead") or {}).get("id") or "")
        source_version = record.get("source_version") or {}
        source_updated_at = str(source_version.get("updated_at") or "")
        source_record_id = str(source_version.get("record_id") or lead_id)
        if not lead_id or not source_updated_at or not source_record_id:
            raise RuntimeError("Secondary CRM record is missing lead.id or source_version")
        parse_timestamp(source_updated_at)
        return lead_id, source_updated_at, source_record_id, _source_hash(record)

    def _activity_at(self, record: Dict[str, Any]) -> str:
        source_version = record.get("source_version") or {}
        lead = record.get("lead") or {}
        activity_at = (
            source_version.get("activity_at")
            or lead.get("last_follow_up_at")
            or lead.get("created_at")
            or source_version.get("updated_at")
        )
        if not activity_at:
            raise RuntimeError("Secondary CRM record is missing an activity timestamp")
        parse_timestamp(str(activity_at))
        return str(activity_at)

    def _classification_due_at(self, record: Dict[str, Any]) -> str:
        settle_hours = _validated_int(
            self.environment,
            "HERMES_SECONDARY_SETTLE_HOURS",
            48,
            1,
            720,
        )
        jitter_hours = _validated_int(
            self.environment,
            "HERMES_SECONDARY_SETTLE_JITTER_HOURS",
            24,
            0,
            168,
        )
        due = parse_timestamp(self._activity_at(record)) + timedelta(
            hours=settle_hours
        )
        jitter_minutes = jitter_hours * 60
        if jitter_minutes:
            lead_id = str((record.get("lead") or {}).get("id") or "")
            seed = hashlib.sha256(
                f"{lead_id}:{_source_hash(record)}:settling".encode("utf-8")
            ).hexdigest()
            due += timedelta(minutes=int(seed[:8], 16) % jitter_minutes)
        return isoformat(due)

    def _repair_settling_due_dates(self) -> int:
        settle_hours = _validated_int(
            self.environment,
            "HERMES_SECONDARY_SETTLE_HOURS",
            48,
            1,
            720,
        )
        jitter_hours = _validated_int(
            self.environment,
            "HERMES_SECONDARY_SETTLE_JITTER_HOURS",
            24,
            0,
            168,
        )
        policy = f"activity-plus-settle-jitter-v3:{settle_hours}:{jitter_hours}"
        repaired = 0
        with self._connect() as connection:
            if self._state(connection, "classification_due_policy") == policy:
                return repaired
            rows = connection.execute(
                """
                SELECT lead_id, crm_snapshot_json, classification_due_at
                FROM secondary_lead_state
                WHERE status = 'settling'
                """
            ).fetchall()
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                try:
                    record = json.loads(row["crm_snapshot_json"])
                    due_at = self._classification_due_at(record)
                except (json.JSONDecodeError, RuntimeError, TypeError, ValueError):
                    continue
                if due_at == row["classification_due_at"]:
                    continue
                cursor = connection.execute(
                    """
                    UPDATE secondary_lead_state
                    SET classification_due_at = ?, updated_at = ?
                    WHERE lead_id = ? AND status = 'settling'
                    """,
                    (due_at, isoformat(self.now()), row["lead_id"]),
                )
                repaired += max(cursor.rowcount, 0)
            self._set_state(
                connection,
                "classification_due_policy",
                policy,
            )
        return repaired

    def _requeue_outdated_review_classifications(self) -> int:
        now_text = isoformat(self.now())
        requeued = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT lead_id, lead_type, policy_version, crm_snapshot_json
                FROM secondary_lead_state
                WHERE status = 'scheduled'
                  AND next_action_at IS NOT NULL
                  AND next_action_at <= ?
                  AND last_classified_at IS NOT NULL
                  AND last_generated_at IS NULL
                  AND latest_message_version_id IS NULL
                  AND (policy_version IS NULL OR policy_version != ?)
                ORDER BY last_classified_at, lead_id
                """,
                (now_text, CLASSIFICATION_POLICY_VERSION),
            ).fetchall()
            for row in rows:
                if not _needs_policy_reclassification(row):
                    continue
                cursor = connection.execute(
                    """
                    UPDATE secondary_lead_state
                    SET status = 'settling',
                        classification_due_at = ?,
                        next_action_at = NULL,
                        last_error = NULL,
                        updated_at = ?
                    WHERE lead_id = ?
                      AND status = 'scheduled'
                    """,
                    (now_text, now_text, row["lead_id"]),
                )
                if not cursor.rowcount:
                    continue
                requeued += 1
                self._event(
                    connection,
                    row["lead_id"],
                    "policy_reclassification_queued",
                    {
                        "policy_version": CLASSIFICATION_POLICY_VERSION,
                        "classification_due_at": now_text,
                    },
                )
        if requeued:
            self._notify_scheduler()
        return requeued

    def _recover_interrupted_classifications(self) -> int:
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT lead_id
                FROM secondary_lead_state
                WHERE status = 'classifying'
                ORDER BY updated_at, lead_id
                """
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE secondary_lead_state
                    SET status = 'settling',
                        classification_due_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE lead_id = ? AND status = 'classifying'
                    """,
                    (
                        now_text,
                        "Recovered after scheduler restart",
                        now_text,
                        row["lead_id"],
                    ),
                )
                self._event(
                    connection,
                    row["lead_id"],
                    "classification_recovered",
                    {"classification_due_at": now_text},
                )
        return len(rows)

    def _recover_interrupted_runs(self) -> int:
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE scheduler_run
                SET status = 'failed',
                    completed_at = ?,
                    error = ?
                WHERE status = 'running'
                """,
                (
                    now_text,
                    "Recovered after scheduler restart",
                ),
            )
        return max(cursor.rowcount, 0)

    def _recover_interrupted_message_jobs(self) -> int:
        """Release jobs left behind by a crashed scheduler process.

        ``processing`` is only owned while the scheduler process is alive.  A
        short age guard keeps a maintenance call from stealing a job that is
        currently being processed by another invocation.  Expired retries are
        already claimable by ``MessageJobProcessor``; their lead state still
        needs to be made runnable when a crash happened between the two state
        updates.
        """
        now = self.now()
        now_text = isoformat(now)
        stale_before = isoformat(now - MESSAGE_JOB_STALE_AFTER)
        recovered = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job.id, job.lead_id, job.status, job.next_attempt_at,
                       state.status AS lead_status
                FROM analysis_job AS job
                JOIN secondary_lead_state AS state
                  ON state.latest_analysis_job_id = job.id
                WHERE state.status IN ('generating', 'scheduled')
                  AND (
                    (
                        job.status = 'processing'
                        AND (
                            job.processing_started_at IS NULL
                            OR job.processing_started_at <= ?
                        )
                    )
                    OR (
                        job.status = 'retry_wait'
                        AND job.next_attempt_at IS NOT NULL
                        AND job.next_attempt_at <= ?
                        AND (
                            state.status = 'generating'
                            OR state.next_action_at IS NULL
                            OR state.next_action_at > ?
                        )
                    )
                  )
                ORDER BY job.updated_at, job.id
                """,
                (stale_before, now_text, now_text),
            ).fetchall()
            for row in rows:
                if row["status"] == "processing":
                    connection.execute(
                        """
                        UPDATE analysis_job
                        SET status = 'pending',
                            next_attempt_at = NULL,
                            processing_started_at = NULL,
                            last_error = ?,
                            updated_at = ?
                        WHERE id = ? AND status = 'processing'
                        """,
                        (
                            "Recovered after scheduler restart",
                            now_text,
                            row["id"],
                        ),
                    )
                recovered += 1
                if row["lead_status"] not in {"generating", "scheduled"}:
                    continue
                connection.execute(
                    """
                    UPDATE secondary_lead_state
                    SET status = 'scheduled',
                        next_action_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE lead_id = ?
                      AND status IN ('generating', 'scheduled')
                    """,
                    (
                        now_text,
                        "Recovered after scheduler restart",
                        now_text,
                        row["lead_id"],
                    ),
                )
                self._event(
                    connection,
                    row["lead_id"],
                    "message_generation_recovered",
                    {
                        "analysis_job_id": row["id"],
                        "previous_job_status": row["status"],
                    },
                )
        return recovered

    def _upsert_discovered(self, record: Dict[str, Any]) -> str:
        lead_id, source_updated_at, source_record_id, source_hash = (
            self._record_identity(record)
        )
        now_text = isoformat(self.now())
        due_at = self._classification_due_at(record)
        snapshot_json = _canonical_json(record)
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT source_hash, latest_analysis_job_id
                FROM secondary_lead_state WHERE lead_id = ?
                """,
                (lead_id,),
            ).fetchone()
            if existing is not None and existing["source_hash"] != source_hash:
                reject_pending_messages_for_lead(
                    lead_id,
                    "secondary-scheduler",
                    "CRM source changed before review; superseded draft rejected.",
                )
            connection.execute("BEGIN IMMEDIATE")
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at, source_record_id,
                       crm_snapshot_json, status, classification_due_at,
                       last_seen_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'settling', ?, ?, ?, ?)
                    """,
                    (
                        lead_id,
                        source_hash,
                        source_updated_at,
                        source_record_id,
                        snapshot_json,
                        due_at,
                        now_text,
                        now_text,
                        now_text,
                    ),
                )
                self._event(
                    connection,
                    lead_id,
                    "discovered",
                    {"classification_due_at": due_at},
                )
                result = "inserted"
            elif existing["source_hash"] != source_hash:
                if existing["latest_analysis_job_id"]:
                    connection.execute(
                        """
                        UPDATE analysis_job
                        SET status = 'failed',
                            next_attempt_at = NULL,
                            processing_started_at = NULL,
                            completed_at = ?,
                            last_error = ?,
                            updated_at = ?
                        WHERE id = ?
                          AND status IN ('pending', 'processing', 'retry_wait')
                        """,
                        (
                            now_text,
                            "Superseded by CRM source change",
                            now_text,
                            existing["latest_analysis_job_id"],
                        ),
                    )
                connection.execute(
                    """
                    UPDATE secondary_lead_state
                    SET source_hash = ?,
                        source_updated_at = ?,
                        source_record_id = ?,
                        crm_snapshot_json = ?,
                        status = 'settling',
                        classification_due_at = ?,
                        next_action_at = NULL,
                        follow_up_count = 0,
                        latest_analysis_job_id = NULL,
                        latest_message_version_id = NULL,
                        latest_run_id = NULL,
                        last_seen_at = ?,
                        last_error = NULL,
                        updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        source_hash,
                        source_updated_at,
                        source_record_id,
                        snapshot_json,
                        due_at,
                        now_text,
                        now_text,
                        lead_id,
                    ),
                )
                self._event(
                    connection,
                    lead_id,
                    "source_changed",
                    {"classification_due_at": due_at},
                )
                result = "updated"
            else:
                connection.execute(
                    """
                    UPDATE secondary_lead_state
                    SET source_updated_at = ?,
                        source_record_id = ?,
                        crm_snapshot_json = ?,
                        last_seen_at = ?,
                        updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        source_updated_at,
                        source_record_id,
                        snapshot_json,
                        now_text,
                        now_text,
                        lead_id,
                    ),
                )
                result = "unchanged"
        if result != "unchanged":
            self._notify_scheduler()
        return result

    def scan(self) -> Dict[str, int]:
        batch_size = _validated_int(
            self.environment,
            "HERMES_SECONDARY_SCAN_BATCH_SIZE",
            500,
            1,
            9999,
        )
        cursor_id = ""
        seen_lead_ids = set()
        counts = {
            "seen": 0,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "converted": 0,
        }
        while True:
            records = self._export_batch(cursor_id, batch_size)
            if not records:
                break
            for record in records:
                lead_id, _, _, _ = self._record_identity(record)
                seen_lead_ids.add(lead_id)
                status = self._upsert_discovered(record)
                counts["seen"] += 1
                counts[status] += 1
            last = records[-1]
            _, _, next_id, _ = self._record_identity(last)
            if next_id <= cursor_id:
                raise RuntimeError("Secondary CRM scan cursor did not advance")
            cursor_id = next_id
            if len(records) < batch_size:
                break
        with self._connect() as connection:
            missing_lead_ids = [
                row["lead_id"]
                for row in connection.execute(
                    """
                    SELECT lead_id FROM secondary_lead_state
                    WHERE status != 'converted'
                    """
                )
                if row["lead_id"] not in seen_lead_ids
            ]
        for lead_id in missing_lead_ids:
            if self._convert_out_of_scope(lead_id):
                counts["converted"] += 1
        return counts

    def _convert_out_of_scope(
        self,
        lead_id: str,
        source: str = "full_scan",
    ) -> bool:
        reject_pending_messages_for_lead(
            lead_id,
            "secondary-scheduler",
            "Lead left the secondary CRM scope; pending review retired.",
        )
        dismiss_pending_actions_for_lead(
            lead_id,
            "secondary-scheduler",
            "Lead left the secondary CRM scope; pending action dismissed.",
        )
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE analysis_job
                SET status = 'failed', next_attempt_at = NULL,
                    processing_started_at = NULL, completed_at = ?,
                    last_error = ?, updated_at = ?
                WHERE lead_id = ?
                  AND status IN ('pending', 'processing', 'retry_wait')
                """,
                (
                    now_text,
                    "Lead left the secondary CRM scope",
                    now_text,
                    lead_id,
                ),
            )
            connection.execute(
                """
                UPDATE notes_follow_up_state
                SET status = 'superseded', next_attempt_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE lead_id = ?
                  AND status IN ('pending', 'processing', 'retry_wait')
                """,
                (now_text, lead_id),
            )
            changed = connection.execute(
                """
                UPDATE secondary_lead_state
                SET status = 'converted',
                    classification_due_at = NULL,
                    next_action_at = NULL,
                    latest_analysis_job_id = NULL,
                    latest_message_version_id = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE lead_id = ? AND status != 'converted'
                """,
                (now_text, lead_id),
            ).rowcount
            if changed:
                self._event(
                    connection,
                    lead_id,
                    "left_secondary_lead_scope",
                    {"source": source},
                )
        return bool(changed)

    def _claim_due_classification(self) -> Optional[sqlite3.Row]:
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM secondary_lead_state
                WHERE status = 'settling'
                  AND classification_due_at <= ?
                  AND NOT EXISTS (
                    SELECT 1
                    FROM notes_follow_up_state notes
                    WHERE notes.lead_id = secondary_lead_state.lead_id
                      AND notes.status != 'superseded'
                  )
                ORDER BY
                  CASE
                    WHEN policy_version IS NOT NULL
                      AND policy_version != ? THEN 0
                    ELSE 1
                  END,
                  classification_due_at,
                  lead_id
                LIMIT 1
                """,
                (now_text, CLASSIFICATION_POLICY_VERSION),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET status = 'classifying', updated_at = ?
                WHERE lead_id = ? AND status = 'settling'
                """,
                (now_text, row["lead_id"]),
            )
            return connection.execute(
                "SELECT * FROM secondary_lead_state WHERE lead_id = ?",
                (row["lead_id"],),
            ).fetchone()

    def _interval_days(
        self,
        lead_type: str,
        lead_id: str,
        sequence: int,
    ) -> int:
        return interval_days(lead_type, lead_id, sequence)

    def _classify_one(self, row: sqlite3.Row) -> Dict[str, Any]:
        lead_id = row["lead_id"]
        record = json.loads(row["crm_snapshot_json"])
        run_id = str(uuid.uuid4())
        run_dir = self.classification_dir / run_id
        candidate, usage = self.classification_runner.run(record, run_dir)
        now_text = isoformat(self.now())
        confidence = float(candidate["confidence"])
        threshold = float(
            self.environment.get("HERMES_CLASSIFICATION_MIN_CONFIDENCE", "0.65")
        )
        if threshold < 0 or threshold > 1:
            raise RuntimeError(
                "HERMES_CLASSIFICATION_MIN_CONFIDENCE must be between 0 and 1"
            )
        contact_permission = candidate["contact_permission"]["status"]
        warnings = record.get("warnings") or []
        follow_up_status = _sales_follow_up_status(candidate)
        follow_up_days = self._interval_days(candidate["lead_type"], lead_id, 0)
        follow_up_at = (
            _sales_follow_up_due_at(record, follow_up_days)
            if follow_up_status != "none"
            else None
        )
        if contact_permission == "do_not_contact" or "DO_NOT_CONTACT" in warnings:
            status = "paused"
            next_action_at = None
            pause_reason = "CRM contains DO_NOT_CONTACT"
        elif follow_up_status != "none" and int(row["follow_up_count"]) >= 1:
            status = "paused"
            next_action_at = None
            pause_reason = "Sales already replied and one automated follow-up was sent"
        elif follow_up_status != "none" and follow_up_at is None:
            status = "needs_review"
            next_action_at = None
            pause_reason = SALES_FOLLOW_UP_TIME_UNVERIFIED
        elif confidence < threshold:
            status = "needs_review"
            next_action_at = None
            pause_reason = "Classification confidence is below threshold"
        else:
            status = "scheduled"
            next_action_at = follow_up_at or isoformat(
                self.now() + timedelta(days=follow_up_days)
            )
            pause_reason = None
        classification_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"secondary-classification:{lead_id}:{row['source_hash']}",
            )
        )
        policy_version = CLASSIFICATION_POLICY_VERSION
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT source_hash, latest_analysis_job_id
                FROM secondary_lead_state WHERE lead_id = ?
                """,
                (lead_id,),
            ).fetchone()
            if current is None or current["source_hash"] != row["source_hash"]:
                raise RuntimeError("CRM source changed while classification was running")
            if current["latest_analysis_job_id"]:
                connection.execute(
                    """
                    UPDATE analysis_job
                    SET status = 'failed',
                        next_attempt_at = NULL,
                        processing_started_at = NULL,
                        completed_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('pending', 'processing', 'retry_wait')
                    """,
                    (
                        now_text,
                        "Superseded by current classification",
                        now_text,
                        current["latest_analysis_job_id"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO secondary_lead_classification
                  (id, lead_id, source_hash, lead_type, confidence, reason,
                   evidence_json, policy_version, result_json, usage_json,
                   classified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (lead_id, source_hash) DO UPDATE SET
                  lead_type = excluded.lead_type,
                  confidence = excluded.confidence,
                  reason = excluded.reason,
                  evidence_json = excluded.evidence_json,
                  policy_version = excluded.policy_version,
                  result_json = excluded.result_json,
                  usage_json = excluded.usage_json,
                  classified_at = excluded.classified_at
                """,
                (
                    classification_id,
                    lead_id,
                    row["source_hash"],
                    candidate["lead_type"],
                    confidence,
                    candidate["reason"],
                    _canonical_json(candidate["evidence"]),
                    policy_version,
                    _canonical_json(candidate),
                    _canonical_json(usage) if isinstance(usage, dict) else None,
                    now_text,
                ),
            )
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET lead_type = ?,
                    classification_confidence = ?,
                    classification_reason = ?,
                    policy_version = ?,
                    status = ?,
                    classification_due_at = NULL,
                    next_action_at = ?,
                    latest_analysis_job_id = NULL,
                    last_classified_at = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (
                    candidate["lead_type"],
                    confidence,
                    candidate["reason"],
                    policy_version,
                    status,
                    next_action_at,
                    now_text,
                    pause_reason,
                    now_text,
                    lead_id,
                ),
            )
            self._event(
                connection,
                lead_id,
                "classified",
                {
                    "lead_type": candidate["lead_type"],
                    "confidence": confidence,
                    "status": status,
                    "next_action_at": next_action_at,
                },
            )
        return {
            "lead_id": lead_id,
            "lead_type": candidate["lead_type"],
            "confidence": confidence,
            "status": status,
            "next_action_at": next_action_at,
        }

    def classify_due(self, limit: int = 20) -> List[Dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise RuntimeError("Classification limit must be between 1 and 500")
        with self._connect() as connection:
            resume_at = self._state(
                connection,
                "classification_backlog_resume_at",
            )
        if resume_at and parse_timestamp(resume_at) > self.now():
            return []
        results = []
        successful = 0
        attempts = 0
        max_attempts = limit * 2
        while successful < limit and attempts < max_attempts:
            row = self._claim_due_classification()
            if row is None:
                break
            attempts += 1
            try:
                results.append(self._classify_one(row))
                successful += 1
            except Exception as exc:
                retry_at = isoformat(self.now() + timedelta(minutes=20))
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE secondary_lead_state
                        SET status = 'settling',
                            classification_due_at = ?,
                            last_error = ?,
                            updated_at = ?
                        WHERE lead_id = ?
                        """,
                        (
                            retry_at,
                            str(exc)[-4000:],
                            isoformat(self.now()),
                            row["lead_id"],
                        ),
                    )
                    self._event(
                        connection,
                        row["lead_id"],
                        "classification_failed",
                        {"retry_at": retry_at, "error": str(exc)[-1000:]},
                    )
                results.append(
                    {
                        "lead_id": row["lead_id"],
                        "status": "retry_wait",
                        "retry_at": retry_at,
                        "error": str(exc)[-1000:],
                    }
                )
        backlog_delay_minutes = _validated_int(
            self.environment,
            "HERMES_CLASSIFICATION_BACKLOG_DELAY_MINUTES",
            10,
            1,
            1440,
        )
        with self._connect() as connection:
            due = connection.execute(
                """
                SELECT 1
                FROM secondary_lead_state
                WHERE status = 'settling'
                  AND classification_due_at <= ?
                LIMIT 1
                """,
                (isoformat(self.now()),),
            ).fetchone()
            if due and (successful >= limit or attempts >= max_attempts):
                self._set_state(
                    connection,
                    "classification_backlog_resume_at",
                    isoformat(
                        self.now()
                        + timedelta(minutes=backlog_delay_minutes)
                    ),
                )
            elif not due:
                connection.execute(
                    """
                    DELETE FROM scheduler_state
                    WHERE key = 'classification_backlog_resume_at'
                    """
                )
        return results

    def confirm_classification(
        self,
        lead_id: str,
        lead_type: str,
        reviewer: str = "review-ui",
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        if lead_type not in LEAD_TYPES:
            raise RuntimeError("Classification lead_type is invalid")
        reviewer = reviewer.strip()
        if not reviewer or len(reviewer) > 200:
            raise RuntimeError("Reviewer must be between 1 and 200 characters")
        if note is not None and len(note) > 2000:
            raise RuntimeError("Review note must be at most 2000 characters")
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM secondary_lead_state
                WHERE lead_id = ?
                """,
                (lead_id,),
            ).fetchone()
            if row is None:
                raise KeyError(lead_id)
            if (
                row["status"] != "needs_review"
                or row["last_classified_at"] is None
                or row["last_generated_at"] is not None
                or row["latest_message_version_id"] is not None
            ):
                raise RuntimeError(
                    "This lead is not waiting for classification confirmation"
                )
            previous_type = row["lead_type"]
            next_action_at = isoformat(
                self.now()
                + timedelta(
                    days=self._interval_days(
                        lead_type,
                        lead_id,
                        int(row["follow_up_count"]),
                    )
                )
            )
            classification = connection.execute(
                """
                SELECT id, result_json
                FROM secondary_lead_classification
                WHERE lead_id = ? AND source_hash = ?
                """,
                (lead_id, row["source_hash"]),
            ).fetchone()
            if classification is None:
                raise RuntimeError("Classification result is missing")
            result_json = self._json_value(classification["result_json"], {})
            if not isinstance(result_json, dict):
                result_json = {}
            result_json["model_lead_type"] = (
                result_json.get("model_lead_type") or previous_type
            )
            result_json["lead_type"] = lead_type
            result_json["human_review"] = {
                "reviewer": reviewer,
                "note": note,
                "reviewed_at": now_text,
            }
            connection.execute(
                """
                UPDATE secondary_lead_classification
                SET lead_type = ?, result_json = ?
                WHERE id = ?
                """,
                (
                    lead_type,
                    _canonical_json(result_json),
                    classification["id"],
                ),
            )
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET lead_type = ?,
                    status = 'scheduled',
                    next_action_at = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (lead_type, next_action_at, now_text, lead_id),
            )
            self._event(
                connection,
                lead_id,
                "classification_confirmed",
                {
                    "model_lead_type": previous_type,
                    "lead_type": lead_type,
                    "reviewer": reviewer,
                    "note": note,
                    "next_action_at": next_action_at,
                },
            )
        self._notify_scheduler()
        return {
            "lead_id": lead_id,
            "lead_type": lead_type,
            "status": "scheduled",
            "next_action_at": next_action_at,
            "reviewer": reviewer,
        }

    def retry_attention(
        self,
        lead_id: str,
        actor: str = "review-ui",
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        actor = actor.strip()
        if not actor or len(actor) > 200:
            raise RuntimeError("Actor must be between 1 and 200 characters")
        if note is not None and len(note) > 2000:
            raise RuntimeError("Retry note must be at most 2000 characters")
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM secondary_lead_state WHERE lead_id = ?",
                (lead_id,),
            ).fetchone()
            if row is None:
                raise KeyError(lead_id)
            if row["status"] not in {"needs_contact", "failed"}:
                raise RuntimeError("This lead is not waiting for a manual retry")
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET status = 'scheduled',
                    next_action_at = ?,
                    last_error = NULL,
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (now_text, now_text, lead_id),
            )
            self._event(
                connection,
                lead_id,
                "manual_retry_queued",
                {
                    "previous_status": row["status"],
                    "actor": actor,
                    "note": note,
                    "next_action_at": now_text,
                },
            )
        self._notify_scheduler()
        return {
            "lead_id": lead_id,
            "status": "scheduled",
            "next_action_at": now_text,
        }

    def _export_current_record(self, lead_id: str) -> Optional[Dict[str, Any]]:
        records = self._export_batch(
            "",
            1,
            record_id=lead_id,
        )
        return records[0] if records else None

    def _claim_due_action(self) -> Optional[sqlite3.Row]:
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM secondary_lead_state
                WHERE status = 'scheduled'
                  AND next_action_at <= ?
                  AND NOT EXISTS (
                    SELECT 1
                    FROM notes_follow_up_state notes
                    WHERE notes.lead_id = secondary_lead_state.lead_id
                      AND notes.status != 'superseded'
                  )
                ORDER BY next_action_at, lead_id
                LIMIT 1
                """,
                (now_text,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET status = 'generating', updated_at = ?
                WHERE lead_id = ? AND status = 'scheduled'
                """,
                (now_text, row["lead_id"]),
            )
            return connection.execute(
                "SELECT * FROM secondary_lead_state WHERE lead_id = ?",
                (row["lead_id"],),
            ).fetchone()

    def _classification_context(
        self,
        row: sqlite3.Row,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            classification = connection.execute(
                """
                SELECT result_json
                FROM secondary_lead_classification
                WHERE lead_id = ? AND source_hash = ?
                """,
                (row["lead_id"], row["source_hash"]),
            ).fetchone()
        if classification is None:
            return None
        result = self._json_value(classification["result_json"], None)
        return result if isinstance(result, dict) else None

    def _message_input(self, row: sqlite3.Row) -> Dict[str, Any]:
        source_record = json.loads(row["crm_snapshot_json"])
        classification = self._classification_context(row)
        record = prepare_message_record(source_record, classification)
        lead = record.setdefault("lead", {})
        lead_type = row["lead_type"]
        if lead_type == "no_current_demand":
            lead["type"] = "no_current_demand"
            lead.pop("subtype", None)
        else:
            lead["type"] = "lead"
            lead["subtype"] = MESSAGE_SUBTYPES[lead_type]
        lead["raw_type"] = LEAD_TYPE_LABELS[lead_type]
        lead["classification_source"] = "local.secondary_lead_state"
        lead["next_eligible_follow_up_at"] = row["next_action_at"]
        lead["successful_outbound_count"] = int(row["follow_up_count"])
        record["secondary_lead_schedule"] = {
            "lead_type": lead_type,
            "lead_type_label": LEAD_TYPE_LABELS[lead_type],
            "classified_at": row["last_classified_at"],
            "next_action_at": row["next_action_at"],
            "follow_up_count": int(row["follow_up_count"]),
            "timing_verified": True,
        }
        return record

    def _enqueue_message(
        self,
        row: sqlite3.Row,
        record: Dict[str, Any],
    ) -> str:
        generation_key = (
            f"{row['lead_id']}:{row['source_hash']}:{row['next_action_at']}:"
            f"{row['follow_up_count']}"
        )
        job_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"secondary-message:{generation_key}",
            )
        )
        existing_job_id = row["latest_analysis_job_id"]
        if existing_job_id == job_id:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT status FROM analysis_job WHERE id = ?",
                    (existing_job_id,),
                ).fetchone()
            if existing and existing["status"] in {"pending", "retry_wait"}:
                return job_id
        message_hash = hashlib.sha256(generation_key.encode("utf-8")).hexdigest()
        job_dir = self.message_processor.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job_dir.chmod(0o700)
        input_path = job_dir / "input.json"
        if not input_path.exists():
            input_path.write_text(
                json.dumps([record], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            input_path.chmod(0o600)
        now_text = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if existing_job_id and existing_job_id != job_id:
                connection.execute(
                    """
                    UPDATE analysis_job
                    SET status = 'failed',
                        next_attempt_at = NULL,
                        processing_started_at = NULL,
                        completed_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status IN ('pending', 'processing', 'retry_wait')
                    """,
                    (
                        now_text,
                        "Superseded by current message input",
                        now_text,
                        existing_job_id,
                    ),
                )
            connection.execute(
                """
                INSERT INTO analysis_job
                  (id, lead_id, source_hash, source_updated_at, source_record_id,
                   status, input_path, run_root, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT (lead_id, source_hash) DO NOTHING
                """,
                (
                    job_id,
                    row["lead_id"],
                    message_hash,
                    row["source_updated_at"],
                    row["source_record_id"],
                    str(input_path),
                    str(job_dir / "runs"),
                    now_text,
                    now_text,
                ),
            )
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET latest_analysis_job_id = ?, updated_at = ?
                WHERE lead_id = ?
                """,
                (job_id, now_text, row["lead_id"]),
            )
        return job_id

    def _analysis_result(self, job_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM analysis_result WHERE job_id = ?",
                (job_id,),
            ).fetchone()

    def _dispatch_one(self, row: sqlite3.Row) -> Dict[str, Any]:
        lead_id = row["lead_id"]
        current = self._export_current_record(lead_id)
        if current is None:
            self._convert_out_of_scope(lead_id, source="dispatch_recheck")
            return {"lead_id": lead_id, "status": "converted"}
        _, _, _, current_hash = self._record_identity(current)
        if current_hash != row["source_hash"]:
            self._upsert_discovered(current)
            return {"lead_id": lead_id, "status": "resettling"}
        self._upsert_discovered(current)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM secondary_lead_state WHERE lead_id = ?",
                (lead_id,),
            ).fetchone()

        message_input = self._message_input(row)
        job_id = self._enqueue_message(row, message_input)
        process_result = self.message_processor.process_job(job_id)
        analysis = self._analysis_result(job_id)
        if analysis is None:
            with self._connect() as connection:
                job = connection.execute(
                    "SELECT status, next_attempt_at, last_error FROM analysis_job WHERE id = ?",
                    (job_id,),
                ).fetchone()
                retry_at = job["next_attempt_at"] if job else None
                status = "scheduled" if retry_at else "failed"
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE secondary_lead_state
                    SET status = ?,
                        next_action_at = ?,
                        last_error = ?,
                        updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (
                        status,
                        retry_at,
                        (job["last_error"] if job else "Message generation failed"),
                        isoformat(self.now()),
                        lead_id,
                    ),
                )
            return {
                "lead_id": lead_id,
                "status": status,
                "next_action_at": retry_at,
                "pipeline": process_result,
            }

        output = json.loads(analysis["result_json"])
        decision = analysis["decision"]
        now_text = isoformat(self.now())
        if decision == "generated":
            channel = analysis["output_type"]
            contact = message_input.get("contact") or {}
            recipient = (
                contact.get("email")
                if channel == "email"
                else contact.get("linkedin_url")
            )
            recipient_valid = (
                valid_email(recipient)
                if channel == "email"
                else valid_linkedin(recipient)
            )
            if recipient_valid:
                message_version_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        (
                            f"twenty-hermes:{analysis['run_id']}:{lead_id}:"
                            f"{channel}:1"
                        ),
                    )
                )
                status = "waiting_review"
                error = None
            else:
                message_version_id = None
                status = "needs_contact"
                error = "Generated message has no valid Outbox recipient"
        elif decision == "no_message":
            message_version_id = None
            status = "paused"
            error = output.get("reason") or "Hermes decided not to generate a message"
        else:
            message_version_id = None
            status = "needs_review"
            error = output.get("reason") or "Hermes could not generate a message"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET status = ?,
                    next_action_at = NULL,
                    latest_message_version_id = ?,
                    latest_run_id = ?,
                    last_generated_at = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (
                    status,
                    message_version_id,
                    analysis["run_id"],
                    now_text,
                    error,
                    now_text,
                    lead_id,
                ),
            )
            self._event(
                connection,
                lead_id,
                "message_generation_completed",
                {
                    "decision": decision,
                    "status": status,
                    "analysis_job_id": job_id,
                    "message_version_id": message_version_id,
                },
            )
        return {
            "lead_id": lead_id,
            "status": status,
            "decision": decision,
            "analysis_job_id": job_id,
            "message_version_id": message_version_id,
        }

    def dispatch_due(self, limit: int = 20) -> List[Dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise RuntimeError("Dispatch limit must be between 1 and 500")
        results = []
        for _ in range(limit):
            row = self._claim_due_action()
            if row is None:
                break
            try:
                results.append(self._dispatch_one(row))
            except Exception as exc:
                retry_at = isoformat(self.now() + timedelta(minutes=20))
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """
                        UPDATE secondary_lead_state
                        SET status = 'scheduled',
                            next_action_at = ?,
                            last_error = ?,
                            updated_at = ?
                        WHERE lead_id = ?
                        """,
                        (
                            retry_at,
                            str(exc)[-4000:],
                            isoformat(self.now()),
                            row["lead_id"],
                        ),
                    )
                    self._event(
                        connection,
                        row["lead_id"],
                        "message_generation_failed",
                        {"retry_at": retry_at, "error": str(exc)[-1000:]},
                    )
                results.append(
                    {
                        "lead_id": row["lead_id"],
                        "status": "retry_wait",
                        "retry_at": retry_at,
                        "error": str(exc)[-1000:],
                    }
                )
        return results

    def handle_outbox_signal(
        self,
        message_version_id: str,
        event: str,
        event_at: Optional[datetime] = None,
    ) -> bool:
        if event not in {"approved", "sent", "rejected"}:
            raise RuntimeError("Outbox signal must be approved, sent, or rejected")
        occurred = (event_at or self.now()).astimezone(timezone.utc)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM secondary_lead_state
                WHERE latest_message_version_id = ?
                """,
                (message_version_id,),
            ).fetchone()
            if row is None:
                return False
            if event == "approved":
                status = "waiting_delivery"
                next_action_at = None
                follow_up_count = int(row["follow_up_count"])
            elif event == "rejected":
                status = "needs_review"
                next_action_at = None
                follow_up_count = int(row["follow_up_count"])
            else:
                follow_up_count = int(row["follow_up_count"]) + 1
                classification = self._classification_context(row)
                has_prior_sales_follow_up = (
                    _sales_follow_up_status(classification) != "none"
                )
                if (
                    (has_prior_sales_follow_up and follow_up_count >= 1)
                    or (
                        row["lead_type"] in SHORT_CYCLE_TYPES
                        and follow_up_count >= 2
                    )
                ):
                    status = "paused"
                    next_action_at = None
                else:
                    status = "scheduled"
                    days = self._interval_days(
                        row["lead_type"],
                        row["lead_id"],
                        follow_up_count,
                    )
                    next_action_at = isoformat(occurred + timedelta(days=days))
            connection.execute(
                """
                UPDATE secondary_lead_state
                SET status = ?,
                    next_action_at = ?,
                    follow_up_count = ?,
                    latest_message_version_id = CASE WHEN ? THEN NULL ELSE latest_message_version_id END,
                    last_generated_at = CASE WHEN ? THEN NULL ELSE last_generated_at END,
                    last_error = NULL,
                    updated_at = ?
                WHERE lead_id = ?
                """,
                (
                    status,
                    next_action_at,
                    follow_up_count,
                    event == "rejected",
                    event == "rejected",
                    isoformat(occurred),
                    row["lead_id"],
                ),
            )
            self._event(
                connection,
                row["lead_id"],
                f"outbox_{event}",
                {
                    "message_version_id": message_version_id,
                    "status": status,
                    "next_action_at": next_action_at,
                    "follow_up_count": follow_up_count,
                },
                event_at=isoformat(occurred),
            )
        self._notify_scheduler()
        return True

    def _scheduler_due(self, key: str) -> bool:
        with self._connect() as connection:
            value = self._state(connection, key)
        return value is None or parse_timestamp(value) <= self.now()

    @staticmethod
    def _runtime_status(scheduler: Dict[str, str]) -> Dict[str, Optional[str]]:
        return {
            "mode": scheduler.get("runtime_mode", "unknown"),
            "last_checked_at": scheduler.get("remote_last_checked_at"),
            "retry_at": scheduler.get("remote_retry_at"),
            "last_error": scheduler.get("remote_last_error"),
        }

    def _set_runtime_mode(
        self,
        mode: str,
        error: Optional[str] = None,
        retry_at: Optional[str] = None,
        reconnect_scan_required: bool = False,
    ) -> Dict[str, Optional[str]]:
        checked_at = isoformat(self.now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._set_state(connection, "runtime_mode", mode)
            self._set_state(connection, "remote_last_checked_at", checked_at)
            self._set_state(
                connection,
                "remote_reconnect_scan_required",
                "true" if reconnect_scan_required else "false",
            )
            if error:
                self._set_state(
                    connection,
                    "remote_last_error",
                    error[-2000:],
                )
            else:
                connection.execute(
                    "DELETE FROM scheduler_state WHERE key = 'remote_last_error'"
                )
            if retry_at:
                self._set_state(connection, "remote_retry_at", retry_at)
            else:
                connection.execute(
                    "DELETE FROM scheduler_state WHERE key = 'remote_retry_at'"
                )
            scheduler = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM scheduler_state"
                )
            }
        return self._runtime_status(scheduler)

    def _enter_local_only(self, error: str) -> Dict[str, Optional[str]]:
        retry_minutes = _validated_int(
            self.environment,
            "HERMES_REMOTE_RETRY_MINUTES",
            10,
            1,
            1440,
        )
        return self._set_runtime_mode(
            "local_only",
            error=error,
            retry_at=isoformat(self.now() + timedelta(minutes=retry_minutes)),
            reconnect_scan_required=True,
        )

    def _probe_remote_services(self) -> Dict[str, Any]:
        with self._connect() as connection:
            scheduler = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM scheduler_state"
                )
            }
        retry_at = scheduler.get("remote_retry_at")
        if (
            scheduler.get("runtime_mode") == "local_only"
            and retry_at
            and parse_timestamp(retry_at) > self.now()
        ):
            return {
                "available": False,
                "reconnect_scan_required": True,
                **self._runtime_status(scheduler),
            }
        if not DEFAULT_DB_CHECK_SCRIPT.is_file():
            runtime = self._enter_local_only(
                f"Twenty database check script is missing: {DEFAULT_DB_CHECK_SCRIPT}"
            )
            return {
                "available": False,
                "reconnect_scan_required": True,
                **runtime,
            }
        try:
            result = subprocess.run(
                [str(DEFAULT_DB_CHECK_SCRIPT)],
                cwd=PROJECT_DIR,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            runtime = self._enter_local_only(
                "Twenty database check timed out"
            )
            return {
                "available": False,
                "reconnect_scan_required": True,
                **runtime,
            }
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            runtime = self._enter_local_only(
                "Twenty database is unavailable: "
                + (detail[-1500:] or f"exit {result.returncode}")
            )
            return {
                "available": False,
                "reconnect_scan_required": True,
                **runtime,
            }
        try:
            outbox_preflight()
        except Exception as exc:
            runtime = self._enter_local_only(
                f"Outbox database is unavailable: {str(exc)[-1500:]}"
            )
            return {
                "available": False,
                "reconnect_scan_required": True,
                **runtime,
            }
        return {
            "available": True,
            "reconnect_scan_required": (
                scheduler.get("remote_reconnect_scan_required") == "true"
            ),
            **self._runtime_status(scheduler),
        }

    def _record_run_start(
        self,
        run_id: str,
        trigger: str,
        started_at: str,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO scheduler_run
                      (id, trigger, status, started_at)
                    VALUES (?, ?, 'running', ?)
                    """,
                    (run_id, trigger, started_at),
                )
        except Exception:
            pass

    def _record_run_finish(
        self,
        run_id: str,
        status: str,
        completed_at: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE scheduler_run
                    SET status = ?, completed_at = ?, result_json = ?, error = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        completed_at,
                        _canonical_json(result) if result is not None else None,
                        error[-4000:] if error else None,
                        run_id,
                    ),
                )
        except Exception:
            pass

    def run_cycle(
        self,
        trigger: str = "manual",
        allow_local_only: bool = False,
    ) -> Dict[str, Any]:
        run_id = str(uuid.uuid4())
        started_at = isoformat(self.now())
        self._record_run_start(run_id, trigger, started_at)
        try:
            self._recover_interrupted_message_jobs()
            self._requeue_outdated_review_classifications()
            full_scan = None
            mode = "online"
            remote: Dict[str, Any] = {
                "mode": mode,
                "last_checked_at": None,
                "retry_at": None,
                "last_error": None,
            }
            reconnect_scan_required = False
            if allow_local_only:
                probe = self._probe_remote_services()
                reconnect_scan_required = bool(
                    probe.get("reconnect_scan_required")
                )
                remote = {
                    key: probe.get(key)
                    for key in (
                        "mode",
                        "last_checked_at",
                        "retry_at",
                        "last_error",
                    )
                }
                if not probe["available"]:
                    mode = "local_only"
            full_scan_hours = _validated_int(
                self.environment,
                "HERMES_SECONDARY_FULL_SCAN_HOURS",
                6,
                1,
                168,
            )
            if mode == "online" and (
                reconnect_scan_required
                or self._scheduler_due("next_full_scan_at")
            ):
                try:
                    full_scan = self.scan()
                    with self._connect() as connection:
                        self._set_state(
                            connection,
                            "next_full_scan_at",
                            isoformat(
                                self.now() + timedelta(hours=full_scan_hours)
                            ),
                        )
                except Exception as exc:
                    if not allow_local_only:
                        raise
                    mode = "local_only"
                    remote = self._enter_local_only(
                        f"Twenty CRM scan failed: {str(exc)[-1500:]}"
                    )
            if allow_local_only and mode == "online":
                remote = self._set_runtime_mode("online")
            notes = None
            notes_enabled = self.environment.get(
                "HERMES_NOTES_ENABLED", "true"
            ).strip().lower() in {"1", "true", "yes", "on"}
            if (
                mode == "online"
                and notes_enabled
                and self._scheduler_due("next_notes_scan_at")
            ):
                notes_interval_minutes = _validated_int(
                    self.environment,
                    "HERMES_NOTES_SCAN_MINUTES",
                    10,
                    1,
                    1440,
                )
                try:
                    notes = self.notes_processor.run()
                except Exception as exc:
                    notes = {"status": "error", "error": str(exc)[-1500:]}
                finally:
                    with self._connect() as connection:
                        self._set_state(
                            connection,
                            "next_notes_scan_at",
                            isoformat(
                                self.now()
                                + timedelta(minutes=notes_interval_minutes)
                            ),
                        )
            classification_batch_size = _validated_int(
                self.environment,
                "HERMES_CLASSIFICATION_BATCH_SIZE",
                20,
                1,
                500,
            )
            classifications = self.classify_due(classification_batch_size)
            dispatched = self.dispatch_due() if mode == "online" else []
            result = {
                "status": "ok",
                "mode": mode,
                "remote": remote,
                "run_id": run_id,
                "full_scan": full_scan,
                "classifications": classifications,
                "dispatched": dispatched,
                "notes": notes,
            }
            self._record_run_finish(
                run_id,
                "completed",
                isoformat(self.now()),
                result=result,
            )
            return result
        except Exception as exc:
            self._record_run_finish(
                run_id,
                "failed",
                isoformat(self.now()),
                error=str(exc),
            )
            raise

    def _next_wakeup_at(self) -> datetime:
        candidates: List[datetime] = []
        with self._connect() as connection:
            runtime_mode = self._state(connection, "runtime_mode")
            retry_value = self._state(connection, "remote_retry_at")
            remote_retry_at = (
                parse_timestamp(retry_value)
                if runtime_mode == "local_only" and retry_value
                else None
            )
            value = self._state(connection, "next_full_scan_at")
            if value:
                scan_at = parse_timestamp(value)
                if remote_retry_at:
                    scan_at = max(scan_at, remote_retry_at)
                candidates.append(scan_at)
            notes_enabled = self.environment.get(
                "HERMES_NOTES_ENABLED", "true"
            ).strip().lower() in {"1", "true", "yes", "on"}
            if notes_enabled and runtime_mode != "local_only":
                notes_scan_at = self._state(connection, "next_notes_scan_at")
                candidates.append(
                    parse_timestamp(notes_scan_at)
                    if notes_scan_at
                    else self.now()
                )
            classification_row = connection.execute(
                """
                SELECT min(classification_due_at) AS due_at
                FROM secondary_lead_state
                WHERE status = 'settling'
                  AND classification_due_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM notes_follow_up_state notes
                    WHERE notes.lead_id = secondary_lead_state.lead_id
                      AND notes.status != 'superseded'
                  )
                """
            ).fetchone()
            if classification_row and classification_row["due_at"]:
                classification_at = parse_timestamp(
                    classification_row["due_at"]
                )
                resume_at = self._state(
                    connection,
                    "classification_backlog_resume_at",
                )
                if resume_at:
                    classification_at = max(
                        classification_at,
                        parse_timestamp(resume_at),
                    )
                candidates.append(classification_at)
            action_row = connection.execute(
                """
                SELECT min(next_action_at) AS due_at
                FROM secondary_lead_state
                WHERE status = 'scheduled'
                  AND next_action_at IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM notes_follow_up_state notes
                    WHERE notes.lead_id = secondary_lead_state.lead_id
                      AND notes.status != 'superseded'
                  )
                """
            ).fetchone()
            if action_row and action_row["due_at"]:
                action_at = parse_timestamp(action_row["due_at"])
                if remote_retry_at:
                    action_at = max(action_at, remote_retry_at)
                candidates.append(action_at)
            if remote_retry_at:
                candidates.append(remote_retry_at)
        return (
            max(min(candidates), self.now())
            if candidates
            else self.now() + timedelta(hours=6)
        )

    def run_forever(self) -> None:
        with self.message_processor.file_lock("secondary-scheduler.lock"):
            self.maintain()
            self._recover_interrupted_runs()
            self._recover_interrupted_classifications()
            self.control_socket_path.unlink(missing_ok=True)
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            control.bind(str(self.control_socket_path))
            self.control_socket_path.chmod(0o600)
            try:
                while True:
                    started = self.now()
                    try:
                        result = self.run_cycle(
                            trigger="scheduler",
                            allow_local_only=True,
                        )
                    except Exception as exc:
                        result = {"status": "error", "error": str(exc)}
                    print(
                        json.dumps(result, ensure_ascii=False, default=str),
                        flush=True,
                    )
                    wake_at = self._next_wakeup_at()
                    timeout = max(
                        0.001,
                        (wake_at - self.now()).total_seconds(),
                    )
                    if result.get("status") == "error" and wake_at <= started:
                        timeout = 60.0
                    control.settimeout(timeout)
                    try:
                        control.recv(64)
                    except socket.timeout:
                        pass
            finally:
                control.close()
                self.control_socket_path.unlink(missing_ok=True)

    def status(self, limit: int = 10) -> Dict[str, Any]:
        with self._connect() as connection:
            counts = self._queue_counts(connection)
            types = {
                (row["lead_type"] or "unclassified"): row["count"]
                for row in connection.execute(
                    """
                    SELECT lead_type, count(*) AS count
                    FROM secondary_lead_state
                    GROUP BY lead_type
                    ORDER BY lead_type
                    """
                )
            }
            rows = connection.execute(
                """
                SELECT lead_id, lead_type, status, classification_confidence,
                       classification_due_at, next_action_at, follow_up_count,
                       latest_message_version_id, last_error, updated_at
                FROM secondary_lead_state
                ORDER BY updated_at DESC, lead_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            scheduler = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM scheduler_state ORDER BY key"
                )
            }
        return {
            "runtime": self._runtime_status(scheduler),
            "counts": counts,
            "lead_types": types,
            "scheduler": scheduler,
            "next_wakeup_at": isoformat(self._next_wakeup_at()),
            "records": [dict(row) for row in rows],
        }

    def queue(
        self,
        limit: int = 50,
        statuses: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise RuntimeError("Queue limit must be between 1 and 500")
        selected = list(dict.fromkeys(statuses or []))
        invalid = set(selected) - QUEUE_STATUSES
        if invalid:
            raise RuntimeError(
                f"Unknown queue status: {', '.join(sorted(invalid))}"
            )
        notes_owned = """
          EXISTS (
            SELECT 1 FROM notes_follow_up_state notes
            WHERE notes.lead_id = secondary_lead_state.lead_id
              AND notes.status != 'superseded'
          )
        """
        where = ""
        parameters: List[Any] = []
        if selected:
            placeholders = ", ".join("?" for _ in selected)
            where = f"WHERE status IN ({placeholders})"
            parameters.extend(selected)
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT lead_id, lead_type, classification_confidence,
                       classification_reason, status, classification_due_at,
                       next_action_at, follow_up_count,
                       latest_message_version_id, last_error, updated_at,
                       CASE WHEN {notes_owned} THEN 1 ELSE 0 END AS notes_owned
                FROM secondary_lead_state
                {where}
                ORDER BY
                  CASE WHEN next_action_at IS NULL THEN 1 ELSE 0 END,
                  next_action_at,
                  classification_due_at,
                  lead_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _queue_counts(connection: sqlite3.Connection) -> Dict[str, int]:
        return {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                SELECT status, count(*) AS count
                FROM secondary_lead_state
                GROUP BY status
                ORDER BY status
                """
            )
        }

    @staticmethod
    def _json_value(value: Optional[str], fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback

    def runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise RuntimeError("Run history limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, trigger, status, started_at, completed_at,
                       result_json, error
                FROM scheduler_run
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            results = []
            for row in rows:
                result = self._json_value(row["result_json"], {})
                classifications = result.get("classifications") or []
                dispatched = result.get("dispatched") or []
                scan = (
                    result.get("full_scan")
                    or result.get("reconciliation")
                    or result.get("recent_scan")
                )
                scan_kind = (
                    "full"
                    if result.get("full_scan") is not None
                    else "120d"
                    if result.get("reconciliation") is not None
                    else "15d"
                    if result.get("recent_scan") is not None
                    else None
                )
                completed_at = row["completed_at"] or isoformat(self.now())
                usage = connection.execute(
                    """
                    SELECT
                      COALESCE(SUM(CAST(json_extract(usage_json, '$.input_tokens')
                        AS INTEGER)), 0) AS input_tokens,
                      COALESCE(SUM(CAST(json_extract(usage_json, '$.output_tokens')
                        AS INTEGER)), 0) AS output_tokens,
                      COALESCE(SUM(CAST(json_extract(usage_json, '$.total_tokens')
                        AS INTEGER)), 0) AS total_tokens,
                      COALESCE(SUM(CAST(json_extract(
                        usage_json, '$.estimated_cost_usd') AS REAL)), 0)
                        AS estimated_cost_usd
                    FROM secondary_lead_classification
                    WHERE classified_at >= ? AND classified_at <= ?
                    """,
                    (row["started_at"], completed_at),
                ).fetchone()
                results.append(
                    {
                        "id": row["id"],
                        "trigger": row["trigger"],
                        "status": row["status"],
                        "mode": result.get("mode", "online"),
                        "started_at": row["started_at"],
                        "completed_at": row["completed_at"],
                        "scan_kind": scan_kind,
                        "scan": scan,
                        "classification_count": len(classifications),
                        "classification_statuses": dict(
                            (
                                status,
                                sum(
                                    1
                                    for item in classifications
                                    if item.get("status") == status
                                ),
                            )
                            for status in sorted(
                                {
                                    item.get("status")
                                    for item in classifications
                                    if item.get("status")
                                }
                            )
                        ),
                        "dispatch_count": len(dispatched),
                        "dispatch_statuses": dict(
                            (
                                status,
                                sum(
                                    1
                                    for item in dispatched
                                    if item.get("status") == status
                                ),
                            )
                            for status in sorted(
                                {
                                    item.get("status")
                                    for item in dispatched
                                    if item.get("status")
                                }
                            )
                        ),
                        "usage": dict(usage),
                        "error": row["error"],
                    }
                )
        return results

    def dashboard(self, days: int = 30, run_limit: int = 10) -> Dict[str, Any]:
        if days < 1 or days > 120:
            raise RuntimeError("Dashboard horizon must be between 1 and 120 days")
        now_text = isoformat(self.now())
        horizon_text = isoformat(self.now() + timedelta(days=days))
        with self._connect() as connection:
            counts = self._queue_counts(connection)
            lead_types = {
                (row["lead_type"] or "unclassified"): row["count"]
                for row in connection.execute(
                    """
                    SELECT lead_type, count(*) AS count
                    FROM secondary_lead_state
                    GROUP BY lead_type
                    ORDER BY lead_type
                    """
                )
            }
            due = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN status = 'settling'
                    AND classification_due_at <= ?
                    AND NOT EXISTS (
                      SELECT 1 FROM notes_follow_up_state notes
                      WHERE notes.lead_id = secondary_lead_state.lead_id
                        AND notes.status != 'superseded'
                    ) THEN 1 ELSE 0 END)
                    AS classifications_due,
                  SUM(CASE WHEN status = 'scheduled'
                    AND next_action_at <= ?
                    AND NOT EXISTS (
                      SELECT 1 FROM notes_follow_up_state notes
                      WHERE notes.lead_id = secondary_lead_state.lead_id
                        AND notes.status != 'superseded'
                    ) THEN 1 ELSE 0 END)
                    AS actions_due
                FROM secondary_lead_state
                """,
                (now_text, now_text),
            ).fetchone()
            upcoming = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT substr(due_at, 1, 10) AS date, kind, count(*) AS count
                    FROM (
                      SELECT classification_due_at AS due_at,
                             'classification' AS kind
                      FROM secondary_lead_state
                      WHERE status = 'settling'
                        AND classification_due_at > ?
                        AND classification_due_at <= ?
                        AND NOT EXISTS (
                          SELECT 1 FROM notes_follow_up_state notes
                          WHERE notes.lead_id = secondary_lead_state.lead_id
                            AND notes.status != 'superseded'
                        )
                      UNION ALL
                      SELECT next_action_at AS due_at, 'action' AS kind
                      FROM secondary_lead_state
                      WHERE status = 'scheduled'
                        AND next_action_at > ?
                        AND next_action_at <= ?
                        AND NOT EXISTS (
                          SELECT 1 FROM notes_follow_up_state notes
                          WHERE notes.lead_id = secondary_lead_state.lead_id
                            AND notes.status != 'superseded'
                        )
                    )
                    GROUP BY substr(due_at, 1, 10), kind
                    ORDER BY date, kind
                    """,
                    (now_text, horizon_text, now_text, horizon_text),
                ).fetchall()
            ]
            scheduler = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM scheduler_state ORDER BY key"
                )
            }
            recent_events = [
                {
                    **dict(row),
                    "details": self._json_value(row["details_json"], {}),
                }
                for row in connection.execute(
                    """
                    SELECT event.lead_id, event.event_type, event.event_at,
                           event.details_json, state.lead_type, state.status
                    FROM secondary_lead_event event
                    LEFT JOIN secondary_lead_state state
                      ON state.lead_id = event.lead_id
                    ORDER BY event.event_at DESC, event.id DESC
                    LIMIT 20
                    """
                ).fetchall()
            ]
        return {
            "generated_at": now_text,
            "runtime": self._runtime_status(scheduler),
            "counts": counts,
            "lead_types": lead_types,
            "due": {
                "classifications": int(due["classifications_due"] or 0),
                "actions": int(due["actions_due"] or 0),
            },
            "scheduler": scheduler,
            "next_wakeup_at": isoformat(self._next_wakeup_at()),
            "upcoming": upcoming,
            "runs": self.runs(run_limit),
            "recent_events": recent_events,
        }

    def lead_detail(self, lead_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            state = connection.execute(
                "SELECT * FROM secondary_lead_state WHERE lead_id = ?",
                (lead_id,),
            ).fetchone()
            if state is None:
                raise KeyError(lead_id)
            state_value = dict(state)
            state_value["crm_snapshot"] = self._json_value(
                state_value.pop("crm_snapshot_json"),
                {},
            )
            events = [
                {
                    **dict(row),
                    "details": self._json_value(row["details_json"], {}),
                }
                for row in connection.execute(
                    """
                    SELECT id, event_type, event_at, details_json
                    FROM secondary_lead_event
                    WHERE lead_id = ?
                    ORDER BY event_at DESC, id DESC
                    LIMIT 100
                    """,
                    (lead_id,),
                ).fetchall()
            ]
            classifications = []
            for row in connection.execute(
                """
                SELECT lead_type, confidence, reason, evidence_json,
                       policy_version,
                       json_extract(
                         result_json,
                         '$.information_completeness'
                       ) AS information_completeness,
                       classified_at
                FROM secondary_lead_classification
                WHERE lead_id = ?
                ORDER BY classified_at DESC
                """,
                (lead_id,),
            ).fetchall():
                value = dict(row)
                value["evidence"] = self._json_value(
                    value.pop("evidence_json"),
                    [],
                )
                classifications.append(value)
            notes_row = connection.execute(
                """
                SELECT latest_note_id, structural_state, status, decision,
                       publication_type, publication_id, crm_snapshot_json,
                       updated_at
                FROM notes_follow_up_state
                WHERE lead_id = ?
                ORDER BY source_created_at DESC, updated_at DESC
                LIMIT 1
                """,
                (lead_id,),
            ).fetchone()
            notes_follow_up = None
            if notes_row is not None:
                notes_record = self._json_value(
                    notes_row["crm_snapshot_json"],
                    {},
                )
                notes = notes_record.get("notes", [])
                if not isinstance(notes, list):
                    notes = []
                notes_follow_up = {
                    "latest_note_id": notes_row["latest_note_id"],
                    "structural_state": notes_row["structural_state"],
                    "status": notes_row["status"],
                    "decision": notes_row["decision"],
                    "publication_type": notes_row["publication_type"],
                    "publication_id": notes_row["publication_id"],
                    "note_count": len(notes),
                    "notes": notes,
                    "updated_at": notes_row["updated_at"],
                }
        return {
            "state": state_value,
            "events": events,
            "classifications": classifications,
            "notes_follow_up": notes_follow_up,
        }


def record_outbox_signal(
    message_version_id: str,
    event: str,
    event_at: Optional[datetime] = None,
) -> bool:
    state_dir = Path(
        os.getenv("HERMES_POLL_STATE_DIR", str(DEFAULT_STATE_DIR))
    )
    scheduler = SecondaryLeadScheduler(
        state_dir=state_dir,
        environment=os.environ,
    )
    return scheduler.handle_outbox_signal(
        str(message_version_id),
        event,
        event_at,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Twenty CRM secondary-lead scheduler"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("once", help="Run one scheduler cycle")
    subparsers.add_parser("run", help="Run the event-driven scheduler")
    subparsers.add_parser("scan", help="Scan all CRM secondary leads")
    classify_parser = subparsers.add_parser(
        "classify",
        help="Classify records whose settling time has elapsed",
    )
    classify_parser.add_argument(
        "--limit",
        type=int,
        default=int(os.getenv("HERMES_CLASSIFICATION_BATCH_SIZE", "20")),
    )
    dispatch_parser = subparsers.add_parser(
        "dispatch",
        help="Generate messages for due secondary leads",
    )
    dispatch_parser.add_argument("--limit", type=int, default=20)
    status_parser = subparsers.add_parser("status", help="Show scheduler state")
    status_parser.add_argument("--limit", type=int, default=10)
    queue_parser = subparsers.add_parser("queue", help="Show secondary-lead queue")
    queue_parser.add_argument("--limit", type=int, default=50)
    signal_parser = subparsers.add_parser(
        "signal",
        help="Apply an Outbox lifecycle signal",
    )
    signal_parser.add_argument("message_version_id")
    signal_parser.add_argument("event", choices=("approved", "sent", "rejected"))
    args = parser.parse_args(argv)

    scheduler = SecondaryLeadScheduler(
        state_dir=Path(
            os.getenv("HERMES_POLL_STATE_DIR", str(DEFAULT_STATE_DIR))
        )
    )
    if args.command in {"once", "scan", "classify", "dispatch"}:
        scheduler.maintain()
    if args.command == "once":
        print(
            json.dumps(
                scheduler.run_cycle(trigger="once"),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    elif args.command == "run":
        scheduler.run_forever()
    elif args.command == "scan":
        print(
            json.dumps(
                scheduler.scan(),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "classify":
        print(
            json.dumps(
                scheduler.classify_due(args.limit),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "dispatch":
        print(
            json.dumps(
                scheduler.dispatch_due(args.limit),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    elif args.command == "status":
        print(
            json.dumps(
                scheduler.status(args.limit),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    elif args.command == "queue":
        print(
            json.dumps(
                scheduler.queue(args.limit),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        changed = scheduler.handle_outbox_signal(
            args.message_version_id,
            args.event,
        )
        print(json.dumps({"updated": changed}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
