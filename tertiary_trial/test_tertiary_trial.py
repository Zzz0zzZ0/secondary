import unittest
from datetime import date
from unittest.mock import patch

from tertiary_trial.tertiary_trial import classify_record, classify_records, read_crm_rows


def row(**overrides):
    value = {
        "lead_id": "lead-1",
        "lifecycle": "NEW",
        "follow_up_tier": "OPT0",
        "name": "Test Lead",
        "company": "company-1",
        "email": "lead@example.com",
        "linkedin_url": None,
        "last_follow_up": None,
        "last_response": None,
    }
    value.update(overrides)
    return value


class TertiaryTrialTest(unittest.TestCase):
    TODAY = date(2026, 8, 12)

    def test_new_lead_is_uncontacted_only_when_outbox_is_available(self):
        result = classify_record(
            row(), sent_lead_ids=set(), outbox_available=True, today=self.TODAY
        )
        self.assertEqual("uncontacted", result["queue"])

        result = classify_record(
            row(), sent_lead_ids=set(), outbox_available=False, today=self.TODAY
        )
        self.assertEqual("contact_fact_unknown", result["queue"])

    def test_no_reply_uses_follow_up_age(self):
        result = classify_record(
            row(
                lifecycle="NO_REPLY",
                last_follow_up=date(2026, 8, 5),
            ),
            sent_lead_ids=set(),
            outbox_available=True,
            today=self.TODAY,
        )
        self.assertEqual("no_response_due", result["queue"])
        self.assertEqual("6-10d", result["age_bucket"])

    def test_old_no_reply_requires_reactivation_decision(self):
        result = classify_record(
            row(
                lifecycle="NO_REPLY",
                last_follow_up=date(2026, 6, 1),
            ),
            sent_lead_ids=set(),
            outbox_available=True,
            today=self.TODAY,
        )
        self.assertEqual("reactivation_or_close", result["queue"])

    def test_response_and_sent_facts_are_never_sendable(self):
        result = classify_record(
            row(lifecycle="NO_REPLY", last_response=date(2026, 8, 11)),
            sent_lead_ids={"lead-1"},
            outbox_available=True,
            today=self.TODAY,
        )
        self.assertEqual("response_conflict", result["queue"])

    def test_missing_channel_is_actionable_separate_queue(self):
        result = classify_record(
            row(email=None, linkedin_url=None),
            sent_lead_ids=set(),
            outbox_available=True,
            today=self.TODAY,
        )
        self.assertEqual("missing_channel", result["queue"])

    def test_counts_and_samples_are_bounded(self):
        counts, samples = classify_records(
            [row(lead_id="1"), row(lead_id="2"), row(lead_id="3")],
            sent_lead_ids=set(),
            outbox_available=True,
            today=self.TODAY,
            sample_limit=2,
        )
        self.assertEqual(3, counts["uncontacted"])
        self.assertEqual(2, len(samples["uncontacted"]))

    def test_crm_read_requires_workspace_schema(self):
        with patch.dict("os.environ", {"TWENTY_WORKSPACE_SCHEMA": ""}):
            with self.assertRaisesRegex(RuntimeError, "missing or invalid"):
                read_crm_rows()


if __name__ == "__main__":
    unittest.main()
