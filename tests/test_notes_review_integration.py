import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from app.secondary.message_policy import validation_errors


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "notes_trial" / "notes_trial.py"
)


def load_notes_trial():
    spec = importlib.util.spec_from_file_location("notes_trial_for_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


notes_trial = load_notes_trial()


class NotesReviewIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            "lead_id": "person-1",
            "contact_name": "Mr. Buyer",
            "contact_email": "buyer@example.com",
            "company_name": "Example Materials",
            "life_cycle": "QUALIFIED",
            "sales_name": "倩文 于",
            "notes": [],
        }
        self.latest = {
            "note_id": "note-1",
            "direction": "SHOU",
            "email_at": "2026-08-20T10:00:00+00:00",
            "created_at": "2026-08-20T10:01:00+00:00",
            "subject": "Re: Specs",
            "sender_name": "Buddy",
            "sender_account": "buddy@okgmineral.com",
            "body": "## 最新邮件原文\nCould you send the current specification?",
        }
        self.record["notes"] = [self.latest]
        self.state = {
            "state": "needs_analysis",
            "reason": "最后一封可靠邮件为客户来信，需要判断是否应回复",
            "latest": self.latest,
        }
        self.semantic = {
            "contact_permission": "allowed",
            "decision": "reply",
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "confidence": 0.9,
            "reason": "客户请求规格资料，需要直接回复并进入人工审阅。",
            "evidence_index": 0,
            "evidence_quote": "Could you send the current specification?",
            "resume_at": None,
            "review_required": True,
        }
        self.draft = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "language": "English",
            "content": {
                "subject": "Re: Specs",
                "subject_zh": "回复：规格",
                "body": "Hello Mr. Buyer,\n\nWe noted your request and will review it internally.\n\nBuddy",
                "body_zh": "您好，Buyer先生：\n\n我们已记录您的请求，将在内部确认后跟进。\n\nBuddy",
            },
        }
        self.analysis = {"semantic": self.semantic, "draft": self.draft}

    def test_crm_referral_links_reach_both_prompts_and_review_snapshot(self):
        self.record["crm_referral"] = {"recommended_by": [], "referred_contacts": [{"id": "other-buyer", "name": "Related Buyer", "email": "other@example.com"}]}
        snapshot = notes_trial.build_notes_review_snapshot(self.record, self.state, self.semantic)
        self.assertEqual("recommender", snapshot["referral_context"]["current_contact_role"])
        self.assertEqual("buyer@example.com", snapshot["contact"]["email"])
        self.assertIn('"recommender"', notes_trial._semantic_prompt(self.record, self.latest, "Buddy"))
        self.assertIn('"other-buyer"', notes_trial._draft_prompt(self.record, self.latest, "Buddy", self.semantic))

    def test_publish_builds_review_ui_payload_and_only_calls_pending_hook(self):
        created = Mock(return_value="message-version-1")
        with patch.object(notes_trial, "create_review_message", created):
            result = notes_trial.publish_review(self.record, self.state, self.analysis)

        self.assertEqual("pending_review", result["status"])
        self.assertEqual("message-version-1", result["message_version_id"])
        run_id, lead_id, snapshot, output = created.call_args.args
        self.assertEqual("person-1", lead_id)
        self.assertEqual(run_id, notes_trial.notes_review_run_id("note-1"))
        self.assertFalse(snapshot["test_mode"])
        self.assertTrue(snapshot["notes_review_only"])
        self.assertNotIn("notes_experiment", snapshot)
        self.assertEqual({"id": "person-1", "type": "secondary_notes_reply"}, snapshot["lead"])
        self.assertEqual("buyer@example.com", snapshot["contact"]["email"])
        self.assertEqual("email", snapshot["output"]["type"])
        self.assertEqual({"name": "倩文 于"}, snapshot["sales"])
        self.assertEqual(
            "Buddy",
            snapshot["conversation_sender_identity"]["name"],
        )
        self.assertEqual(
            "buddy@okgmineral.com",
            snapshot["conversation_sender_identity"]["account"],
        )
        self.assertEqual(
            notes_trial.NOTES_REVIEW_WARNINGS,
            snapshot["warnings"],
        )
        self.assertEqual("reply", snapshot["conversation_action"])
        self.assertNotIn("email_reply_context", snapshot)
        self.assertEqual(
            "Could you send the current specification?",
            snapshot["review_context"]["crm_email_evidence"]["evidence_quote"],
        )
        self.assertEqual("crm.note", snapshot["source_version"]["source"])
        self.assertEqual("generated", output["decision"])
        self.assertEqual("email", output["output_type"])
        self.assertEqual("回复客户最新邮件", output["message_goal"])
        self.assertEqual([], output["information_requested"])
        self.assertTrue(output["review_required"])
        self.assertEqual(self.draft["content"], output["content"])
        self.assertEqual(notes_trial.NOTES_REVIEW_WARNINGS, output["warnings"])
        self.assertNotIn("email_evidence", output)

    def test_linkedin_card_parses_and_publishes_on_linkedin(self):
        self.assertEqual(
            "https://www.linkedin.com/in/mr-buyer/",
            notes_trial._normalized_linkedin_url(
                "www.linkedin.com/in/mr-buyer/?trk=crm"
            ),
        )
        notes = notes_trial.parse_linkedin_context_note(
            {
                "note_id": "linkedin-note-1",
                "note_title": "LinkedIn沟通 01 | Mr. Buyer | 2026-08-25",
                "note_created_at": "2026-08-25T03:44:15+00:00",
                "created_by_context": {
                    "source": "crm.note.linkedin",
                    "captured_at": "2026-08-25T03:44:15Z",
                },
                "body": """# LinkedIn 沟通上下文

## 对话

### 1 · 我方 · FRIDAY

> Hello

### 2 · 客户 · 2:16 PM

> Please send the current specification.
""",
            }
        )
        self.assertEqual(["FA", "SHOU"], [note["direction"] for note in notes])
        self.assertEqual("linkedin", notes[-1]["channel"])
        state = notes_trial.structural_state(notes)
        self.assertEqual("needs_analysis", state["state"])

        self.record["contact_email"] = None
        self.record["contact_linkedin_url"] = (
            "https://www.linkedin.com/in/mr-buyer/"
        )
        self.record["notes"] = notes
        self.latest = notes[-1]
        self.state = state
        self.semantic.update(
            {
                "latest_note_id": self.latest["note_id"],
                "evidence_quote": "Please send the current specification.",
            }
        )
        self.draft.update(
            {
                "latest_note_id": self.latest["note_id"],
                "content": {
                    "subject": None,
                    "subject_zh": None,
                    "body": "Thanks for your message. Which grade do you need?",
                    "body_zh": "感谢您的消息。请问您需要什么牌号？",
                },
            }
        )
        created = Mock(return_value="linkedin-message-1")
        with patch.object(notes_trial, "create_review_message", created):
            result = notes_trial.publish_review(
                self.record,
                self.state,
                {"semantic": self.semantic, "draft": self.draft},
            )

        self.assertEqual("linkedin-message-1", result["message_version_id"])
        _run_id, _lead_id, snapshot, output = created.call_args.args
        self.assertEqual("linkedin", snapshot["output"]["type"])
        self.assertEqual(
            self.record["contact_linkedin_url"],
            snapshot["contact"]["linkedin_url"],
        )
        self.assertEqual("linkedin", output["output_type"])
        self.assertIsNone(output["content"]["subject"])
        self.assertEqual([], validation_errors(output, snapshot, "person-1"))

    def test_publish_internal_task_uses_action_queue_not_message_review(self):
        message_created = Mock(return_value="should-not-exist")
        action_created = Mock(
            return_value={
                "action_id": "action-1",
                "status": "pending",
                "created": True,
            }
        )
        analysis = {
            "semantic": {**self.semantic, "decision": "internal_task"},
            "draft": None,
            "sales_action": {
                "lead_id": "person-1",
                "latest_note_id": "note-1",
                "action": "准备客户要求的当前规格资料，核对版本后再回复客户。",
            },
        }
        with patch.object(notes_trial, "create_review_message", message_created), patch.object(
            notes_trial, "create_review_action", action_created
        ):
            result = notes_trial.publish_review(self.record, self.state, analysis)

        self.assertEqual("pending", result["status"])
        self.assertEqual("action-1", result["action_id"])
        self.assertIsNone(result["message_version_id"])
        message_created.assert_not_called()
        run_id, lead_id, action_type, snapshot, stored_analysis = action_created.call_args.args
        self.assertEqual(notes_trial.notes_review_run_id("note-1"), run_id)
        self.assertEqual("person-1", lead_id)
        self.assertEqual("internal_task", action_type)
        self.assertEqual("倩文 于", snapshot["sales"]["name"])
        self.assertEqual(self.semantic["reason"], stored_analysis["reason"])
        self.assertEqual(analysis["sales_action"], stored_analysis["sales_action"])
        self.assertNotIn("sales_draft", stored_analysis)

    def test_publish_manual_review_uses_exact_latest_evidence_fallback(self):
        action_created = Mock(
            return_value={"action_id": "action-2", "status": "pending"}
        )
        analysis = {
            "semantic": {
                "contact_permission": "uncertain",
                "decision": "manual_review",
                "reason": "Hermes 分析失败，需人工复核",
                "review_required": True,
            },
            "draft": None,
        }
        with patch.object(notes_trial, "create_review_action", action_created):
            result = notes_trial.publish_review(
                self.record, self.state, analysis
            )

        self.assertEqual("pending", result["status"])
        stored_analysis = action_created.call_args.args[4]
        self.assertEqual(
            "Could you send the current specification?",
            stored_analysis["evidence_quote"],
        )

    def test_structural_manual_review_does_not_require_sender_or_contact_email(self):
        self.latest["sender_name"] = None
        self.latest["sender_account"] = None
        self.record["contact_email"] = None
        state = {
            "state": "manual_review",
            "reason": "最新邮件时间无法可靠解析",
            "latest": None,
        }
        analysis = {
            "semantic": {
                "contact_permission": "uncertain",
                "decision": "manual_review",
                "reason": state["reason"],
                "review_required": True,
            },
            "draft": None,
        }
        action_created = Mock(
            return_value={"action_id": "action-structural", "status": "pending"}
        )

        with patch.object(notes_trial, "create_review_action", action_created):
            result = notes_trial.publish_review(self.record, state, analysis)

        self.assertEqual("pending", result["status"])
        snapshot = action_created.call_args.args[3]
        self.assertEqual(
            "chloe@okgminerals.com",
            snapshot["conversation_sender_identity"]["account"],
        )
        self.assertIsNone(snapshot["contact"]["email"])

    def test_publish_skips_no_action_or_invalid_email(self):
        message_created = Mock(return_value="should-not-exist")
        action_created = Mock(return_value="should-not-exist")
        with patch.object(notes_trial, "create_review_message", message_created), patch.object(
            notes_trial, "create_review_action", action_created
        ):
            no_action = {
                "semantic": {**self.semantic, "decision": "no_action"},
                "draft": None,
            }
            result = notes_trial.publish_review(self.record, self.state, no_action)
            self.assertEqual("not_published", result["status"])
            self.record["contact_email"] = "not-an-email"
            result = notes_trial.publish_review(self.record, self.state, self.analysis)
            self.assertEqual("not_published", result["status"])
        message_created.assert_not_called()
        action_created.assert_not_called()

    def test_run_id_is_deterministic_and_cli_only_reports_safe_identifiers(self):
        self.assertEqual(
            notes_trial.notes_review_run_id("note-1"),
            notes_trial.notes_review_run_id("note-1"),
        )
        with patch.object(notes_trial, "load_env_file"), patch.object(
            notes_trial, "read_records", return_value=[self.record]
        ), patch.object(
            notes_trial, "safe_analyze", return_value=self.analysis
        ), patch.object(
            notes_trial, "create_review_message", return_value="message-version-1"
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                status = notes_trial.main(
                    ["--record-id", "person-1", "--publish-review"]
                )
        self.assertEqual(0, status)
        self.assertEqual(
            '{"message_version_id": "message-version-1", "lead_id": "person-1", "status": "pending_review"}\n',
            output.getvalue(),
        )
        self.assertNotIn("Could you send", output.getvalue())
        self.assertNotIn("Hello Mr.", output.getvalue())

    def test_publish_flag_requires_single_record(self):
        with self.assertRaises(SystemExit):
            notes_trial.main(["--publish-review"])

    def test_current_note_sender_comes_from_the_first_bold_name(self):
        note = notes_trial.parse_note(
            {
                "note_id": "note-current",
                "direction": "FA",
                "note_title": "发信 | Customer | UID:1",
                "body": (
                    "**Buddy**\n"
                    "**方向**: 发信\n"
                    "**日期**: Thu, 20 Aug 2026 23:35:30 +0800\n"
                    "**主题**: Re: Specs\n"
                    "## 最新邮件原文\nHello"
                ),
                "note_created_at": "2026-08-20T15:35:31+00:00",
            }
        )

        self.assertEqual("Buddy", note["sender_name"])
        self.assertEqual("buddy@okgmineral.com", note["sender_account"])

    def test_reply_sender_falls_back_to_crm_creator_mapping(self):
        self.latest["sender_name"] = None
        self.latest["sender_account"] = None

        identity = notes_trial.reply_sender_identity(self.record, self.latest)

        self.assertEqual(
            {
                "name": "Chloe",
                "account": "chloe@okgminerals.com",
                "source": "crm.creator.mapping",
            },
            identity,
        )

    def test_trusted_new_note_falls_back_to_created_at_without_marked_date(self):
        cases = (
            ("IMAP自动抓取", "SHOU", "收信", "needs_analysis"),
            ("crm-outbound自动发送", "FA", "发信", "waiting_customer"),
            ("crm-outbound历史迁移", "FA", "发信", "waiting_customer"),
        )
        for index, (source, direction, title_direction, expected_state) in enumerate(
            cases
        ):
            with self.subTest(source=source):
                note = notes_trial.parse_note(
                    {
                        "note_id": f"note-new-{index}",
                        "direction": direction,
                        "note_title": (
                            f"{title_direction} | 26-08-20 (周四) 18:00:00"
                        ),
                        "body": (
                            "**Yuki**\n"
                            f"**方向**: {title_direction}\n"
                            "**主题**: Re: Specs\n"
                            f"**来源**: {source}\n"
                            f"**邮件标识**: <message-{index}@example.com>\n"
                            "## 最新邮件原文\nPlease send the current specification."
                        ),
                        "note_created_at": "2026-08-20T10:00:00+00:00",
                    }
                )

                self.assertEqual("2026-08-20T10:00:00+00:00", note["email_at"])
                self.assertEqual("crm.note.createdAt", note["email_at_source"])
                self.assertEqual(expected_state, notes_trial.structural_state([note])["state"])

    def test_empty_crm_transport_event_does_not_replace_latest_business_email(self):
        customer_reply = notes_trial.parse_note(
            {
                "note_id": "note-customer-reply",
                "direction": "SHOU",
                "note_title": "收信 | 26-07-24 (周五) 20:24:30",
                "body": (
                    "**Dean**\n"
                    "**来源**: IMAP自动抓取\n"
                    "**邮件标识**: UID:13794\n"
                    "## 最新邮件原文\nPlease contact our plant colleagues."
                ),
                "note_created_at": "2026-07-24T12:24:30+00:00",
            }
        )
        receipt = notes_trial.parse_note(
            {
                "note_id": "note-unread-receipt",
                "direction": "SHOU",
                "note_title": "收信 | 26-07-24 (周五) 22:35:30",
                "body": (
                    "**Dean**\n"
                    "**来源**: IMAP自动抓取\n"
                    "**邮件标识**: UID:13796\n"
                    "## 最新邮件原文\n(邮件正文为空或无法解析)"
                ),
                "note_created_at": "2026-07-24T14:35:30+00:00",
                "source_message_id": "message-unread-receipt",
                "source_message_subject": "No leído: Re: Follow-up",
                "source_message_text_empty": True,
            }
        )

        state = notes_trial.structural_state([customer_reply, receipt])

        self.assertEqual("needs_analysis", state["state"])
        self.assertEqual("note-customer-reply", state["latest"]["note_id"])
        self.assertEqual(
            [customer_reply],
            notes_trial.business_email_notes([customer_reply, receipt]),
        )

        receipt_only = notes_trial.structural_state([receipt])
        self.assertEqual("waiting_customer", receipt_only["state"])
        self.assertEqual("note-unread-receipt", receipt_only["latest"]["note_id"])

    def test_missing_provenance_does_not_promote_quoted_date_to_email_time(self):
        cases = (
            "**来源**: IMAP自动抓取\n## 最新邮件原文\nPlease send the current specification.",
            "**来源**: 人工整理\n**邮件标识**: <message@example.com>\n## 最新邮件原文\nPlease send the current specification.",
            "## 最新邮件原文\nDate: Wed, 20 Aug 2026 10:00:00 +0000\nPlease send the current specification.",
        )
        for index, body_tail in enumerate(cases):
            with self.subTest(index=index):
                note = notes_trial.parse_note(
                    {
                        "note_id": f"note-manual-{index}",
                        "direction": "SHOU",
                        "note_title": "收信 | Customer | UID:manual",
                        "body": (
                            "**Yuki**\n"
                            "**方向**: 收信\n"
                            "**主题**: Re: Specs\n"
                            f"{body_tail}"
                        ),
                        "note_created_at": "2026-08-20T10:00:00+00:00",
                    }
                )

                self.assertIsNone(note["email_at"])
                self.assertEqual("manual_review", notes_trial.structural_state([note])["state"])

    def test_trusted_note_rejects_inconsistent_title(self):
        for title in (
            "发信 | 26-08-20 (周四) 18:00:00",
            "收信 | 26-08-20 (周四) 20:00:00",
            "收信 | 26-99-99 (周四) 18:00:00",
        ):
            with self.subTest(title=title):
                note = notes_trial.parse_note(
                    {
                        "note_id": "note-bad-title",
                        "direction": "SHOU",
                        "note_title": title,
                        "body": (
                            "**Yuki**\n"
                            "**主题**: Re: Specs\n"
                            "**来源**: IMAP自动抓取\n"
                            "**邮件标识**: <message@example.com>\n"
                            "## 最新邮件原文\nHello"
                        ),
                        "note_created_at": "2026-08-20T10:00:00+00:00",
                    }
                )

                self.assertIsNone(note["email_at"])

    def test_legacy_email_communication_title_is_ignored(self):
        self.assertTrue(
            notes_trial.is_legacy_email_note(
                "邮件沟通记录 | 发信 | Customer | UID:1"
            )
        )
        self.assertFalse(
            notes_trial.is_legacy_email_note("发信 | Customer | UID:1")
        )

    def test_analyze_retries_validation_once_with_the_local_error(self):
        semantic = {**self.semantic, "decision": "no_action"}
        hermes_result = Mock(
            returncode=0,
            stdout=json.dumps({"lead_id": "person-1"}),
            stderr="",
        )
        with patch.object(notes_trial.os, "access", return_value=True), patch.object(
            notes_trial.subprocess, "run", return_value=hermes_result
        ) as run, patch.object(
            notes_trial,
            "validate_semantic_result",
            side_effect=[RuntimeError("Semantic result shape is invalid"), semantic],
        ):
            result = notes_trial.analyze(self.record, self.state)

        self.assertEqual({"semantic": semantic, "draft": None}, result)
        self.assertEqual(2, run.call_count)
        retry_prompt = run.call_args_list[1].args[0][-1]
        self.assertIn("Semantic result shape is invalid", retry_prompt)
        self.assertIn("Do not relax any rule", retry_prompt)

    def test_analyze_retries_malformed_json_once(self):
        semantic = {**self.semantic, "decision": "no_action"}
        malformed = Mock(
            returncode=0,
            stdout=(
                '{"lead_id":"person-1","reason":"customer said "no",'
                '"decision":"no_action"}'
            ),
            stderr="",
        )
        corrected = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    key: value
                    for key, value in semantic.items()
                    if key not in {"evidence_quote", "review_required"}
                }
            ),
            stderr="",
        )
        with patch.object(notes_trial.os, "access", return_value=True), patch.object(
            notes_trial.subprocess, "run", side_effect=[malformed, corrected]
        ) as run:
            result = notes_trial.analyze(self.record, self.state)

        self.assertEqual({"semantic": semantic, "draft": None}, result)
        self.assertEqual(2, run.call_count)
        retry_prompt = run.call_args_list[1].args[0][-1]
        self.assertIn("Hermes output does not contain a JSON object", retry_prompt)
        self.assertIn("Do not relax any rule", retry_prompt)

    def test_prompt_uses_conversation_actions_instead_of_forced_holding_reply(self):
        prompt = notes_trial._semantic_prompt(self.record, self.latest, "Buddy")

        self.assertIn("reply|internal_task|referral|no_action|manual_review", prompt)
        self.assertIn("A customer's question never proves its own answer", prompt)
        self.assertIn('Never call the salesperson\'s outbound proposal', prompt)
        self.assertIn(
            "never reopens an earlier request and never creates an internal_task",
            prompt,
        )
        self.assertNotIn("use a short holding reply", prompt)
        self.assertNotIn("Do not ask a new question", prompt)

    def test_prompt_treats_redirected_offer_as_history_aware_referral(self):
        outbound = {
            **self.latest,
            "note_id": "note-outbound",
            "direction": "FA",
            "email_at": None,
            "subject": "Refractory raw materials",
            "body": "## 中文翻译\nWe offer silicon carbide and magnesia bricks.",
        }
        incoming = {
            **self.latest,
            "note_id": "note-incoming",
            "direction": "SHOU",
            "body": "## 最新邮件原文\nYou can send your offer to buyer@example.com.",
        }
        record = {**self.record, "notes": [outbound, incoming]}

        prompt = notes_trial._semantic_prompt(record, incoming, "Buddy")

        self.assertIn(
            "redirects an existing proposal to another email address",
            prompt,
        )
        payload = json.loads(prompt.split("CRM RECORD:\n", 1)[1])
        self.assertEqual(2, len(payload["recent_email_notes"]))
        self.assertEqual(["FA", "SHOU"], [n["direction"] for n in payload["recent_email_notes"]])

    def test_semantic_contract_rejects_draft_fields(self):
        candidate = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "decision": "reply",
            "confidence": 0.9,
            "reason": "客户要求回复。",
            "evidence_index": 0,
            "resume_at": None,
            "content": self.draft["content"],
        }

        with self.assertRaisesRegex(RuntimeError, "must not contain draft"):
            notes_trial.validate_semantic_result(
                candidate, self.record, self.latest
            )

    def test_draft_contract_rejects_semantic_decision(self):
        candidate = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "language": "English",
            "content": self.draft["content"],
            "decision": "reply",
        }

        with self.assertRaisesRegex(RuntimeError, "must not contain decision"):
            notes_trial.validate_draft_result(
                candidate, self.record, self.latest, "Buddy"
            )

    def test_blocked_contact_cannot_be_classified_as_referral(self):
        candidate = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "contact_permission": "blocked",
            "decision": "referral",
            "confidence": 0.9,
            "reason": "客户要求停止联系。",
            "evidence_index": 0,
            "resume_at": None,
        }

        with self.assertRaisesRegex(RuntimeError, "blocked contact must be no_action"):
            notes_trial.validate_semantic_result(candidate, self.record, self.latest)

    def test_publish_blocks_customer_message_without_allowed_permission(self):
        message_created = Mock(return_value="should-not-exist")
        analysis = {
            "semantic": {
                **self.semantic,
                "contact_permission": "blocked",
                "decision": "reply",
            },
            "draft": self.draft,
        }

        with patch.object(notes_trial, "create_review_message", message_created):
            result = notes_trial.publish_review(self.record, self.state, analysis)

        self.assertEqual("not_published", result["status"])
        message_created.assert_not_called()

    def test_analyze_runs_semantic_then_generation(self):
        semantic = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "contact_permission": "allowed",
            "decision": "reply",
            "confidence": 0.9,
            "reason": "客户要求回复。",
            "evidence_index": 0,
            "resume_at": None,
        }
        draft = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "language": "English",
            "content": self.draft["content"],
        }
        hermes_results = [
            Mock(returncode=0, stdout=json.dumps(semantic), stderr=""),
            Mock(returncode=0, stdout=json.dumps(draft), stderr=""),
        ]
        with patch.object(notes_trial.os, "access", return_value=True), patch.object(
            notes_trial.subprocess, "run", side_effect=hermes_results
        ) as run:
            result = notes_trial.analyze(self.record, self.state)

        self.assertEqual(2, run.call_count)
        self.assertEqual("reply", result["semantic"]["decision"])
        self.assertNotIn("content", result["semantic"])
        self.assertNotIn("decision", result["draft"])

    def test_analyze_injects_one_business_knowledge_snapshot_into_both_stages(self):
        semantic = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "contact_permission": "allowed",
            "decision": "reply",
            "confidence": 0.9,
            "reason": "客户要求回复。",
            "evidence_index": 0,
            "resume_at": None,
        }
        draft = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "language": "English",
            "content": self.draft["content"],
        }
        hermes_results = [
            Mock(returncode=0, stdout=json.dumps(semantic), stderr=""),
            Mock(returncode=0, stdout=json.dumps(draft), stderr=""),
        ]
        with patch.dict(
            notes_trial.os.environ,
            {"HERMES_NOTES_BUSINESS_KNOWLEDGE_ENABLED": "true"},
        ), patch.object(notes_trial.os, "access", return_value=True), patch.object(
            notes_trial.subprocess, "run", side_effect=hermes_results
        ) as run:
            notes_trial.analyze(self.record, self.state)

        prompts = [call.args[0][-1] for call in run.call_args_list]
        for prompt in prompts:
            self.assertIn("APPROVED BUSINESS KNOWLEDGE SNAPSHOT", prompt)
            self.assertIn("business-knowledge-v1", prompt)
            self.assertIn("company.positioning", prompt)
            self.assertIn("product.non_catalog", prompt)
        semantic_hash = prompts[0].split("SHA-256: ", 1)[1].splitlines()[0]
        draft_hash = prompts[1].split("SHA-256: ", 1)[1].splitlines()[0]
        self.assertEqual(semantic_hash, draft_hash)

    def test_business_knowledge_direct_answer_overrides_missing_notes_fact(self):
        snapshot = notes_trial.load_snapshot(("notes_semantic", "notes_draft"))
        prompt = notes_trial._semantic_prompt(
            self.record, self.state["latest"], "Buddy", snapshot
        )

        self.assertIn(
            "or a direct_answer entry in the approved business knowledge snapshot",
            prompt,
        )
        self.assertIn("Apply its routing mode before choosing an action", prompt)

    def test_business_knowledge_is_disabled_by_default(self):
        with patch.dict(notes_trial.os.environ):
            notes_trial.os.environ.pop(
                "HERMES_NOTES_BUSINESS_KNOWLEDGE_ENABLED", None
            )
            self.assertIsNone(notes_trial._business_knowledge_snapshot())

    def test_analyze_internal_task_generates_direct_sales_action(self):
        semantic = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "contact_permission": "allowed",
            "decision": "internal_task",
            "confidence": 0.9,
            "reason": "客户要求销售准备规格资料。",
            "evidence_index": 0,
            "resume_at": None,
        }
        sales_action = {
            "lead_id": "person-1",
            "latest_note_id": "note-1",
            "action": "准备客户要求的当前规格资料，核对版本后再回复客户。",
        }
        hermes_results = [
            Mock(returncode=0, stdout=json.dumps(semantic), stderr=""),
            Mock(returncode=0, stdout=json.dumps(sales_action), stderr=""),
        ]

        with patch.object(notes_trial.os, "access", return_value=True), patch.object(
            notes_trial.subprocess, "run", side_effect=hermes_results
        ) as run:
            result = notes_trial.analyze(self.record, self.state)

        self.assertEqual(2, run.call_count)
        self.assertEqual("internal_task", result["semantic"]["decision"])
        self.assertIsNone(result["draft"])
        self.assertEqual(sales_action, result["sales_action"])
        generation_prompt = run.call_args_list[1].args[0][-1]
        self.assertIn("direct internal instruction for the salesperson", generation_prompt)
        self.assertIn("Do not write an email", generation_prompt)
        self.assertIn("short imperative steps", generation_prompt)
        self.assertNotIn("customer-facing email draft", generation_prompt)

    def test_referral_generation_has_no_next_step(self):
        prompt = notes_trial._draft_prompt(
            self.record,
            self.latest,
            "Buddy",
            {**self.semantic, "decision": "referral"},
        )

        self.assertIn("Gratitude is the entire communicative", prompt)
        self.assertIn("any statement about what the sender or Aceler", prompt)
        self.assertIn("language used by the latest SHOU", prompt)

    def test_review_snapshot_keeps_recent_email_history(self):
        older = {
            **self.latest,
            "note_id": "note-older",
            "direction": "FA",
            "email_at": None,
            "body": "## 最新邮件原文\nEarlier sales proposal",
        }
        record = {**self.record, "notes": [older, self.latest]}

        snapshot = notes_trial.build_notes_review_snapshot(
            record, self.state, self.semantic
        )

        history = snapshot["review_context"]["crm_email_history"]
        self.assertEqual(["note-older", "note-1"], [note["note_id"] for note in history])

    def test_email_history_does_not_drop_older_notes(self):
        record = {
            **self.record,
            "notes": [
                {**self.latest, "note_id": f"note-{index}"}
                for index in range(20)
            ],
        }

        history = notes_trial.recent_email_history(record)

        self.assertEqual(20, len(history))
        self.assertEqual("note-0", history[0]["note_id"])
        self.assertEqual("note-19", history[-1]["note_id"])

    def test_reply_subject_collapses_repeated_thread_prefixes(self):
        self.assertEqual(
            "Re: Brazil market plan",
            notes_trial._reply_subject(
                "RES: 回复：ENC: Re: Brazil market plan"
            ),
        )
        self.assertEqual(
            "回复：巴西市场计划",
            notes_trial._reply_subject("回复：答复：巴西市场计划", "回复："),
        )
        self.assertEqual(
            "Re: Aluminum die casting consumables",
            notes_trial._reply_subject("ОТН: Aluminum die casting consumables"),
        )

    def test_question_punctuation_is_not_used_as_a_semantic_validator(self):
        candidate = {
            **self.draft,
            "content": {
                **self.draft["content"],
                "body": "Hello Mr. Buyer,\n\nWhich grade? Which application?\n\nBuddy",
                "body_zh": "您好，Buyer先生：\n\n哪个牌号？什么应用？\n\nBuddy",
            },
        }

        result = notes_trial.validate_draft_result(
            candidate, self.record, self.latest, "Buddy"
        )

        self.assertNotIn("decision", result)

    def test_referral_meaning_is_not_inferred_from_future_tense(self):
        candidate = {
            **self.draft,
            "content": {
                **self.draft["content"],
                "body": "Thank you for the introduction. We will always appreciate your help.\n\nBuddy",
                "body_zh": "感谢您的介绍。我们会一直感谢您的帮助。\n\nBuddy",
            },
        }

        result = notes_trial.validate_draft_result(
            candidate, self.record, self.latest, "Buddy"
        )

        self.assertNotIn("decision", result)

    def test_notes_review_payload_passes_shared_policy_without_legacy_warning(self):
        snapshot = notes_trial.build_notes_review_snapshot(
            self.record, self.state, self.semantic
        )
        output = notes_trial.build_notes_review_output(
            self.record, self.latest, self.semantic, self.draft
        )

        self.assertEqual([], validation_errors(output, snapshot, "person-1"))
        self.assertNotIn(
            "CRM_EVIDENCE_REQUIRES_MANUAL_CONFIRMATION", snapshot["warnings"]
        )


if __name__ == "__main__":
    unittest.main()
