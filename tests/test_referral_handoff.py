import json
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch
from tests import test_followup_rescheduling as fixtures
from tests import test_outbox_sender_account as outbox_fixtures
from app import outbox
from app.secondary.message_policy import prepare_message_record
from app.secondary.referrals import referral_history_check
from notes_trial import notes_trial as notes


class ReferralHandoffTest(unittest.TestCase):
    setUp = fixtures.FollowUpReschedulingTest.setUp
    state = fixtures.FollowUpReschedulingTest.state

    def notes(self, direction='FA', days=0):
        at=(self.now-timedelta(days=days)).isoformat()
        return {'lead_id':'lead-1','contact_name':'Buyer','company_name':'Company','contact_email':'buyer@example.com',
                'notes':[{'note_id':'sent-1','direction':direction,'email_at':at,'created_at':at,'body':'Previous conversation','channel':'email','subject':None}]}

    def test_handoff_preserves_target_queue_and_does_not_generate_for_referrer(self):
        parent=dict(self.record, crm_referral={'recommended_by':[], 'referred_contacts':[{'id':'target','name':'Buyer'}]})
        target=dict(self.record, lead={'id':'target'}, contact={'name':'Buyer','email':'target@example.com'}, crm_referral={'recommended_by':[{'id':'lead-1','name':'Introducer'}],'referred_contacts':[]})
        self.scheduler._upsert_discovered(target)
        with self.scheduler._connect() as c:
            c.execute("UPDATE secondary_lead_state SET status='waiting_review',latest_message_version_id='draft-existing',next_action_at=NULL WHERE lead_id='target'")
            c.execute("UPDATE secondary_lead_state SET crm_snapshot_json=? WHERE lead_id='lead-1'", (json.dumps(parent),))
        self.scheduler._export_current_record=Mock(return_value=target)
        self.scheduler.message_processor.process_job=Mock()
        with patch('app.secondary_scheduler.reject_pending_messages_for_lead'), patch('app.secondary_scheduler.read_referral_history',return_value=(None,referral_history_check(None))):
            result=self.scheduler._handoff_recommender(self.state())
            self.assertEqual('referral_handoff',result['decision'])
            self.assertEqual('waiting_review',result['targets'][0]['status'])
            self.assertEqual([],self.scheduler._route_referrals())
        self.scheduler.message_processor.process_job.assert_not_called()
        with self.scheduler._connect() as c:
            target_state=c.execute("select * from secondary_lead_state where lead_id='target'").fetchone()
        self.assertEqual('draft-existing',target_state['latest_message_version_id'])
        self.assertEqual('paused', self.state()['status'])

    def check(self, record):
        message=prepare_message_record(dict(self.record, referral_context={'current_contact_role':'referred'}),None)
        with patch('app.secondary_scheduler.read_referral_history',return_value=(record,referral_history_check(record))):
            outcome=self.scheduler._check_referral_history(self.state(),message)
        return outcome,message

    def test_no_history_allows_first_contact(self):
        outcome,message=self.check(None)
        self.assertIsNone(outcome)
        self.assertEqual('referred_intro',message['message_route'])
        self.assertFalse(message['referral_history_check']['has_outbound'])

    def test_referrer_send_claim_is_not_inherited_when_recipient_has_no_notes(self):
        message=prepare_message_record(dict(self.record, referral_context={'current_contact_role':'referred'}),{'sales_follow_up_context':{'status':'sales_replied','evidence_quote':'Wrote to another contact who referred this email'}})
        with patch('app.secondary_scheduler.read_referral_history',return_value=(None,referral_history_check(None))):
            outcome=self.scheduler._check_referral_history(self.state(),message)
        self.assertIsNone(outcome)
        self.assertEqual('referred_intro',message['message_route'])
        self.assertEqual('none',message['sales_follow_up_context']['status'])

    def test_recent_sent_note_defers_duplicate_outreach(self):
        outcome,_=self.check(self.notes())
        self.assertEqual('waiting_existing_followup',outcome['decision'])
        self.assertGreater(self.state()['next_action_at'],self.now.isoformat())

    def test_old_sent_note_generates_followup_using_recipient_history(self):
        with self.scheduler._connect() as c:c.execute("UPDATE secondary_lead_state SET next_action_at=?", (self.now.isoformat(),))
        outcome,message=self.check(self.notes(days=60))
        self.assertIsNone(outcome)
        self.assertEqual('conversation_follow_up',message['message_route'])
        self.assertEqual('Previous conversation',message['conversation_history'][0]['body'])
        self.assertTrue(message['referral_history_check']['has_outbound'])
        self.assertEqual('buyer@example.com',message['contact']['email'])

    def test_incoming_reply_is_owned_by_notes_before_any_first_contact(self):
        outcome,_=self.check(self.notes(direction='SHOU'))
        self.assertEqual('notes_history_deferred',outcome['decision'])
        self.assertIsNone(self.scheduler._claim_due_action())

    def test_unknown_send_date_requires_review(self):
        record=self.notes();record['notes'][0]['email_at']=None
        outcome,_=self.check(record)
        self.assertEqual('needs_review',outcome['status'])

    def test_notes_read_error_never_means_no_history(self):
        with patch('app.secondary_scheduler.read_referral_history',side_effect=RuntimeError('CRM unavailable')):
            with self.assertRaisesRegex(RuntimeError,'CRM unavailable'):
                self.scheduler._check_referral_history(self.state(),prepare_message_record(self.record,None))

    def test_notes_never_drafts_for_recommender(self):
        record=self.notes(direction='SHOU')
        record['crm_referral']={'recommended_by':[],'referred_contacts':[{'id':'target','name':'Buyer'}]}
        with patch.object(notes,'_run_hermes') as llm:
            result=notes.analyze(record,notes.structural_state(record['notes']))
        self.assertEqual('no_action',result['semantic']['decision']);llm.assert_not_called()

    def test_notes_processor_honors_referrer_role_from_classification(self):
        with self.scheduler._connect() as c:
            result=json.loads(c.execute('select result_json from secondary_lead_classification').fetchone()[0])
            result['referral_relationship']={'current_contact_role':'recommender','related_contacts':[{'name':'Buyer Two'}]}
            c.execute('update secondary_lead_classification set result_json=?',(json.dumps(result),))
        record=self.notes(direction='SHOU');state=notes.structural_state(record['notes'])
        processor=self.scheduler.notes_processor
        note_id=processor._discover(record,state)
        with self.scheduler._connect() as c:
            row=c.execute('select * from notes_follow_up_state where latest_note_id=?',(note_id,)).fetchone()
        processor.analyze=Mock();processor.publish=Mock()
        self.assertEqual(('no_action',None,False,False),processor._process_one(row,record,state))
        processor.analyze.assert_not_called();processor.publish.assert_not_called()

    def test_new_referral_in_notes_creates_internal_handoff_instead_of_thanks(self):
        record=self.notes(direction='SHOU');record['sales_name']='倩文 于'
        semantic={'decision':'referral','contact_permission':'allowed'}
        with patch.object(notes,'_run_hermes',return_value=semantic) as llm, patch.object(notes,'_business_knowledge_snapshot',return_value=None), patch.object(notes.os,'access',return_value=True):
            result=notes.analyze(record,notes.structural_state(record['notes']))
        self.assertEqual('internal_task',result['semantic']['decision'])
        self.assertIsNone(result['draft']);self.assertEqual(1,llm.call_count)
        self.assertIn('Notes',result['sales_action']['action'])


class ReferralApprovalTest(unittest.TestCase):
    def test_legacy_referrer_draft_cannot_be_approved(self):
        row=outbox_fixtures.pending_message('email','referrer@example.com','倩文 于',message_route='recommender_thanks')
        cursor=outbox_fixtures.FakeCursor(row)
        with patch.object(outbox,'connect',return_value=outbox_fixtures.FakeConnection(cursor)):
            with self.assertRaisesRegex(RuntimeError,'推荐人消息已停用'):
                outbox.approve_message(row[0],'reviewer')
        self.assertFalse(any('INSERT' in q for q,p in cursor.calls))

    def test_changed_recipient_notes_block_approval(self):
        row=outbox_fixtures.pending_message('email','buyer@example.com','倩文 于',referral_context={'current_contact_role':'referred'},referral_history_check={'source_hash':'old'})
        cursor=outbox_fixtures.FakeCursor(row)
        with patch.object(outbox,'connect',return_value=outbox_fixtures.FakeConnection(cursor)), patch.object(outbox,'read_referral_history',return_value=(None,{'source_hash':'new'})):
            with self.assertRaisesRegex(RuntimeError,'Notes 已变化'):
                outbox.approve_message(row[0],'reviewer')
        self.assertFalse(any('INSERT' in q for q,p in cursor.calls))
