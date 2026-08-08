"""Seed three test outbox rows for the three required sales reps.

Each row bypasses the same-channel / same-recipient exclusion logic by
using a `SYSTEM-TEST-` prefixed lead_id in the payload (see
app/delivery_service.py::claim_delivery).

Sales -> sender account (per skill/generate-secondary-lead-message/
references/sender-identity-map.md):
  倩文 于 -> chloe@okgminerals.com
  阳淮 李 -> dean@okgminerals.com
  铖 沈   -> xiaoxue@okgminerals.com

Recipient for all three: nikola@okgmineral.com
"""

import json
import os
import sys
import uuid
from pathlib import Path

# Make `app.*` importable when running as a script.
PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Load config/local.env into the environment so app.db.connect works
# without an interactive password prompt.
for line in (PROJECT_DIR / "config" / "local.env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    os.environ.setdefault(key.strip(), value.strip())

from app.db import connect  # noqa: E402
from app.secondary.sender_identity import resolve_sender_identity  # noqa: E402


RECIPIENT = "nikola@okgmineral.com"
CHANNEL = "email"
PROVIDER = "email"

SALES_FIXTURES = [
    ("倩文 于", "SYSTEM-TEST-PER-SALES-CHLOE-FRESH"),
    ("阳淮 李", "SYSTEM-TEST-PER-SALES-DEAN-FRESH"),
    ("铖 沈",   "SYSTEM-TEST-PER-SALES-XIAOXUE-FRESH"),
]


def build_crm_snapshot(sales_name, recipient):
    return {
        "schema_version": "1.0",
        "lead": {
            "id": None,
            "name": "Nikola",
            "last_follow_up_at": "2026-08-01T00:00:00Z",
        },
        "contact": {
            "name": "Nikola",
            "email": recipient,
        },
        "sales": {"name": sales_name},
        "company": {"name": "OKG Mineral"},
        "output": {"type": CHANNEL},
    }


def build_original_output(recipient):
    return {
        "decision": "generated",
        "output_type": CHANNEL,
        "language": "English",
        "message_goal": "Follow up on a recent conversation",
        "content": {
            "subject": "Following up",
            "subject_zh": "跟进一下",
            "body": "Hi Nikola,\n\nJust checking back in.\n\nBest",
            "body_zh": "你好 Nikola，\n\n再跟您确认一下。\n\n顺颂商祺",
        },
        "information_requested": [],
        "warnings": [],
        "reason": "测试用例，由脚本直插 outbox",
        "review_required": True,
    }


def build_payload(lead_id, recipient, sales_name):
    return {
        "lead_id": lead_id,
        "to": recipient,
        "subject": "Following up",
        "body": "Hi Nikola,\n\nJust checking back in.\n\nBest",
        "body_html": None,
        "language": "English",
        "contact_name": "Nikola",
        "company_name": "OKG Mineral",
        "sales_name": sales_name,
    }


def insert_one(cursor, sales_name, lead_id):
    sender = resolve_sender_identity(sales_name)
    if sender is None:
        raise RuntimeError(f"No sender identity for: {sales_name}")
    account = sender["account"]
    sender_account_ref = f"email:{account}"

    run_id = f"system-test-{uuid.uuid4().hex[:8]}"
    message_id = uuid.uuid4()
    original_output = build_original_output(RECIPIENT)
    crm_snapshot = build_crm_snapshot(sales_name, RECIPIENT)

    # message_version row (FK target for delivery_outbox)
    cursor.execute(
        """
        INSERT INTO sales_automation.message_version
          (id, run_id, lead_id, channel, version, recipient_original,
           crm_snapshot, original_output, review_status)
        VALUES (%s, %s, %s, %s, 1, %s, %s::jsonb, %s::jsonb, 'approved')
        """,
        (
            message_id,
            run_id,
            lead_id,
            CHANNEL,
            RECIPIENT,
            json.dumps(crm_snapshot, ensure_ascii=False),
            json.dumps(original_output, ensure_ascii=False),
        ),
    )

    payload = build_payload(lead_id, RECIPIENT, sales_name)
    payload_canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    import hashlib as _hashlib
    digest = _hashlib.sha256(payload_canonical.encode("utf-8")).hexdigest()
    outbox_id = uuid.uuid4()
    idempotency_key = f"{PROVIDER}:{message_id}:v1"

    # delivery_outbox row
    cursor.execute(
        """
        INSERT INTO sales_automation.delivery_outbox
          (id, message_version_id, channel, provider, recipient_original,
           sender_account_ref, payload, payload_sha256, idempotency_key,
           status, attempt_count, max_attempts, available_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                'queued', 0, 5, now())
        """,
        (
            outbox_id,
            message_id,
            CHANNEL,
            PROVIDER,
            RECIPIENT,
            sender_account_ref,
            payload_canonical,
            digest,
            idempotency_key,
        ),
    )
    return {
        "outbox_id": str(outbox_id),
        "message_id": str(message_id),
        "lead_id": lead_id,
        "sender_account_ref": sender_account_ref,
        "sender_account": account,
    }


def main():
    inserted = []
    with connect() as conn, conn.cursor() as cursor:
        for sales_name, lead_id in SALES_FIXTURES:
            inserted.append(insert_one(cursor, sales_name, lead_id))
        conn.commit()

    print(f"Inserted {len(inserted)} outbox rows:")
    for row in inserted:
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()
