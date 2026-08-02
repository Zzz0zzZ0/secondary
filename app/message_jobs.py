import fcntl
import json
import os
import re
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = PROJECT_DIR / "outputs" / "poller"
DEFAULT_PIPELINE_SCRIPT = PROJECT_DIR / "scripts" / "twenty_run_hermes_pipeline.sh"
RETRY_DELAYS_MINUTES = (5, 20, 60)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    if re.search(r"[+-]\d{2}$", normalized):
        normalized += ":00"
    elif re.search(r"[+-]\d{4}$", normalized):
        normalized = normalized[:-2] + ":" + normalized[-2:]
    fractional = re.match(
        r"^(.*?)[.,](\d+)([+-]\d{2}:\d{2})?$",
        normalized,
    )
    if fractional:
        microseconds = (fractional.group(2) + "000000")[:6]
        normalized = (
            f"{fractional.group(1)}.{microseconds}"
            f"{fractional.group(3) or ''}"
        )
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class JobProcessorBusy(RuntimeError):
    pass


class MessageJobProcessor:
    def __init__(
        self,
        state_dir: Path = DEFAULT_STATE_DIR,
        pipeline_script: Path = DEFAULT_PIPELINE_SCRIPT,
        environment: Optional[Dict[str, str]] = None,
        now: Callable[[], datetime] = utc_now,
    ):
        self.state_dir = state_dir.resolve()
        self.pipeline_script = pipeline_script.resolve()
        self.environment = dict(environment if environment is not None else os.environ)
        self.now = now
        self.db_path = self.state_dir / "poller.sqlite3"
        self.jobs_dir = self.state_dir / "jobs"
        self.reports_dir = self.state_dir / "reports"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        self.jobs_dir.chmod(0o700)
        self.reports_dir.chmod(0o700)
        self._initialize_database()
        self.db_path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_database(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS analysis_job (
                  id TEXT PRIMARY KEY,
                  lead_id TEXT NOT NULL,
                  source_hash TEXT NOT NULL,
                  source_updated_at TEXT NOT NULL,
                  source_record_id TEXT NOT NULL,
                  status TEXT NOT NULL
                    CHECK (status IN (
                      'pending', 'processing', 'retry_wait', 'completed', 'failed'
                    )),
                  attempt_count INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT,
                  input_path TEXT NOT NULL,
                  run_root TEXT NOT NULL,
                  summary_path TEXT,
                  last_error TEXT,
                  created_at TEXT NOT NULL,
                  processing_started_at TEXT,
                  completed_at TEXT,
                  updated_at TEXT NOT NULL,
                  UNIQUE (lead_id, source_hash)
                );

                CREATE INDEX IF NOT EXISTS analysis_job_runnable_idx
                  ON analysis_job (status, next_attempt_at, created_at);

                CREATE TABLE IF NOT EXISTS analysis_result (
                  id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL UNIQUE
                    REFERENCES analysis_job(id),
                  lead_id TEXT NOT NULL,
                  source_hash TEXT NOT NULL,
                  source_updated_at TEXT NOT NULL,
                  schema_version TEXT NOT NULL DEFAULT '1.0',
                  decision TEXT NOT NULL
                    CHECK (decision IN (
                      'generated', 'no_message', 'cannot_generate'
                    )),
                  output_type TEXT NOT NULL
                    CHECK (output_type IN ('email', 'linkedin')),
                  language TEXT,
                  subject TEXT,
                  subject_zh TEXT,
                  body TEXT,
                  body_zh TEXT,
                  message_goal TEXT,
                  information_requested_json TEXT NOT NULL DEFAULT '[]',
                  warnings_json TEXT NOT NULL DEFAULT '[]',
                  reason TEXT NOT NULL,
                  review_required INTEGER NOT NULL DEFAULT 1
                    CHECK (review_required = 1),
                  crm_snapshot_json TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  usage_json TEXT,
                  run_id TEXT,
                  model TEXT,
                  generated_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS analysis_result_lead_idx
                  ON analysis_result (lead_id, generated_at DESC);

                CREATE INDEX IF NOT EXISTS analysis_result_decision_idx
                  ON analysis_result (decision, generated_at DESC);

                CREATE INDEX IF NOT EXISTS analysis_result_channel_idx
                  ON analysis_result (output_type, generated_at DESC);
                """
            )

    @contextmanager
    def file_lock(self, name: str):
        lock_path = self.state_dir / name
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JobProcessorBusy(
                    f"Job processor lock is already held: {lock_path}"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _claim_job(self, job_id: Optional[str] = None) -> Optional[sqlite3.Row]:
        now_text = isoformat(self.now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if job_id is None:
                row = connection.execute(
                    """
                    SELECT *
                    FROM analysis_job
                    WHERE status = 'pending'
                       OR (status = 'retry_wait' AND next_attempt_at <= ?)
                    ORDER BY created_at, id
                    LIMIT 1
                    """,
                    (now_text,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT *
                    FROM analysis_job
                    WHERE id = ?
                      AND (
                        status = 'pending'
                        OR (status = 'retry_wait' AND next_attempt_at <= ?)
                      )
                    """,
                    (job_id, now_text),
                ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE analysis_job
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    processing_started_at = ?,
                    next_attempt_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_text, now_text, row["id"]),
            )
            return connection.execute(
                "SELECT * FROM analysis_job WHERE id = ?",
                (row["id"],),
            ).fetchone()

    def _pipeline_environment(self, row: sqlite3.Row) -> Dict[str, str]:
        environment = self.environment.copy()
        environment.update(
            {
                "TWENTY_HERMES_INPUT_FILE": row["input_path"],
                "TWENTY_HERMES_PROCESS_LIMIT": "1",
                "TWENTY_HERMES_RUNS_DIR": row["run_root"],
                "TWENTY_HERMES_REPORTS_DIR": str(self.reports_dir),
                "OUTBOX_AUTO_IMPORT": self.environment.get(
                    "HERMES_POLL_OUTBOX_AUTO_IMPORT",
                    "false",
                ).lower(),
                "GMAIL_SEND_ENABLED": "false",
                "EMAIL_LIVE_SEND_ENABLED": "false",
            }
        )
        return environment

    def _mark_failure(self, row: sqlite3.Row, error: str) -> Dict[str, Any]:
        max_attempts = int(self.environment.get("HERMES_POLL_MAX_ATTEMPTS", "3"))
        if max_attempts < 1 or max_attempts > len(RETRY_DELAYS_MINUTES):
            raise RuntimeError(
                f"HERMES_POLL_MAX_ATTEMPTS must be between 1 and {len(RETRY_DELAYS_MINUTES)}"
            )
        attempt_count = int(row["attempt_count"])
        now = self.now()
        if attempt_count >= max_attempts:
            status = "failed"
            next_attempt_at = None
        else:
            status = "retry_wait"
            next_attempt_at = isoformat(
                now + timedelta(minutes=RETRY_DELAYS_MINUTES[attempt_count - 1])
            )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE analysis_job
                SET status = ?,
                    next_attempt_at = ?,
                    last_error = ?,
                    processing_started_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    next_attempt_at,
                    error[-4000:],
                    isoformat(now),
                    row["id"],
                ),
            )
        return {
            "job_id": row["id"],
            "lead_id": row["lead_id"],
            "status": status,
            "attempt_count": attempt_count,
            "next_attempt_at": next_attempt_at,
            "error": error[-1000:],
        }

    @staticmethod
    def _read_json(path: Path, required: bool = True) -> Optional[Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if required:
                raise RuntimeError(f"Required Hermes artifact is missing: {path}")
            return None
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON artifact {path}: {exc}") from exc

    def _result_artifacts(
        self,
        row: sqlite3.Row,
        summary_path: Path,
    ) -> Dict[str, Any]:
        run_dir = summary_path.parent
        record_dirs = sorted(
            child for child in (run_dir / "records").glob("*") if child.is_dir()
        )
        if len(record_dirs) != 1:
            raise RuntimeError("Hermes run must contain exactly one record directory")
        record_dir = record_dirs[0]
        crm_snapshot = self._read_json(record_dir / "input.json")
        output = self._read_json(record_dir / "generated.json")
        usage = self._read_json(record_dir / "usage.json", required=False)
        review = self._read_json(record_dir / "review.json", required=False) or {}
        summary = self._read_json(summary_path)
        if not isinstance(crm_snapshot, dict) or not isinstance(output, dict):
            raise RuntimeError("Hermes result artifacts must be JSON objects")
        if output.get("lead_id") != row["lead_id"]:
            raise RuntimeError("Hermes result lead_id does not match the polling job")
        decision = output.get("decision")
        output_type = output.get("output_type")
        if decision not in {"generated", "no_message", "cannot_generate"}:
            raise RuntimeError("Hermes result decision is invalid")
        if output_type not in {"email", "linkedin"}:
            raise RuntimeError("Hermes result output_type is invalid")
        if output.get("review_required") is not True:
            raise RuntimeError("Hermes result must require human review")
        content = output.get("content")
        if not isinstance(content, dict):
            raise RuntimeError("Hermes result content is missing")
        reason = output.get("reason")
        if not isinstance(reason, str) or not reason:
            raise RuntimeError("Hermes result reason is missing")
        information_requested = output.get("information_requested")
        warnings = output.get("warnings")
        if not isinstance(information_requested, list) or not isinstance(warnings, list):
            raise RuntimeError("Hermes result list fields are invalid")
        generated_at = (
            review.get("generated_at")
            or summary.get("completed_at")
            or isoformat(self.now())
        )
        return {
            "crm_snapshot": crm_snapshot,
            "output": output,
            "usage": usage if isinstance(usage, dict) else None,
            "summary": summary,
            "decision": decision,
            "output_type": output_type,
            "content": content,
            "reason": reason,
            "information_requested": information_requested,
            "warnings": warnings,
            "generated_at": str(generated_at),
        }

    def _store_result_and_complete(
        self,
        row: sqlite3.Row,
        summary_path: Path,
        artifacts: Dict[str, Any],
    ) -> str:
        output = artifacts["output"]
        content = artifacts["content"]
        crm_snapshot = artifacts["crm_snapshot"]
        usage = artifacts["usage"]
        summary = artifacts["summary"]
        result_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"twenty-hermes-result:{row['id']}",
            )
        )
        now_text = isoformat(self.now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO analysis_result
                  (id, job_id, lead_id, source_hash, source_updated_at,
                   schema_version, decision, output_type, language,
                   subject, subject_zh, body, body_zh, message_goal,
                   information_requested_json, warnings_json, reason,
                   review_required, crm_snapshot_json, result_json, usage_json,
                   run_id, model, generated_at, created_at)
                VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1,
                  ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT (job_id) DO NOTHING
                """,
                (
                    result_id,
                    row["id"],
                    row["lead_id"],
                    row["source_hash"],
                    row["source_updated_at"],
                    str(crm_snapshot.get("schema_version") or "1.0"),
                    artifacts["decision"],
                    artifacts["output_type"],
                    output.get("language"),
                    content.get("subject"),
                    content.get("subject_zh"),
                    content.get("body"),
                    content.get("body_zh"),
                    output.get("message_goal"),
                    json.dumps(
                        artifacts["information_requested"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        artifacts["warnings"],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    artifacts["reason"],
                    json.dumps(
                        crm_snapshot,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    json.dumps(
                        output,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    (
                        json.dumps(
                            usage,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if usage is not None
                        else None
                    ),
                    summary.get("run_id"),
                    usage.get("model") if usage else None,
                    artifacts["generated_at"],
                    now_text,
                ),
            )
            connection.execute(
                """
                UPDATE analysis_job
                SET status = 'completed',
                    summary_path = ?,
                    last_error = NULL,
                    processing_started_at = NULL,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(summary_path), now_text, now_text, row["id"]),
            )
        return result_id

    def process_job(self, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        row = self._claim_job(job_id)
        if row is None:
            return None
        if not self.pipeline_script.is_file():
            return self._mark_failure(
                row,
                f"Hermes pipeline script is missing: {self.pipeline_script}",
            )
        run_root = Path(row["run_root"])
        run_root.mkdir(parents=True, exist_ok=True)
        log_path = run_root.parent / f"pipeline-attempt-{row['attempt_count']}.log"
        try:
            result = subprocess.run(
                ["bash", str(self.pipeline_script)],
                cwd=PROJECT_DIR,
                env=self._pipeline_environment(row),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return self._mark_failure(row, f"Could not start Hermes pipeline: {exc}")
        log_path.write_text(
            (result.stdout or "") + (result.stderr or ""),
            encoding="utf-8",
        )
        summaries = sorted(
            run_root.glob("*/summary.json"),
            key=lambda path: path.stat().st_mtime,
        )
        if result.returncode != 0 or not summaries:
            detail = (result.stderr or result.stdout or "").strip()
            return self._mark_failure(
                row,
                detail or f"Hermes pipeline exited with code {result.returncode}",
            )
        summary_path = summaries[-1]
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return self._mark_failure(row, f"Invalid Hermes summary: {exc}")
        if summary.get("record_count") != 1 or summary.get("valid_count") != 1:
            return self._mark_failure(
                row,
                "Hermes output did not pass validation",
            )
        try:
            artifacts = self._result_artifacts(row, summary_path)
            result_id = self._store_result_and_complete(row, summary_path, artifacts)
        except (OSError, RuntimeError, sqlite3.DatabaseError) as exc:
            return self._mark_failure(row, f"Could not store Hermes result: {exc}")
        return {
            "job_id": row["id"],
            "result_id": result_id,
            "lead_id": row["lead_id"],
            "status": "completed",
            "attempt_count": row["attempt_count"],
            "summary_path": str(summary_path),
        }
