from typing import Any, Dict, Optional
from .domain import LEAD_TYPES


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Classification {field} is missing")
    return value.strip()


def _optional_quote(value: Any, note: str, field: str) -> Optional[str]:
    if value is None:
        return None
    quote = _required_text(value, field)
    if quote not in note:
        raise RuntimeError(
            f"Classification {field} must quote lead.internal_note exactly"
        )
    return quote


def _grounded_people(value: Any, note: str, field: str) -> list:
    if not isinstance(value, list):
        raise RuntimeError(f"Classification {field} must be an array")
    normalized = []
    seen_names = set()
    for index, person in enumerate(value):
        if not isinstance(person, dict):
            raise RuntimeError(f"Classification {field}[{index}] is invalid")
        name = _required_text(person.get("name"), f"{field}[{index}].name")
        quote = _optional_quote(
            person.get("evidence_quote"),
            note,
            f"{field}[{index}].evidence_quote",
        )
        if name not in note or quote is None or name not in quote:
            raise RuntimeError(
                f"Classification {field} must be fully grounded in CRM text"
            )
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise RuntimeError(f"Classification {field} contains duplicates")
        seen_names.add(normalized_name)
        normalized.append({"name": name, "evidence_quote": quote})
    return normalized


def validate_classification(
    candidate: Dict[str, Any],
    record: Dict[str, Any],
) -> Dict[str, Any]:
    lead = record.get("lead") or {}
    lead_id = str(lead.get("id") or "")
    note = str(lead.get("internal_note") or "")
    if candidate.get("lead_id") != lead_id:
        raise RuntimeError("Classification lead_id does not match CRM")
    if candidate.get("lead_type") not in LEAD_TYPES:
        raise RuntimeError("Classification lead_type is invalid")
    for field in ("confidence", "information_completeness"):
        value = candidate.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or value > 1
        ):
            raise RuntimeError(
                f"Classification {field} must be between 0 and 1"
            )
    _required_text(candidate.get("reason"), "reason")
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or any(
        not isinstance(item, str) for item in evidence
    ):
        raise RuntimeError("Classification evidence must be a string array")
    if candidate.get("review_required") is not True:
        raise RuntimeError("Classification must require review")

    assessment = candidate.get("message_evidence")
    if not isinstance(assessment, dict):
        raise RuntimeError("Classification message_evidence is missing")
    status = assessment.get("status")
    if status not in {"sufficient", "insufficient"}:
        raise RuntimeError("Classification message_evidence.status is invalid")
    customer_quote = _optional_quote(
        assessment.get("customer_action_quote"),
        note,
        "message_evidence.customer_action_quote",
    )
    business_quote = _optional_quote(
        assessment.get("business_detail_quote"),
        note,
        "message_evidence.business_detail_quote",
    )
    assessment["reason"] = _required_text(
        assessment.get("reason"),
        "message_evidence.reason",
    )

    follow_up = candidate.get("sales_follow_up_context")
    if not isinstance(follow_up, dict):
        raise RuntimeError("Classification sales_follow_up_context is missing")
    follow_up_status = follow_up.get("status")
    if follow_up_status not in {"none", "sales_replied", "information_sent"}:
        raise RuntimeError("Classification sales_follow_up_context.status is invalid")
    follow_up_quote = _optional_quote(
        follow_up.get("evidence_quote"),
        note,
        "sales_follow_up_context.evidence_quote",
    )
    if follow_up_status == "none" and follow_up_quote is not None:
        raise RuntimeError(
            "Classification sales_follow_up_context.none cannot carry evidence"
        )
    if follow_up_status != "none" and follow_up_quote is None:
        raise RuntimeError(
            "Classification sales follow-up context must quote CRM text exactly"
        )
    candidate["sales_follow_up_context"] = {
        "status": follow_up_status,
        "evidence_quote": follow_up_quote,
    }

    normalized_recommenders = _grounded_people(
        candidate.get("recommended_by"), note, "recommended_by"
    )
    candidate["recommended_by"] = normalized_recommenders
    if candidate.get("lead_type") != "referred" and normalized_recommenders:
        raise RuntimeError(
            "Classification recommended_by is only valid for referred leads"
        )

    if "referral_relationship" not in candidate:
        raise RuntimeError("Classification referral_relationship is missing")
    relationship = candidate.get("referral_relationship")
    if relationship is not None:
        if candidate.get("lead_type") != "referred" or not isinstance(
            relationship, dict
        ):
            raise RuntimeError("Classification referral_relationship is invalid")
        current_role = relationship.get("current_contact_role")
        if current_role not in {"recommender", "referred"}:
            raise RuntimeError(
                "Classification referral_relationship.current_contact_role is invalid"
            )
        related_contacts = _grounded_people(
            relationship.get("related_contacts"),
            note,
            "referral_relationship.related_contacts",
        )
        if not related_contacts:
            raise RuntimeError(
                "Classification referral_relationship.related_contacts is empty"
            )
        if (
            current_role == "referred"
            and normalized_recommenders != related_contacts
        ):
            raise RuntimeError(
                "Classification recommended_by must match referral relationship"
            )
        if current_role == "recommender" and normalized_recommenders:
            candidate["recommended_by"] = []
        candidate["referral_relationship"] = {
            "current_contact_role": current_role,
            "related_contacts": related_contacts,
        }

    structured_demands = lead.get("product_demands")
    has_structured_demand = (
        isinstance(structured_demands, list) and bool(structured_demands)
    )
    has_grounded_text = bool(customer_quote and business_quote)
    has_grounded_referral = bool(relationship)
    evidence_is_sufficient = (
        has_structured_demand or has_grounded_text or has_grounded_referral
    )
    if status == "sufficient" and not evidence_is_sufficient:
        raise RuntimeError(
            "Classification sufficient message evidence is not grounded"
        )
    if status == "insufficient" and evidence_is_sufficient:
        assessment["status"] = "sufficient"

    permission = candidate.get("contact_permission")
    if not isinstance(permission, dict):
        raise RuntimeError("Classification contact_permission is missing")
    permission_status = permission.get("status")
    if permission_status not in {"allowed", "do_not_contact"}:
        raise RuntimeError("Classification contact_permission.status is invalid")
    permission_quote = _optional_quote(
        permission.get("evidence_quote"),
        note,
        "contact_permission.evidence_quote",
    )
    if permission_status == "do_not_contact" and permission_quote is None:
        raise RuntimeError(
            "Classification do_not_contact must quote CRM text exactly"
        )
    if permission_status == "allowed" and permission_quote is not None:
        raise RuntimeError(
            "Classification allowed contact permission cannot carry evidence"
        )

    if "customer_used_sender_name" not in candidate:
        raise RuntimeError(
            "Classification customer_used_sender_name is missing"
        )
    sender_name = candidate.get("customer_used_sender_name")
    if sender_name is not None:
        if not isinstance(sender_name, dict):
            raise RuntimeError(
                "Classification customer_used_sender_name is invalid"
            )
        name = _required_text(
            sender_name.get("name"),
            "customer_used_sender_name.name",
        )
        quote = _optional_quote(
            sender_name.get("evidence_quote"),
            note,
            "customer_used_sender_name.evidence_quote",
        )
        if quote is None or name not in quote:
            raise RuntimeError(
                "Classification customer_used_sender_name must be fully "
                "grounded in CRM text"
            )
        candidate["customer_used_sender_name"] = {
            "name": name,
            "evidence_quote": quote,
        }
    return candidate
