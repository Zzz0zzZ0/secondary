import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from app import outbox, delivery_service
from app.secondary_scheduler import SecondaryLeadScheduler
from app.secondary.review_timing import draft_at, review_lead_days, follow_up_at
from tests.test_outbox_sender_account import pending_message, FakeCursor, FakeConnection
from tests import test_followup_rescheduling as fixtures
from tests.test_delivery_service import FakeCursor as DeliveryCursor, FakeConnection as DeliveryConnection


class AdvanceWindowTest(unittest.TestCase):
    def test_windows_and_exact_boundary(self):
        now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        self.assertEqual(3, review_lead_days('unknown_demand', 'lead', 0))
        self.assertEqual(7, review_lead_days('below_moq', 'lead', 0))
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(state_dir=Path(directory), environment={'HERMES_NOTES_ENABLED':'false'}, now=lambda:now)
            for lead, kind, days in [('short','unknown_demand',3),('long','below_moq',7),('outside','unknown_demand',4)]:
                scheduler._upsert_discovered({'lead':{'id':lead},'source_version':{'record_id':lead,'updated_at':now.isoformat()}})
                with scheduler._connect() as c:c.execute("UPDATE secondary_lead_state SET status='scheduled',lead_type=?,next_action_at=? WHERE lead_id=?",(kind,(now+timedelta(days=days)).isoformat(),lead))
            self.assertEqual(now, scheduler._next_wakeup_at())
            self.assertEqual('short', scheduler._claim_due_action()['lead_id'])
            self.assertEqual('long', scheduler._claim_due_action()['lead_id'])
            self.assertIsNone(scheduler._claim_due_action())
            with scheduler._connect() as c:c.execute("UPDATE secondary_lead_state SET status='scheduled',generation_retry_at=? WHERE lead_id='short'",((now+timedelta(minutes=20)).isoformat(),))
            self.assertIsNone(scheduler._claim_due_action(), 'retry must not use the advance window to run early')

    def test_conflicting_dates_use_latest_safe_deadline(self):
        self.assertEqual(datetime(2026,9,12,tzinfo=timezone.utc),follow_up_at({'review_schedule':{'follow_up_at':'2026-09-10T00:00:00Z'},'lead':{'next_follow_up_at':'2026-09-12T00:00:00Z'}}))


class EarlyDispatchTest(unittest.TestCase):
    setUp = fixtures.FollowUpReschedulingTest.setUp
    state = fixtures.FollowUpReschedulingTest.state

    def test_early_generation_retains_deadline_and_does_not_repeat(self):
        due = self.now + timedelta(days=2)
        with self.scheduler._connect() as c:c.execute('UPDATE secondary_lead_state SET next_action_at=?',(due.isoformat(),))
        self.scheduler._export_current_record=Mock(return_value=self.record)
        self.scheduler.message_processor.process_job=Mock(return_value=None)
        self.scheduler._analysis_result=Mock(return_value={'decision':'generated','output_type':'email','run_id':'run','result_json':json.dumps({'decision':'generated'})})
        result=self.scheduler._dispatch_one(self.scheduler._claim_due_action())
        self.assertEqual('waiting_review',result['status'])
        self.assertEqual(due.isoformat(),self.state()['next_action_at'])
        self.assertIsNone(self.scheduler._claim_due_action())
        with self.scheduler._connect() as c:job=c.execute('SELECT input_path FROM analysis_job').fetchone()
        data=json.loads(Path(job['input_path']).read_text())[0]
        self.assertFalse(data['secondary_lead_schedule']['delivery_due'])
        self.assertTrue(data['secondary_lead_schedule']['generation_ready'])
        self.assertEqual(due.isoformat(),data['review_schedule']['follow_up_at'])

    def test_no_message_does_not_spin_in_review_window(self):
        with self.scheduler._connect() as c:c.execute('UPDATE secondary_lead_state SET next_action_at=?',(self.now.isoformat(),))
        self.scheduler._export_current_record=Mock(return_value=self.record)
        self.scheduler.message_processor.process_job=Mock(return_value=None)
        self.scheduler._analysis_result=Mock(return_value={'decision':'no_message','run_id':'run','result_json':json.dumps({'reason':'No useful message','warnings':[]})})
        self.scheduler._dispatch_one(self.scheduler._claim_due_action())
        self.assertEqual(self.state()['next_action_at'], self.state()['generation_retry_at'])
        self.assertIsNone(self.scheduler._claim_due_action())

    def test_review_invalidation_keeps_send_count_and_deadline(self):
        due=self.state()['next_action_at']
        with self.scheduler._connect() as c:c.execute("UPDATE secondary_lead_state SET status='waiting_delivery',latest_message_version_id='m'")
        self.assertTrue(self.scheduler.handle_outbox_signal('m','review_invalidated'))
        row=self.state();self.assertEqual('waiting_review',row['status']);self.assertEqual(due,row['next_action_at']);self.assertEqual(0,row['follow_up_count'])


class ReviewSafetyTest(unittest.TestCase):
    def test_future_approval_cannot_enqueue_even_without_frontend(self):
        row=pending_message('email','buyer@example.com','倩文 于',review_schedule={'follow_up_at':'2099-01-01T00:00:00Z'})
        cursor=FakeCursor(row)
        with patch.object(outbox,'connect',return_value=FakeConnection(cursor)):
            with self.assertRaisesRegex(RuntimeError,'尚未到'):outbox.approve_message(row[0],'reviewer')
        self.assertFalse(any('INSERT' in q for q,p in cursor.calls))

    def test_changed_notes_block_review_and_due_delivery(self):
        snapshot={'review_schedule':{'follow_up_at':'2020-01-01T00:00:00Z','notes_source_hash':'old','crm_source_hash':'current'}}
        with patch.object(outbox,'read_referral_history',return_value=(None,{'source_hash':'changed'})):
            with self.assertRaises(outbox.ReviewContextChanged):outbox.ensure_send_ready('lead',snapshot)

    def test_marking_review_never_creates_delivery_and_checks_edit_version(self):
        at=datetime(2026,9,8,tzinfo=timezone.utc)
        row=pending_message('email','buyer@example.com','倩文 于')+(at,)
        cursor=FakeCursor(row)
        with patch.object(outbox,'connect',return_value=FakeConnection(cursor)),patch.object(outbox,'get_review_message',return_value={'id':'m'}),patch.object(outbox,'_json',side_effect=lambda x:x):
            outbox.mark_message_reviewed(row[0],'reviewer',at.isoformat())
            with self.assertRaisesRegex(RuntimeError,'草稿已被更新'):outbox.mark_message_reviewed(row[0],'reviewer',(at-timedelta(seconds=1)).isoformat())
        writes=[(q,p) for q,p in cursor.calls if q.strip().startswith('UPDATE')]
        self.assertEqual(1,len(writes));self.assertEqual('reviewer',writes[0][1][0]['content_review']['reviewed_by'])
        self.assertFalse(any('INSERT' in q for q,p in cursor.calls))

    def test_edit_invalidates_review_marker(self):
        cursor=FakeCursor(pending_message('email','buyer@example.com','倩文 于'))
        with patch.object(outbox,'connect',return_value=FakeConnection(cursor)),patch.object(outbox,'get_review_message'):
            outbox.save_message_edit('m','Subject','Changed')
        self.assertTrue(any("crm_snapshot - 'content_review'" in q for q,p in cursor.calls))

    def test_worker_rechecks_context_before_returning_payload(self):
        from uuid import uuid4
        row=(uuid4(),uuid4(),'email','email','buyer@example.com','email:sender',{},'key',1)
        cursor=DeliveryCursor(fetchone_results=[row,('lead',{})],fetchall_results=[])
        connection=DeliveryConnection(cursor);connection.commit=Mock()
        with patch.object(delivery_service,'connect',return_value=connection),patch.object(delivery_service,'ensure_send_ready',side_effect=outbox.ReviewContextChanged('Notes changed')),patch.object(delivery_service,'notify_secondary_outbox_event') as notify:
            self.assertIsNone(delivery_service.claim_delivery('worker',['email'],['email']))
        self.assertTrue(any("status='cancelled'" in q for q,p in cursor.calls))
        self.assertTrue(any("review_status='pending_review'" in q and 'version=version+1' in q for q,p in cursor.calls))
        self.assertFalse(any('INSERT INTO sales_automation.delivery_attempt' in q for q,p in cursor.calls))
        notify.assert_called_once_with(row[1],'review_invalidated')


if __name__=='__main__':unittest.main()

class RegenerationFreshnessTest(unittest.TestCase):
    def test_new_reply_preserves_old_draft_and_routes_to_notes(self):
        from review_api import service
        from unittest.mock import MagicMock
        scheduler=MagicMock()
        scheduler._export_current_record.return_value={'lead':{'id':'lead'}}
        scheduler._record_identity.return_value=('lead','','','crm')
        scheduler.notes_processor._discover.return_value='new-note'
        scheduler._connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value={'status':'pending'}
        scheduler.now.return_value=datetime(2026,9,8,tzinfo=timezone.utc)
        message={'lead_id':'lead','crm_snapshot':{'review_schedule':{'crm_source_hash':'crm'}}}
        original=json.dumps(message)
        with patch('app.secondary_scheduler.SecondaryLeadScheduler',return_value=scheduler),patch.object(service,'read_referral_history',return_value=({'notes':[{'id':'new'}]},{'source_hash':'new'})),patch('notes_trial.notes_trial.structural_state',return_value={'state':'needs_analysis'}):
            with self.assertRaisesRegex(RuntimeError,'新的客户回复'):service._current_review_input(message)
        self.assertEqual(original,json.dumps(message))
        scheduler._notify_scheduler.assert_called_once()
        scheduler._message_input.assert_not_called()

    def test_replacement_rechecks_context_after_model_finishes(self):
        at=datetime(2026,9,8,tzinfo=timezone.utc)
        row=pending_message('email','buyer@example.com','倩文 于')+(at,)
        cursor=FakeCursor(row)
        with patch.object(outbox,'connect',return_value=FakeConnection(cursor)),patch.object(outbox,'ensure_review_current',side_effect=outbox.ReviewContextChanged('Notes changed')):
            with self.assertRaises(outbox.ReviewContextChanged):
                outbox.replace_message_draft(row[0],{},crm_snapshot={'review_schedule':{'notes_source_hash':'old'}},expected_updated_at=at.isoformat())
        self.assertFalse(any(q.strip().startswith('UPDATE') for q,p in cursor.calls))
