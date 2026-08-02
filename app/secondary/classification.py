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

    recommended_by = candidate.get("recommended_by")
    if not isinstance(recommended_by, list):
        raise RuntimeError("Classification recommended_by must be an array")
    normalized_recommenders = []
    seen_names = set()
    for index, recommender in enumerate(recommended_by):
        if not isinstance(recommender, dict):
            raise RuntimeError(
                f"Classification recommended_by[{index}] is invalid"
            )
        name = _required_text(
            recommender.get("name"),
            f"recommended_by[{index}].name",
        )
        quote = _optional_quote(
            recommender.get("evidence_quote"),
            note,
            f"recommended_by[{index}].evidence_quote",
        )
        if name not in note or quote is None or name not in quote:
            raise RuntimeError(
                "Classification recommended_by must be fully grounded in CRM text"
            )
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise RuntimeError("Classification recommended_by contains duplicates")
        seen_names.add(normalized_name)
        normalized_recommenders.append(
            {
                "name": name,
                "evidence_quote": quote,
            }
        )
    candidate["recommended_by"] = normalized_recommenders
    if candidate.get("lead_type") != "referred" and normalized_recommenders:
        raise RuntimeError(
            "Classification recommended_by is only valid for referred leads"
        )

    structured_demands = lead.get("product_demands")
    has_structured_demand = (
        isinstance(structured_demands, list) and bool(structured_demands)
    )
    has_grounded_text = bool(customer_quote and business_quote)
    has_grounded_referral = bool(normalized_recommenders)
    evidence_is_sufficient = (
        has_structured_demand or has_grounded_text or has_grounded_referral
    )
    if status == "sufficient" and not evidence_is_sufficient:
        raise RuntimeError(
            "Classification sufficient message evidence is not grounded"
        )
    if status == "insufficient" and evidence_is_sufficient:
        raise RuntimeError(
            "Classification message evidence contradicts grounded fields"
        )

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
