import copy
from typing import Any, Dict, List, Optional

from .sender_identity import resolve_sender_identity


MANUAL_CONFIRMATION_WARNING = "CRM_EVIDENCE_REQUIRES_MANUAL_CONFIRMATION"
NOTES_REVIEW_WARNING = "NOTES_REVIEW_ONLY_REQUIRES_MANUAL_REVIEW"
COMPANY_CONTEXT_WARNINGS = {
    "COMPANY_CONTEXT_MISSING",
    "COMPANY_CONTEXT_THIN",
}
MESSAGE_ROUTES = {
    "email_reply",
    "recommender_thanks",
    "referred_intro",
    "qualification",
    "conversation_follow_up",
    "referral_review",
    "default",
}


def message_route(
    source_record: Dict[str, Any],
    classification: Optional[Dict[str, Any]],
) -> str:
    """Choose the smallest safe message task without changing CRM lead type."""
    lead = source_record.get("lead") or {}
    lead_type = classification.get("lead_type") if isinstance(classification, dict) else None
    raw_type = lead.get("raw_type")
    if raw_type is None:
        raw_type = lead.get("subtype")
    raw_types = raw_type if isinstance(raw_type, list) else [raw_type]
    referred = lead_type == "referred" or any(
        item in {"RECOMMEND", "RECOMMENDED", "（被）推荐"}
        for item in raw_types
    )
    relationship = (
        classification.get("referral_relationship")
        if isinstance(classification, dict)
        else source_record.get("referral_context")
    )
    follow_up = (
        classification.get("sales_follow_up_context")
        if isinstance(classification, dict)
        else source_record.get("sales_follow_up_context")
    )
    notes_evidence = (source_record.get("review_context") or {}).get(
        "crm_email_evidence"
    )
    if (
        source_record.get("notes_review_only") is True
        or source_record.get("notes_experiment") is True
    ) and isinstance(
        notes_evidence, dict
    ):
        return "email_reply"
    if lead_type == "below_moq" or "SMALL_QUANTITY" in raw_types:
        return "default"
    if (
        isinstance(follow_up, dict)
        and follow_up.get("status") in {"sales_replied", "information_sent"}
    ):
        return "conversation_follow_up"
    if referred:
        role = relationship.get("current_contact_role") if isinstance(relationship, dict) else None
        if role == "recommender":
            return "recommender_thanks"
        if role == "referred":
            return "referred_intro"
        return "referral_review"
    if lead_type == "no_current_demand":
        return "default"
    return "qualification"


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
    follow_up = (
        classification.get("sales_follow_up_context")
        if isinstance(classification, dict)
        else None
    )
    if (
        isinstance(follow_up, dict)
        and follow_up.get("status") in {"sales_replied", "information_sent"}
    ):
        has_explicit_crm_evidence = True
    reason_codes: List[str] = []
    if not has_reliable_follow_up:
        reason_codes.append("FOLLOW_UP_TIME_MISSING")
    if not has_explicit_crm_evidence:
        reason_codes.append("CRM_EVIDENCE_INSUFFICIENT")
    return {
        "allowed": True,
        "timing_reliable": has_reliable_follow_up,
        "explicit_crm_evidence": has_explicit_crm_evidence,
        "requires_manual_confirmation": bool(reason_codes),
        "reason_codes": reason_codes,
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
    record["message_route"] = message_route(source_record, classification)
    if isinstance(classification, dict):
        assessment = classification.get("message_evidence")
        if isinstance(assessment, dict):
            record["crm_message_evidence"] = copy.deepcopy(assessment)
        follow_up = classification.get("sales_follow_up_context")
        if isinstance(follow_up, dict):
            record["sales_follow_up_context"] = copy.deepcopy(follow_up)
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
    if eligibility["requires_manual_confirmation"]:
        warnings = record.setdefault("warnings", [])
        if MANUAL_CONFIRMATION_WARNING not in warnings:
            warnings.append(MANUAL_CONFIRMATION_WARNING)
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
        or warning == MANUAL_CONFIRMATION_WARNING
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
    reason = candidate.get("reason")
    if not isinstance(reason, str) or not reason:
        errors.append("缺少判断理由")
    if candidate.get("review_required") is not True:
        errors.append("review_required未明确设为true")
    route = message_route(crm_input, None)
    explicit_route = crm_input.get("message_route")
    if explicit_route in MESSAGE_ROUTES:
        route = explicit_route
    if route not in MESSAGE_ROUTES:
        errors.append("message_route缺失或不合法")
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
        if route == "referral_review":
            errors.append("推荐关系未确认时不得生成客户消息")
        if route == "conversation_follow_up":
            context = crm_input.get("sales_follow_up_context") or {}
            if context.get("status") not in {"sales_replied", "information_sent"}:
                errors.append("简短跟进缺少已验证的销售动作")
            if (
                isinstance(information_requested, list)
                and len(information_requested) > 1
            ):
                errors.append("简短跟进一次最多询问一个事项")
        if route == "email_reply":
            context = (crm_input.get("review_context") or {}).get(
                "crm_email_evidence"
            ) or {}
            quote = context.get("evidence_quote")
            if not isinstance(quote, str) or not quote.strip():
                errors.append("邮件回复缺少客户来信原文证据")
            if (
                not isinstance(candidate_warnings, list)
                or NOTES_REVIEW_WARNING not in candidate_warnings
            ):
                errors.append("Notes审阅消息必须保留人工审阅标记")
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
    sender_identity = crm_input.get("conversation_sender_identity") or {}
    sender_name = sender_identity.get("name") or _mapped_sender_name(crm_input)
    if decision == "generated" and output_type == "email" and isinstance(sender_name, str):
        for field, label in (("body", "客户正文"), ("body_zh", "中文正文对照")):
            body = content.get(field)
            lines = (
                [line.strip() for line in body.splitlines() if line.strip()]
                if isinstance(body, str)
                else []
            )
            if sender_name not in lines[-4:]:
                errors.append(f"{label}未使用客户确认过的发件人称呼")
    if (
        MANUAL_CONFIRMATION_WARNING in input_warnings
        and MANUAL_CONFIRMATION_WARNING not in warnings
    ):
        errors.append("CRM证据不足时必须标记为需要人工确认")
    return errors
