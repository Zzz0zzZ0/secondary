import copy
from typing import Any, Dict, List, Optional

from .sender_identity import resolve_sender_identity


GENERATION_BLOCK_WARNING = (
    "MESSAGE_GENERATION_BLOCKED_INSUFFICIENT_CRM_EVIDENCE"
)
COMPANY_CONTEXT_WARNINGS = {
    "COMPANY_CONTEXT_MISSING",
    "COMPANY_CONTEXT_THIN",
}


def _mapped_sender_name(record: Dict[str, Any]) -> Optional[str]:
    sales_name = (record.get("sales") or {}).get("name")
    identity = resolve_sender_identity(sales_name)
    return identity["display_name"] if identity is not None else None


def generation_eligibility(
    record: Dict[str, Any],
    classification: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    lead = record.get("lead") or {}
    source_version = record.get("source_version") or {}
    has_reliable_follow_up = bool(
        str(lead.get("last_follow_up_at") or "").strip()
        and source_version.get("activity_at_source") == "lastFollowUp"
    )
    assessment = (
        classification.get("message_evidence")
        if isinstance(classification, dict)
        else None
    )
    has_explicit_crm_evidence = bool(
        isinstance(assessment, dict)
        and assessment.get("status") == "sufficient"
    )
    allowed = has_reliable_follow_up or has_explicit_crm_evidence
    reason_codes: List[str] = []
    if not has_reliable_follow_up:
        reason_codes.append("FOLLOW_UP_TIME_MISSING")
    if not has_explicit_crm_evidence:
        reason_codes.append("CRM_EVIDENCE_INSUFFICIENT")
    return {
        "allowed": allowed,
        "timing_reliable": has_reliable_follow_up,
        "explicit_crm_evidence": has_explicit_crm_evidence,
        "reason_codes": [] if allowed else reason_codes,
    }


def prepare_message_record(
    source_record: Dict[str, Any],
    classification: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    record = copy.deepcopy(source_record)
    lead = record.setdefault("lead", {})
    internal_note = lead.pop("internal_note", None)
    record["review_context"] = {
        "crm_internal_note": internal_note,
    }
    if isinstance(classification, dict):
        assessment = classification.get("message_evidence")
        if isinstance(assessment, dict):
            record["crm_message_evidence"] = copy.deepcopy(assessment)
        permission = classification.get("contact_permission")
        if isinstance(permission, dict):
            record["contact_permission"] = copy.deepcopy(permission)
        sender_name = classification.get("customer_used_sender_name")
        if (
            isinstance(sender_name, dict)
            and isinstance(sender_name.get("name"), str)
            and sender_name["name"].strip()
        ):
            record["conversation_sender_identity"] = {
                "name": sender_name["name"].strip(),
                "evidence_quote": sender_name.get("evidence_quote"),
                "source": "classification.customer_used_sender_name",
            }
        relationship = classification.get("referral_relationship")
        if isinstance(relationship, dict):
            current_role = relationship.get("current_contact_role")
            related_contacts = [
                copy.deepcopy(item)
                for item in relationship.get("related_contacts", [])
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].strip()
            ]
            if current_role in {"recommender", "referred"} and related_contacts:
                record["referral_context"] = {
                    "current_contact_role": current_role,
                    "related_contacts": related_contacts,
                    "source": "classification.referral_relationship",
                }
                names = [item["name"].strip() for item in related_contacts]
                if current_role == "referred":
                    lead["recommended_by"] = " and ".join(names)
                else:
                    lead["referred_contacts"] = " and ".join(names)

        recommended_by = classification.get("recommended_by")
        if (
            "referral_context" not in record
            and isinstance(recommended_by, list)
            and recommended_by
        ):
            recommenders = [
                copy.deepcopy(item)
                for item in recommended_by
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].strip()
            ]
            names = [item["name"].strip() for item in recommenders]
            current_contact_name = str(
                (record.get("contact") or {}).get("name") or ""
            ).strip()
            if current_contact_name and any(
                name.casefold() == current_contact_name.casefold() for name in names
            ):
                record["referral_context"] = {
                    "current_contact_role": "recommender",
                    "related_contacts": [],
                    "source": "legacy.classification.recommended_by",
                }
            else:
                lead["recommended_by"] = " and ".join(names)
                record["referral_context"] = {
                    "current_contact_role": "referred",
                    "related_contacts": recommenders,
                    "source": "legacy.classification.recommended_by",
                }

    eligibility = generation_eligibility(source_record, classification)
    record["message_generation_eligibility"] = eligibility
    if not eligibility["allowed"]:
        warnings = record.setdefault("warnings", [])
        if GENERATION_BLOCK_WARNING not in warnings:
            warnings.append(GENERATION_BLOCK_WARNING)
    return record


def normalize_candidate(
    candidate: Dict[str, Any],
    crm_input: Dict[str, Any],
) -> Dict[str, Any]:
    normalized = copy.deepcopy(candidate)
    candidate_warnings = normalized.get("warnings")
    if not isinstance(candidate_warnings, list):
        return normalized
    input_warnings = crm_input.get("warnings")
    if not isinstance(input_warnings, list):
        input_warnings = []
    retained = [
        warning
        for warning in candidate_warnings
        if warning not in COMPANY_CONTEXT_WARNINGS
    ]
    retained.extend(
        warning
        for warning in input_warnings
        if warning in COMPANY_CONTEXT_WARNINGS
    )
    normalized["warnings"] = sorted(set(retained))
    return normalized


def validation_errors(
    candidate: Any,
    crm_input: Dict[str, Any],
    lead_id: str,
) -> List[str]:
    if not isinstance(candidate, dict):
        return ["Hermes返回内容不是有效JSON"]
    errors: List[str] = []
    content = candidate.get("content")
    decision = candidate.get("decision")
    output_type = candidate.get("output_type")
    if candidate.get("lead_id") != lead_id:
        errors.append("记录ID与CRM不一致")
    if decision not in {"generated", "no_message", "cannot_generate"}:
        errors.append("decision取值不合法")
    if output_type not in {"email", "linkedin"}:
        errors.append("output_type取值不合法")
    if output_type != (crm_input.get("output") or {}).get("type"):
        errors.append("输出渠道与CRM任务不一致")
    if not isinstance(content, dict):
        errors.append("content结构缺失")
        content = {}
    elif any(
        key not in content
        for key in ("subject", "subject_zh", "body", "body_zh")
    ):
        errors.append("content缺少双语字段")
    message_goal = candidate.get("message_goal")
    if message_goal is not None and not isinstance(message_goal, str):
        errors.append("消息目标格式不合法")
    information_requested = candidate.get("information_requested")
    if not isinstance(information_requested, list) or any(
        not isinstance(item, str) for item in information_requested or []
    ):
        errors.append("待补充信息必须为数组")
    candidate_warnings = candidate.get("warnings")
    if not isinstance(candidate_warnings, list) or any(
        not isinstance(item, str) for item in candidate_warnings or []
    ):
        errors.append("内部提醒必须为数组")
    if not isinstance(candidate.get("reason"), str) or not candidate.get("reason"):
        errors.append("缺少判断理由")
    if candidate.get("review_required") is not True:
        errors.append("review_required未明确设为true")
    input_warnings = (
        crm_input.get("warnings")
        if isinstance(crm_input.get("warnings"), list)
        else []
    )
    permission = crm_input.get("contact_permission") or {}
    if (
        "DO_NOT_CONTACT" in input_warnings
        or permission.get("status") == "do_not_contact"
    ) and decision != "no_message":
        errors.append("CRM禁止联系时只能返回no_message")
    eligibility = crm_input.get("message_generation_eligibility") or {}
    if eligibility.get("allowed") is False and decision != "cannot_generate":
        errors.append(
            "CRM信息不足，系统禁止生成客户消息"
            if decision == "generated"
            else "CRM信息不足时只能返回cannot_generate"
        )

    if decision == "generated":
        if not isinstance(content.get("body"), str) or not content.get("body"):
            errors.append("客户正文为空")
        if (
            not isinstance(content.get("body_zh"), str)
            or not content.get("body_zh")
        ):
            errors.append("缺少中文正文对照")
        if output_type == "email":
            if (
                not isinstance(content.get("subject"), str)
                or not content.get("subject")
            ):
                errors.append("邮件主题为空")
            if (
                not isinstance(content.get("subject_zh"), str)
                or not content.get("subject_zh")
            ):
                errors.append("缺少中文主题对照")
        elif (
            content.get("subject") is not None
            or content.get("subject_zh") is not None
        ):
            errors.append("LinkedIn消息不应包含主题")
        sender_identity = crm_input.get("conversation_sender_identity") or {}
        sender_name = _mapped_sender_name(crm_input) or sender_identity.get("name")
        if output_type == "email" and isinstance(sender_name, str):
            for field, label in (
                ("body", "客户正文"),
                ("body_zh", "中文正文对照"),
            ):
                body = content.get(field)
                lines = (
                    [line.strip() for line in body.splitlines() if line.strip()]
                    if isinstance(body, str)
                    else []
                )
                if sender_name not in lines[-4:]:
                    errors.append(
                        f"{label}未使用客户确认过的发件人称呼"
                    )
    elif any(
        content.get(key) is not None
        for key in ("subject", "subject_zh", "body", "body_zh")
    ):
        errors.append("不输出时主题和正文必须为空")

    warnings = (
        candidate.get("warnings")
        if isinstance(candidate.get("warnings"), list)
        else []
    )
    for code, label in (
        ("COMPANY_CONTEXT_MISSING", "公司背调缺失"),
        ("COMPANY_CONTEXT_THIN", "公司背调信息不足"),
    ):
        expected = code in input_warnings
        actual = code in warnings
        if actual and not expected:
            errors.append(f"Hermes自行添加了“{label}”标记")
        elif expected and not actual:
            errors.append(f"Hermes遗漏了CRM提供的“{label}”标记")
    return errors
