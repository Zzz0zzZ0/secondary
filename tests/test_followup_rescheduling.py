import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app.secondary_scheduler import SecondaryLeadScheduler


class FollowUpReschedulingTest(unittest.TestCase):
    def setUp(self):
        reader = patch("app.secondary_scheduler.read_referral_history", return_value=(None, {"source_hash": "empty"}))
        reader.start()
        self.addCleanup(reader.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.scheduler = SecondaryLeadScheduler(
            state_dir=Path(self.directory.name), environment={}, now=lambda: self.now,
        )
        self.record = {
            "lead": {"id": "lead-1"},
            "contact": {"email": "buyer@example.com"},
            "output": {"type": "email"},
            "source_version": {"record_id": "lead-1", "updated_at": self.now.isoformat()},
        }
        self.scheduler._upsert_discovered(self.record)
        self.scheduler.classification_runner.run = Mock(return_value=({
            "lead_id": "lead-1", "lead_type": "unknown_demand",
            "confidence": 0.9, "reason": "Follow up later", "evidence": [],
            "contact_permission": {"status": "allowed", "evidence_quote": None},
            "sales_follow_up_context": {"status": "sales_replied", "evidence_quote": None},
        }, None))
        self.scheduler._classify_one(self.state())

    def state(self):
        with self.scheduler._connect() as connection:
            return connection.execute("SELECT * FROM secondary_lead_state").fetchone()

    def test_completed_no_reply_releases_only_allowed_followed_conversations(self):
        for permission, has_outbound, expected in (
            ("allowed", True, 1), ("blocked", True, 0),
            ("uncertain", True, 0), ("allowed", False, 0),
        ):
            with self.subTest(permission=permission, has_outbound=has_outbound):
                snapshot = {"notes": [{
                    "direction": "FA", "note_id": "note-1", "channel": "email",
                    "email_at": self.now.isoformat(), "created_at": self.now.isoformat(),
                    "subject": "Follow up", "body": "I will follow up later.",
                }] if has_outbound else []}
                resume_at = (self.now + timedelta(days=100)).isoformat()
                analysis = {"semantic": {"decision": "no_action", "contact_permission": permission, "resume_at": resume_at}}
                with self.scheduler._connect() as connection:
                    connection.execute("DELETE FROM notes_follow_up_state")
                    connection.execute("UPDATE secondary_lead_state SET status='settling', next_action_at=NULL")
                    connection.execute("""INSERT INTO notes_follow_up_state
                        (latest_note_id,lead_id,source_hash,source_created_at,crm_snapshot_json,
                         structural_state,status,decision,analysis_json,created_at,updated_at)
                        VALUES ('note-1','lead-1','note-source',?,?,'needs_analysis',
                                'completed','no_action',?,?,?)""",
                        (self.now.isoformat(), json.dumps(snapshot), json.dumps(analysis),
                         self.now.isoformat(), self.now.isoformat()))
                self.assertEqual(expected, self.scheduler._resume_notes_follow_ups())
                self.assertEqual(0, self.scheduler._resume_notes_follow_ups())
                if expected:
                    state = self.state()
                    self.assertEqual("scheduled", state["status"])
                    self.assertGreater(state["next_action_at"], self.now.isoformat())
                    self.assertEqual(resume_at, state["next_action_at"])
                    self.assertIsNone(self.scheduler._claim_due_action())
                    self.now = datetime.fromisoformat(state["next_action_at"])
                    due = self.scheduler._claim_due_action()
                    self.assertIsNotNone(due)
                    self.assertEqual("conversation_follow_up", self.scheduler._message_input(due)["message_route"])
                else:
                    self.assertEqual("settling", self.state()["status"])

    def test_due_followup_no_message_reschedules_unless_contact_is_blocked(self):
        for warnings, expected in (([], "scheduled"), (["DO_NOT_CONTACT"], "paused")):
            with self.subTest(warnings=warnings):
                self.scheduler._export_current_record = Mock(return_value=self.record)
                self.scheduler.message_processor.process_job = Mock(return_value=None)
                self.scheduler._analysis_result = Mock(return_value={
                    "decision": "no_message", "run_id": "test-run",
                    "result_json": json.dumps({"reason": "Already followed up", "warnings": warnings}),
                })
                with self.scheduler._connect() as connection:
                    connection.execute("UPDATE secondary_lead_state SET status='scheduled',generation_retry_at=NULL,next_action_at=?", (self.now.isoformat(),))
                result = self.scheduler._dispatch_one(self.scheduler._claim_due_action())
                self.assertEqual(expected, result["status"])
                state = self.state()
                self.assertEqual(expected, state["status"])
                if expected == "scheduled":
                    self.assertGreater(state["next_action_at"], self.now.isoformat())
                else:
                    self.assertIsNone(state["next_action_at"])



if __name__ == "__main__":
    unittest.main()
