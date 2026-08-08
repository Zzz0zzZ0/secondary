# Output Schema

Return one JSON object and no surrounding Markdown.

```json
{
  "decision": "generated | no_message | cannot_generate",
  "lead_id": "CRM record ID",
  "output_type": "email | linkedin",
  "language": "English",
  "content": {
    "subject": "Email subject or null",
    "subject_zh": "Chinese subject translation or null",
    "body": "Customer-facing message or null",
    "body_zh": "Chinese body translation or null"
  },
  "message_goal": "中文内部下一步说明或null",
  "information_requested": [],
  "warnings": [],
  "reason": "中文内部判断理由",
  "review_required": true
}
```

## Field Rules

- Always set `review_required` to `true`.
- For `generated`, set `content.body` and `content.body_zh` to non-empty messages with identical meaning.
- For email `generated`, set `content.subject` and `content.subject_zh` to non-empty subjects with identical meaning.
- For LinkedIn, always set both subject fields to `null`.
- For `no_message` and `cannot_generate`, set all four content fields to `null`.
- Write `message_goal`, all `information_requested` items, and `reason` in Simplified Chinese.
- Keep `warnings`, `reason`, and `message_goal` internal. Never repeat their codes or wording in the customer-facing body.
- Use stable uppercase warning codes.

## Recommended Warning Codes

- `COMPANY_CONTEXT_MISSING`
- `COMPANY_CONTEXT_THIN`
- `NON_CATALOG_PRODUCT_REQUIRES_FACTORY_CONFIRMATION`
- `CONTACT_NAME_MISSING`
- `PRODUCT_FACTS_MISSING`
- `INTERNAL_CONFIRMATION_REQUIRED`
- `CUSTOMER_REFERENCE_DISCLOSURE_REQUIRES_APPROVAL`
- `SUPPLY_RELATIONSHIP_UNVERIFIED`
- `PRICE_REQUIRES_MANUAL_CONFIRMATION`
- `AVAILABILITY_REQUIRES_MANUAL_CONFIRMATION`
- `DELIVERY_REQUIRES_MANUAL_CONFIRMATION`
- `DOCUMENT_AVAILABILITY_UNVERIFIED`
- `MISSING_SALES_SIGNATURE`
- `SALES_NAME_TRANSLITERATED`
- `SALES_NAME_TRANSLITERATION_UNCERTAIN`
- `SALES_ENGLISH_NAME_MISSING`
- `RECENT_NO_CURRENT_DEMAND`
- `DO_NOT_CONTACT`
- `FOLLOW_UP_DATE_NOT_REACHED`
- `FOLLOW_UP_TIMING_UNVERIFIED`
- `CRM_EVIDENCE_REQUIRES_MANUAL_CONFIRMATION`
- `REFERRAL_CONTEXT_MISSING`
- `UNKNOWN_DEMAND_ATTEMPT_LIMIT_REACHED`
- `REFERRED_CONTACT_ATTEMPT_LIMIT_REACHED`

`COMPANY_CONTEXT_MISSING` and `COMPANY_CONTEXT_THIN` must match the input
warnings exactly. Never add either code based on Hermes's own assessment of the
company background.
- `INSUFFICIENT_ORDER_QUANTITY`
