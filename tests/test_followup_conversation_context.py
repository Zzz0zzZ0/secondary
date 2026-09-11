import json
import unittest

from tests import test_followup_rescheduling as fixtures
from app.secondary.validate_candidate import main as validate_candidate


class FollowUpConversationContextTest(unittest.TestCase):
    setUp = fixtures.FollowUpReschedulingTest.setUp
    state = fixtures.FollowUpReschedulingTest.state

    def test_linked_recommender_survives_notes_followup_route(self):
        record = json.loads(self.state()["crm_snapshot_json"])
        record["crm_referral"] = {"recommended_by": [], "referred_contacts": [{"id": "buyer-2", "name": "Other Buyer"}]}
        with self.scheduler._connect() as connection:
            connection.execute("UPDATE secondary_lead_state SET crm_snapshot_json=?", (json.dumps(record),))
            connection.execute("""INSERT INTO notes_follow_up_state
                (latest_note_id,lead_id,source_hash,source_created_at,crm_snapshot_json,
                 structural_state,status,created_at,updated_at)
                VALUES ('card','lead-1','notes-hash',?,'{}','waiting_customer','skipped',?,?)""",
                (self.now.isoformat(), self.now.isoformat(), self.now.isoformat()))
        prepared = self.scheduler._message_input(self.state())
        self.assertEqual("referral_handoff", prepared["message_route"])
        self.assertEqual("Other Buyer", prepared["referral_context"]["related_contacts"][0]["name"])

    def test_provider_quota_error_is_not_reported_as_malformed_json(self):
        root = self.scheduler.state_dir
        (root / "input.json").write_text(json.dumps(self.record))
        (root / "raw.txt").write_text("API call failed after 3 retries: HTTP 429: quota exceeded (2056)")
        result = validate_candidate([
            "--input", str(root / "input.json"), "--raw", str(root / "raw.txt"),
            "--candidate-output", str(root / "candidate.json"),
            "--output", str(root / "output.json"), "--errors", str(root / "errors.json"),
        ])
        self.assertEqual(1, result)
        self.assertEqual(["Hermes provider quota exceeded (HTTP 429 / 2056)"], json.loads((root / "errors.json").read_text()))
        self.assertFalse((root / "output.json").exists())
    def test_notes_history_and_channel_reach_generation_and_review(self):
        history = [
            {"note_id": "card:1", "direction": "SHOU", "channel": "linkedin",
             "email_at": None, "created_at": self.now.isoformat(), "subject": None,
             "body": "We use magnesia bricks; please follow up next month."},
            {"note_id": "card:2", "direction": "FA", "channel": "linkedin",
             "email_at": self.now.isoformat(), "created_at": self.now.isoformat(),
             "subject": None, "body": "I will check back next month."},
        ]
        snapshot = {"notes": history, "contact_linkedin_url": "https://www.linkedin.com/in/buyer"}
        with self.scheduler._connect() as connection:
            connection.execute("""INSERT INTO notes_follow_up_state
                (latest_note_id,lead_id,source_hash,source_created_at,crm_snapshot_json,
                 structural_state,status,created_at,updated_at)
                VALUES ('card','lead-1','notes-v1',?,?,'waiting_customer','skipped',?,?)""",
                (self.now.isoformat(),json.dumps(snapshot),self.now.isoformat(),self.now.isoformat()))
        record = self.scheduler._message_input(self.state())
        self.assertEqual("linkedin", record["output"]["type"])
        self.assertEqual("Customer language", record["output"]["default_language"])
        self.assertEqual(snapshot["contact_linkedin_url"], record["contact"]["linkedin_url"])
        self.assertEqual([n["body"] for n in history], [n["body"] for n in record["conversation_history"]])
        self.assertEqual(record["conversation_history"], record["review_context"]["crm_email_history"])
        self.assertEqual("crm.note.linkedin", record["sales_follow_up_context"]["source"])
        self.assertEqual(history[-1]["body"], record["sales_follow_up_context"]["evidence_quote"])
        # The actual pipeline removes review_context before constructing the prompt.
        del record["review_context"]
        self.assertEqual(2, len(record["conversation_history"]))
        old_job = self.scheduler._enqueue_message(self.state(), record)
        with self.scheduler._connect() as connection:
            connection.execute("UPDATE notes_follow_up_state SET source_hash='notes-v2'")
        updated = self.scheduler._message_input(self.state())
        self.assertNotEqual(old_job, self.scheduler._enqueue_message(self.state(), updated))
