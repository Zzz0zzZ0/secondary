import copy
import unittest

from app.secondary.classification import validate_classification
from app.secondary.message_policy import prepare_message_record
from app.secondary.message_policy import validation_errors


class SecondaryReferralTest(unittest.TestCase):
    def _candidate(self, role, name, quote):
        related = [{"name": name, "evidence_quote": quote}]
        return {
            "lead_id": "lead-1",
            "lead_type": "referred",
            "confidence": 0.9,
            "information_completeness": 0.7,
            "reason": "备注明确记录了推荐关系。",
            "evidence": [quote],
            "message_evidence": {
                "status": "sufficient",
                "customer_action_quote": quote,
                "business_detail_quote": quote,
                "reason": "推荐关系有原文依据。",
            },
            "contact_permission": {
                "status": "allowed",
                "evidence_quote": None,
            },
            "customer_used_sender_name": None,
            "recommended_by": related if role == "referred" else [],
            "referral_relationship": {
                "current_contact_role": role,
                "related_contacts": related,
            },
            "review_required": True,
        }

    def _record(self, contact, note):
        return {
            "contact": {"name": contact},
            "lead": {"id": "lead-1", "internal_note": note},
        }

    def test_current_contact_can_be_recommender(self):
        note = "Micael 推荐采购经理 Florian Laux。"
        candidate = self._candidate("recommender", "Florian Laux", note)

        validated = validate_classification(
            copy.deepcopy(candidate), self._record("Micael", note)
        )
        prepared = prepare_message_record(
            self._record("Micael", note), validated
        )

        self.assertEqual(
            prepared["referral_context"]["current_contact_role"],
            "recommender",
        )
        self.assertEqual(prepared["lead"]["referred_contacts"], "Florian Laux")
        self.assertNotIn("recommended_by", prepared["lead"])

    def test_current_contact_can_be_referred(self):
        note = "Nathan 由 Sujal Khatiwada 推荐。"
        candidate = self._candidate("referred", "Sujal Khatiwada", note)

        validated = validate_classification(
            copy.deepcopy(candidate), self._record("Nathan", note)
        )
        prepared = prepare_message_record(
            self._record("Nathan", note), validated
        )

        self.assertEqual(
            prepared["referral_context"]["current_contact_role"],
            "referred",
        )
        self.assertEqual(prepared["lead"]["recommended_by"], "Sujal Khatiwada")

    def test_legacy_self_recommender_is_not_treated_as_recommended_by(self):
        note = "推荐采购经理 Florian Laux。"
        legacy = {
            "recommended_by": [
                {"name": "Micael", "evidence_quote": note},
            ],
        }

        prepared = prepare_message_record(
            self._record("Micael", note), legacy
        )

        self.assertEqual(
            prepared["referral_context"]["current_contact_role"],
            "recommender",
        )
        self.assertNotIn("recommended_by", prepared["lead"])

    def test_exact_crm_sender_map_overrides_conversation_name(self):
        crm_input = {
            "lead": {"id": "lead-1"},
            "output": {"type": "email"},
            "sales": {"name": "倩文 于"},
            "conversation_sender_identity": {"name": "Hangke"},
        }
        candidate = {
            "decision": "generated",
            "lead_id": "lead-1",
            "output_type": "email",
            "content": {
                "subject": "Subject",
                "subject_zh": "主题",
                "body": "Hello,\n\nBest regards,\nChloe\nAceler International",
                "body_zh": "您好，\n\n此致，\nChloe\nAceler International",
            },
            "message_goal": "跟进",
            "information_requested": [],
            "warnings": [],
            "reason": "按映射生成。",
            "review_required": True,
        }

        self.assertEqual(validation_errors(candidate, crm_input, "lead-1"), [])


if __name__ == "__main__":
    unittest.main()
