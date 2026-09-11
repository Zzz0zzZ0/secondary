"""One upcoming draft per contact, with an independent delivery deadline."""
from datetime import timedelta

from app.message_jobs import parse_timestamp
from .domain import interval_days


def review_lead_days(lead_type, lead_id, sequence):
    if lead_type is None:
        return 0
    return 7 if interval_days(lead_type, lead_id, sequence) > 30 else 3


def draft_at(row):
    at = parse_timestamp(row["next_action_at"]) - timedelta(days=review_lead_days(
        row["lead_type"], row["lead_id"], int(row["follow_up_count"])
    ))
    if row["generation_retry_at"]:
        at = max(at, parse_timestamp(row["generation_retry_at"]))
    return at


def follow_up_at(snapshot):
    # A reply to a new customer message can be reviewed immediately.
    if snapshot.get("message_route") == "email_reply":
        return None
    lead = snapshot.get("lead") or {}
    contact = snapshot.get("contact") or {}
    candidates = [
        (snapshot.get("review_schedule") or {}).get("follow_up_at"),
        (snapshot.get("secondary_lead_schedule") or {}).get("next_action_at"),
        (snapshot.get("follow_up_schedule") or {}).get("effective_next_follow_up_at"),
        lead.get("next_eligible_follow_up_at"),
        lead.get("next_follow_up_at") or lead.get("nextFollowUp"),
        contact.get("next_follow_up_at") or contact.get("nextFollowUp"),
    ]
    return max((parse_timestamp(str(value)) for value in candidates if value), default=None)
