import copy
import unittest
from app.secondary.classification import validate_classification
from app.secondary.message_policy import prepare_message_record
from tests import test_secondary_referrals as fixtures


class CrmReferralLinksTest(unittest.TestCase):
    def record(self, incoming=False, outgoing=False):
        record = {'lead': {'id': 'current', 'raw_type': ['RECOMMEND']},
                  'contact': {'id': 'current', 'name': 'Current'},
                  'crm_referral': {'recommended_by': [], 'referred_contacts': []}}
        if incoming:
            record['crm_referral']['recommended_by'] = [{'id': 'introducer', 'name': 'Introducer', 'email': 'intro@example.com'}]
        if outgoing:
            record['crm_referral']['referred_contacts'] = [{'id': 'buyer', 'name': 'Buyer', 'linkedin_url': 'https://linkedin.com/in/buyer'}]
        return record

    def test_crm_links_override_missing_or_wrong_model_direction_without_note(self):
        for incoming, role, name in [(True, 'referred', 'Introducer'), (False, 'recommender', 'Buyer')]:
            record = self.record(incoming=incoming, outgoing=not incoming)
            candidate = fixtures.SecondaryReferralTest()._candidate('referred', 'Invented', 'Invented')
            candidate.update(lead_id='current', lead_type='unknown_demand', evidence=[], recommended_by=[], referral_relationship=None)
            candidate['message_evidence'].update(status='insufficient', customer_action_quote=None, business_detail_quote=None)
            validated = validate_classification(copy.deepcopy(candidate), record)
            self.assertEqual('referred', validated['lead_type'])
            self.assertEqual(role, validated['referral_relationship']['current_contact_role'])
            prepared = prepare_message_record(record, validated)
            self.assertEqual(name, prepared['referral_context']['related_contacts'][0]['name'])
            self.assertEqual('crm.person.recommendedById', prepared['referral_context']['source'])
            self.assertEqual('current', prepared['contact']['id'])

    def test_recommender_role_survives_sales_follow_up_and_stale_classification(self):
        prepared = prepare_message_record(self.record(outgoing=True), {
            'lead_type': 'unknown_demand', 'referral_relationship': None,
            'sales_follow_up_context': {'status': 'sales_replied'},
        })
        self.assertEqual('referral_handoff', prepared['message_route'])
        self.assertEqual('Buyer', prepared['lead']['referred_contacts'])

    def test_both_directions_are_preserved_for_review(self):
        prepared = prepare_message_record(self.record(incoming=True, outgoing=True), None)
        self.assertEqual('both', prepared['referral_context']['current_contact_role'])
        self.assertEqual('referral_review', prepared['message_route'])
        self.assertEqual('Introducer', prepared['lead']['recommended_by'])
        self.assertEqual('Buyer', prepared['lead']['referred_contacts'])

    def test_self_link_is_not_used_as_recommendation(self):
        record = self.record()
        record['crm_referral']['recommended_by'] = [{'id': 'current', 'name': 'Current'}]
        prepared = prepare_message_record(record, None)
        self.assertNotIn('referral_context', prepared)
        self.assertEqual('referral_review', prepared['message_route'])
