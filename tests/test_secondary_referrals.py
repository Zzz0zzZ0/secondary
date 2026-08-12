import copy
import os
import unittest
from pathlib import Path

from app.secondary.classification import validate_classification
from app.secondary.message_policy import MANUAL_CONFIRMATION_WARNING
from app.secondary.message_policy import prepare_message_record
from app.secondary.message_policy import validation_errors
from app.secondary.message_policy import message_route
from app.secondary_scheduler import _needs_policy_reclassification
from review_api.service import _hermes_environment


class SecondaryReferralTest(unittest.TestCase):
    def test_hermes_environment_removes_ipv6_proxy_bypass_tokens(self):
        old = os.environ.get("NO_PROXY")
        try:
            os.environ["NO_PROXY"] = "127.0.0.1,::1,localhost,::1/128"
            self.assertEqual(
                _hermes_environment()["NO_PROXY"], "127.0.0.1,localhost"
            )
        finally:
            if old is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = old

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
        self.assertEqual(prepared["message_route"], "recommender_thanks")

    def test_recommender_normalizes_legacy_recommended_by(self):
        note = "Mario Vicari 告知同事 Lucchi 会负责跟进。"
        candidate = self._candidate("recommender", "Lucchi", note)
        candidate["recommended_by"] = copy.deepcopy(
            candidate["referral_relationship"]["related_contacts"]
        )

        validated = validate_classification(
            candidate, self._record("Mario Vicari", note)
        )

        self.assertEqual(validated["recommended_by"], [])

    def test_policy_treats_colleague_handoff_as_referral(self):
        classification_policy = (
            Path(__file__).resolve().parents[1]
            / "skill"
            / "classify-secondary-lead"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        message_policy = (
            Path(__file__).resolve().parents[1]
            / "skill"
            / "generate-secondary-lead-message"
            / "SKILL.md"
        ).read_text(encoding="utf-8")
        note = "告知后续他的同事：Lucchi会负责跟进"
        candidate = self._candidate("recommender", "Lucchi", note)

        self.assertIn("后续由同事接手", classification_policy)
        self.assertIn("sales team has already contacted", message_policy)
        validated = validate_classification(
            candidate, self._record("Mario Vicari", note)
        )

        self.assertEqual(
            validated["referral_relationship"]["current_contact_role"],
            "recommender",
        )

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
        self.assertEqual(prepared["message_route"], "referred_intro")

    def test_sufficient_evidence_on_secondary_lead_routes_to_qualification(self):
        prepared = prepare_message_record(
            self._record("Customer", "Customer requested a quote for CCM 90%."),
            {
                "lead_type": "unknown_demand",
                "message_evidence": {"status": "sufficient"},
            },
        )
        self.assertEqual(prepared["message_route"], "qualification")

    def test_secondary_lead_evidence_does_not_upgrade_to_inquiry(self):
        prepared = prepare_message_record(
            {
                **self._record("Customer", "客户要求报价。"),
                "lead": {"id": "lead-1", "type": "lead", "raw_type": "未知需求"},
            },
            {"lead_type": "unknown_demand", "message_evidence": {"status": "sufficient"}},
        )
        self.assertEqual(prepared["message_route"], "qualification")

    def test_missing_referral_role_routes_to_review(self):
        prepared = prepare_message_record(
            self._record("Customer", "推荐采购经理 Florian Laux。"),
            {"lead_type": "referred", "message_evidence": {"status": "sufficient"}},
        )
        self.assertEqual(prepared["message_route"], "referral_review")

    def test_recommender_cannot_ask_product_need(self):
        crm_input = {
            "lead": {"id": "lead-1"},
            "output": {"type": "linkedin"},
            "message_route": "recommender_thanks",
        }
        candidate = {
            "decision": "generated",
            "lead_id": "lead-1",
            "output_type": "linkedin",
            "content": {
                "subject": None,
                "subject_zh": None,
                "body": "Thank you for the referral. What products are you sourcing?",
                "body_zh": "感谢您的推荐。您正在采购哪些产品？",
            },
            "message_goal": "感谢推荐",
            "information_requested": [],
            "warnings": [],
            "reason": "感谢推荐并避免询问产品需求。",
            "review_required": True,
        }
        self.assertIn(
            "推荐人消息不得询问产品需求",
            validation_errors(candidate, crm_input, "lead-1"),
        )

    def test_grounded_referral_normalizes_insufficient_message_evidence(self):
        note = "Nathan 由 Sujal Khatiwada 推荐。"
        candidate = self._candidate("referred", "Sujal Khatiwada", note)
        candidate["message_evidence"]["status"] = "insufficient"

        validated = validate_classification(
            candidate, self._record("Nathan", note)
        )

        self.assertEqual(validated["message_evidence"]["status"], "sufficient")

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

    def test_message_reason_must_be_simplified_chinese(self):
        crm_input = {
            "lead": {"id": "lead-1"},
            "output": {"type": "email"},
            "sales": {"name": "倩文 于"},
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
            "reason": "UNKNOWN_DEMAND subtype; asking one qualification question.",
            "review_required": True,
        }

        self.assertIn(
            "判断理由必须使用简体中文",
            validation_errors(candidate, crm_input, "lead-1"),
        )

    def test_insufficient_crm_evidence_creates_review_only_draft(self):
        crm_input = prepare_message_record(
            {
                "lead": {"id": "lead-1"},
                "output": {"type": "email"},
                "sales": {"name": "倩文 于"},
            },
            None,
        )
        candidate = {
            "decision": "generated",
            "lead_id": "lead-1",
            "output_type": "email",
            "content": {
                "subject": "Following up",
                "subject_zh": "跟进确认",
                "body": "Hello,\n\nCould you please share the information you would like us to review?\n\nBest regards,\nChloe\nAceler International",
                "body_zh": "您好，\n\n请告知希望我们协助确认的信息。\n\n此致，\nChloe\nAceler International",
            },
            "message_goal": "请客户补充需要确认的信息。",
            "information_requested": ["需确认的具体信息"],
            "warnings": [MANUAL_CONFIRMATION_WARNING],
            "reason": "CRM缺少可靠跟进时间和明确沟通证据，生成低风险草稿供人工确认。",
            "review_required": True,
        }

        self.assertTrue(crm_input["message_generation_eligibility"]["allowed"])
        self.assertTrue(
            crm_input["message_generation_eligibility"]["requires_manual_confirmation"]
        )
        self.assertEqual(validation_errors(candidate, crm_input, "lead-1"), [])
        candidate["warnings"] = []
        self.assertIn(
            "CRM证据不足时必须标记为需要人工确认",
            validation_errors(candidate, crm_input, "lead-1"),
        )


    def test_v6_reclassifies_unknown_recommend_records_only(self):
        class Row(dict):
            def __getitem__(self, key):
                return super().__getitem__(key)

        referral = Row(
            policy_version="secondary-lead-v5",
            lead_type="unknown_demand",
            crm_snapshot_json='{"lead":{"raw_type":["RECOMMEND"]}}',
        )
        unrelated = Row(
            policy_version="secondary-lead-v5",
            lead_type="unknown_demand",
            crm_snapshot_json='{"lead":{"raw_type":["UNKNOWN_DEMAND"]}}',
        )

        self.assertTrue(_needs_policy_reclassification(referral))
        self.assertFalse(_needs_policy_reclassification(unrelated))
        self.assertTrue(
            _needs_policy_reclassification(
                Row(
                    policy_version=None,
                    lead_type="unknown_demand",
                    crm_snapshot_json="{}",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
