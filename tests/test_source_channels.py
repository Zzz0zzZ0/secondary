import copy
import unittest
from unittest.mock import patch
from app.secondary.channels import source_channel, outbound_channel
from app.secondary_scheduler import _prefer_valid_contact_channel
from app.secondary.message_policy import prepare_message_record, validation_errors
from app.outbox import ensure_review_current, ReviewContextChanged
from notes_trial import notes_trial as notes
from tests import test_notes_review_integration as notes_fixtures

class SourceChannelTest(unittest.TestCase):
    def test_explicit_sources_prevent_contact_fallback(self):
        for source,channel in [('AGENT_1','linkedin'),('isales_Agent_1','linkedin'),('AGENT_3','linkedin'),('CRMGEN_JIN','email'),('CRM跟进','email')]:
            for contact in [{'email':'buyer@example.com'},{'linkedin_url':'https://linkedin.com/in/buyer'}]:
                with self.subTest(source=source,contact=contact):
                    record={'lead':{'source':source},'contact':contact,'output':{'type':'email' if channel=='linkedin' else 'linkedin'}}
                    _prefer_valid_contact_channel(record)
                    self.assertEqual(channel,record['output']['type'])
                    self.assertEqual(channel,prepare_message_record(record,None)['output']['type'])
                    self.assertEqual(channel,outbound_channel(record,{'channel':'email' if channel=='linkedin' else 'linkedin'}))
        self.assertIsNone(source_channel('EMAIL'))
        record={'lead':{'source':'EMAIL'},'contact':{'linkedin_url':'https://linkedin.com/in/buyer'},'output':{'type':'email'}}
        _prefer_valid_contact_channel(record);self.assertEqual('linkedin',record['output']['type'])

    def test_old_wrong_channel_is_blocked_before_approval_or_delivery(self):
        for source,channel in [('AGENT_1','email'),('CRMGEN_JIN','linkedin')]:
            snapshot={'lead':{'source':source},'output':{'type':channel}}
            with self.assertRaises(ReviewContextChanged):ensure_review_current('lead',snapshot)
            errors=validation_errors({'lead_id':'lead','decision':'generated','output_type':channel},snapshot,'lead')
            self.assertIn('输出渠道与CRM来源指定渠道不一致',errors)

    def test_notes_output_uses_source_without_relabeling_history(self):
        fixture=notes_fixtures.NotesReviewIntegrationTest();fixture.setUp()
        record,state,semantic,draft=fixture.record,fixture.state,fixture.semantic,fixture.draft
        record['lead_source']='ISALES_AGENT_1';record['contact_linkedin_url']='https://linkedin.com/in/buyer'
        latest=state['latest'];before=copy.deepcopy(latest)
        draft['content']['subject']=draft['content']['subject_zh']=None
        snapshot=notes.build_notes_review_snapshot(record,state,semantic)
        output=notes.build_notes_review_output(record,latest,semantic,draft)
        self.assertEqual('linkedin',snapshot['output']['type']);self.assertEqual('linkedin',output['output_type'])
        self.assertEqual('ISALES_AGENT_1',snapshot['lead']['source']);self.assertEqual(before,latest)
        self.assertEqual('crm.note',snapshot['source_version']['source'])
        record['lead_source']='CRMGEN_JIN';latest['channel']='linkedin'
        self.assertEqual('email',notes.outbound_channel(record,latest))

    def test_adding_source_does_not_reopen_notes_manual_action(self):
        from tests import test_notes_followup_scheduler as fixtures
        f=fixtures.NotesFollowUpSchedulerTest();f.setUp()
        self.addCleanup(f.connection.close)
        processor=f._processor();record=f.records['reply-lead'];state=notes.structural_state(record['notes'])
        note_id=processor._discover(record,state)
        f.connection.execute("UPDATE notes_follow_up_state SET status='completed',decision='internal_task',publication_type='action',publication_id='action' WHERE latest_note_id=?",(note_id,))
        f.connection.commit()
        record['lead_source']='AGENT_1';processor._discover(record,state)
        row=f.connection.execute('SELECT status,decision,publication_id,crm_snapshot_json FROM notes_follow_up_state WHERE latest_note_id=?',(note_id,)).fetchone()
        self.assertEqual(('completed','internal_task','action'),tuple(row)[:3]);self.assertIn('AGENT_1',row['crm_snapshot_json'])

    def test_missing_linkedin_waits_for_contact_without_calling_model(self):
        from tests import test_followup_rescheduling as fixtures
        from unittest.mock import Mock
        f=fixtures.FollowUpReschedulingTest();f.setUp();self.addCleanup(f.doCleanups)
        f.record['lead']['source']='AGENT_2';f.record['output']['type']='linkedin'
        with patch('app.secondary_scheduler.reject_pending_messages_for_lead'):
            f.scheduler._upsert_discovered(f.record)
        with f.scheduler._connect() as c:c.execute("UPDATE secondary_lead_state SET status='scheduled',lead_type='unknown_demand',next_action_at=?",(f.now.isoformat(),))
        f.scheduler._export_current_record=Mock(return_value=f.record);f.scheduler.message_processor.process_job=Mock()
        result=f.scheduler._dispatch_one(f.scheduler._claim_due_action())
        self.assertEqual('needs_contact',result['status']);f.scheduler.message_processor.process_job.assert_not_called()

    def test_source_metadata_does_not_retire_existing_normal_followup(self):
        from tests import test_notes_followup_scheduler as fixtures
        from unittest.mock import Mock
        f=fixtures.NotesFollowUpSchedulerTest();f.setUp();self.addCleanup(f.connection.close)
        f.records={'waiting-lead':f.records['waiting-lead']}
        processor=f._processor();record=f.records['waiting-lead']
        processor._discover(record,notes.structural_state(record['notes']))
        record['lead_source']='CRMGEN_JIN'
        processor.retire_stale=Mock(return_value=[])
        processor.run(limit=1)
        processor.retire_stale.assert_not_called()
