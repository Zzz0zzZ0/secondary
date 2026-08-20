# Output schema

The output must be one JSON object with exactly these semantic fields:

- `lead_id`: non-empty string equal to the input `lead.id`;
- `lead_type`: one of `no_current_demand`, `unknown_demand`, `referred`,
  `below_moq`;
- `confidence`: number between 0 and 1 measuring certainty that the selected
  category is correct, not how complete the CRM data is;
- `information_completeness`: number between 0 and 1 measuring how complete the
  demand information is;
- `reason`: non-empty Simplified Chinese string;
- `evidence`: array of short strings grounded in CRM data;
- `message_evidence`: object containing:
  - `status`: `sufficient` or `insufficient`;
  - `customer_action_quote`: exact substring of `lead.internal_note` or `null`;
  - `business_detail_quote`: exact substring of `lead.internal_note` or `null`;
  - `reason`: non-empty Simplified Chinese string;
- `sales_follow_up_context`: object containing:
  - `status`: `none`, `sales_replied`, or `information_sent`;
  - `evidence_quote`: exact substring of `lead.internal_note` when status is not
    `none`, otherwise `null`;
- `contact_permission`: object containing:
  - `status`: `allowed` or `do_not_contact`;
  - `evidence_quote`: exact substring of `lead.internal_note` for
    `do_not_contact`, otherwise `null`;
- `customer_used_sender_name`: object or `null`. When an unambiguous pasted
  customer reply directly addresses the Aceler salesperson, the object contains
  the addressed `name` and an exact supporting `evidence_quote` from
  `lead.internal_note`. It never changes the current contact;
- `recommended_by`: array of recommenders for the current contact. Each item
  contains an exact recommender `name` and an exact supporting `evidence_quote`
  from `lead.internal_note`. The current `contact` remains the recipient;
- `referral_relationship`: object or `null`. For a fully grounded referral, the
  object contains `current_contact_role` (`recommender` or `referred`) and
  `related_contacts`, an array of the people mentioned in `lead.internal_note`
  on the other side of that relationship. Each related contact contains an exact
  `name` and an exact supporting `evidence_quote`. When the current contact is
  the recommender, the related contacts are the referred people; when the
  current contact is referred, the related contacts are the recommenders;
- `review_required`: always `true`.

Low information completeness does not require low confidence when
`unknown_demand` is clearly the correct category.

Do not include message content, a channel decision, a follow-up date, Markdown,
or commentary.
