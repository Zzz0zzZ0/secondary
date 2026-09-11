import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.secondary_scheduler import SecondaryLeadScheduler


class MessageJobRecoveryTest(unittest.TestCase):
    def test_reclassification_with_same_due_date_gets_a_fresh_message_job(self):
        now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        record = {
            "lead": {"id": "lead-1", "next_follow_up_at": now.isoformat()},
            "source_version": {"record_id": "lead-1", "updated_at": now.isoformat()},
        }
        candidate = {
            "lead_id": "lead-1", "lead_type": "unknown_demand",
            "confidence": 0.9, "reason": "Needs qualification", "evidence": [],
            "contact_permission": {"status": "allowed", "evidence_quote": None},
        }
        for previous_status in ("failed", "completed"):
            with self.subTest(previous_status=previous_status), tempfile.TemporaryDirectory() as directory:
                scheduler = SecondaryLeadScheduler(
                    state_dir=Path(directory), environment={}, now=lambda: now,
                )
                scheduler.classification_runner.run = lambda *_: (candidate, None)
                scheduler._upsert_discovered(record)

                def state():
                    with scheduler._connect() as connection:
                        return connection.execute(
                            "SELECT * FROM secondary_lead_state WHERE lead_id = 'lead-1'"
                        ).fetchone()

                scheduler._classify_one(state())
                first_due = state()["next_action_at"]
                old_job = scheduler._enqueue_message(state(), scheduler._message_input(state()))
                with scheduler._connect() as connection:
                    connection.execute(
                        "UPDATE analysis_job SET status = ?, last_error = ? WHERE id = ?",
                        (previous_status, "Superseded by current message input" if previous_status == "failed" else None, old_job),
                    )
                now += timedelta(days=1)
                scheduler._classify_one(state())
                self.assertEqual(first_due, state()["next_action_at"])
                current_input = scheduler._message_input(state())
                new_job = scheduler._enqueue_message(state(), current_input)

                self.assertNotEqual(old_job, new_job)
                self.assertEqual(new_job, scheduler._enqueue_message(state(), current_input))
                claimed = scheduler.message_processor._claim_job(new_job)
                self.assertIsNotNone(claimed)
                self.assertEqual(1, claimed["attempt_count"])

    def test_maintain_recovers_interrupted_jobs_and_due_retries(self):
        now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        old = now - timedelta(hours=2)
        fresh = now - timedelta(minutes=5)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            with scheduler._connect() as connection:
                for lead_id, job_id, lead_status, job_status, started_at, retry_at in (
                    (
                        "interrupted-lead",
                        "interrupted-job",
                        "generating",
                        "processing",
                        old,
                        None,
                    ),
                    (
                        "expired-retry-lead",
                        "expired-retry-job",
                        "generating",
                        "retry_wait",
                        None,
                        now - timedelta(minutes=1),
                    ),
                    (
                        "active-lead",
                        "active-job",
                        "generating",
                        "processing",
                        fresh,
                        None,
                    ),
                ):
                    connection.execute(
                        """
                        INSERT INTO secondary_lead_state
                          (lead_id, source_hash, source_updated_at,
                           source_record_id, crm_snapshot_json, status,
                           latest_analysis_job_id, last_seen_at, created_at,
                           updated_at)
                        VALUES (?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)
                        """,
                        (
                            lead_id,
                            f"state-source-{lead_id}",
                            now.isoformat(),
                            lead_id,
                            lead_status,
                            job_id,
                            now.isoformat(),
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO analysis_job
                          (id, lead_id, source_hash, source_updated_at,
                           source_record_id, status, attempt_count,
                           next_attempt_at, input_path, run_root, created_at,
                           processing_started_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            lead_id,
                            f"job-source-{lead_id}",
                            now.isoformat(),
                            lead_id,
                            job_status,
                            retry_at.isoformat() if retry_at else None,
                            str(Path(directory) / job_id / "input.json"),
                            str(Path(directory) / job_id / "runs"),
                            old.isoformat(),
                            started_at.isoformat() if started_at else None,
                            now.isoformat(),
                        ),
                    )

            result = scheduler.maintain()

            self.assertEqual(2, result["message_jobs_recovered"])
            with scheduler._connect() as connection:
                jobs = connection.execute(
                    """
                    SELECT id, status, processing_started_at
                    FROM analysis_job
                    ORDER BY id
                    """
                ).fetchall()
                states = connection.execute(
                    """
                    SELECT lead_id, status, next_action_at
                    FROM secondary_lead_state
                    ORDER BY lead_id
                    """
                ).fetchall()

            self.assertEqual(
                [
                    ("active-job", "processing", fresh.isoformat()),
                    ("expired-retry-job", "retry_wait", None),
                    ("interrupted-job", "pending", None),
                ],
                [tuple(row) for row in jobs],
            )
            self.assertEqual(
                [
                    ("active-lead", "generating", None),
                    ("expired-retry-lead", "scheduled", now.isoformat()),
                    ("interrupted-lead", "scheduled", now.isoformat()),
                ],
                [tuple(row) for row in states],
            )

            now = now + timedelta(minutes=31)
            self.assertEqual(1, scheduler._recover_interrupted_message_jobs())
            with scheduler._connect() as connection:
                active_job = connection.execute(
                    """
                    SELECT status FROM analysis_job WHERE id = 'active-job'
                    """
                ).fetchone()
            self.assertEqual("pending", active_job["status"])

    def test_enqueue_retires_job_for_previous_message_input(self):
        now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            with scheduler._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at,
                       source_record_id, crm_snapshot_json, lead_type, status,
                       next_action_at, latest_analysis_job_id, last_seen_at,
                       created_at, updated_at)
                    VALUES ('lead-1', 'current-source', ?, 'lead-1', '{}',
                            'unknown_demand', 'generating', ?, 'old-job', ?, ?, ?)
                    """,
                    (
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO analysis_job
                      (id, lead_id, source_hash, source_updated_at,
                       source_record_id, status, input_path, run_root,
                       created_at, updated_at)
                    VALUES ('old-job', 'lead-1', 'old-message-input', ?,
                            'lead-1', 'retry_wait', ?, ?, ?, ?)
                    """,
                    (
                        now.isoformat(),
                        str(Path(directory) / "old-input.json"),
                        str(Path(directory) / "old-runs"),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM secondary_lead_state WHERE lead_id = 'lead-1'"
                ).fetchone()

            job_id = scheduler._enqueue_message(row, {"lead": {"id": "lead-1"}})

            self.assertNotEqual("old-job", job_id)
            with scheduler._connect() as connection:
                jobs = connection.execute(
                    "SELECT id, status, last_error FROM analysis_job ORDER BY id"
                ).fetchall()
                state = connection.execute(
                    """
                    SELECT latest_analysis_job_id FROM secondary_lead_state
                    WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
            self.assertEqual(
                [
                    (job_id, "pending", None),
                    ("old-job", "failed", "Superseded by current message input"),
                ],
                [tuple(job) for job in jobs],
            )
            self.assertEqual(job_id, state["latest_analysis_job_id"])

    def test_classification_retires_previous_message_job(self):
        now = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)
        candidate = {
            "lead_id": "lead-1",
            "lead_type": "unknown_demand",
            "confidence": 0.9,
            "reason": "No current product details are available.",
            "evidence": [],
            "contact_permission": {"status": "allowed", "evidence_quote": None},
        }
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory),
                environment={},
                now=lambda: now,
            )
            scheduler.classification_runner.run = lambda _record, _run_dir: (
                candidate,
                None,
            )
            with scheduler._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO secondary_lead_state
                      (lead_id, source_hash, source_updated_at,
                       source_record_id, crm_snapshot_json, status,
                       latest_analysis_job_id, last_seen_at, created_at,
                       updated_at)
                    VALUES ('lead-1', 'source-1', ?, 'lead-1', '{}',
                            'classifying', 'old-job', ?, ?, ?)
                    """,
                    (
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO analysis_job
                      (id, lead_id, source_hash, source_updated_at,
                       source_record_id, status, input_path, run_root,
                       created_at, updated_at)
                    VALUES ('old-job', 'lead-1', 'old-message-input', ?,
                            'lead-1', 'pending', ?, ?, ?, ?)
                    """,
                    (
                        now.isoformat(),
                        str(Path(directory) / "old-input.json"),
                        str(Path(directory) / "old-runs"),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM secondary_lead_state WHERE lead_id = 'lead-1'"
                ).fetchone()

            scheduler._classify_one(row)

            with scheduler._connect() as connection:
                job = connection.execute(
                    "SELECT status, last_error FROM analysis_job WHERE id = 'old-job'"
                ).fetchone()
                state = connection.execute(
                    """
                    SELECT latest_analysis_job_id FROM secondary_lead_state
                    WHERE lead_id = 'lead-1'
                    """
                ).fetchone()
            self.assertEqual("failed", job["status"])
            self.assertEqual(
                "Superseded by current classification",
                job["last_error"],
            )
            self.assertIsNone(state["latest_analysis_job_id"])


if __name__ == "__main__":
    unittest.main()
