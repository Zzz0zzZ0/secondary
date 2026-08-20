#!/usr/bin/env python3
"""Refresh only legacy pending-review messages affected by message routing."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import shlex
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip():
            os.environ.setdefault(key.strip(), shlex.split(value, comments=True)[0] if value.strip() else "")


def instruction_for(route: str) -> str:
    return {
        "recommender_thanks": (
            "按 message_route=recommender_thanks 重写。当前联系人是推荐人；只感谢推荐、"
            "确认收到并在 CRM 支持时请求引荐、转发或提醒。禁止询问当前联系人任何产品、规格、数量、应用、采购或需求。"
        ),
        "referred_intro": (
            "按 message_route=referred_intro 重写。当前联系人是被推荐人；自然提及已核实的推荐人，"
            "做简短首次介绍，只保留一个最小业务问题，不重复客户已提供的信息。"
        ),
        "qualification": (
            "按 message_route=qualification 重写。当前记录仍是二级线索，不得写成实际询盘回复；"
            "用一个保守、低负担的问题确认产品方向、用途、规格或数量，禁止声称客户已经提出正式询盘。"
        ),
        "conversation_follow_up": (
            "按 message_route=conversation_follow_up 重写。销售已经回复或发送资料；只做简短跟进，"
            "禁止重新介绍公司、声称客户提出询盘或感谢客户兴趣。最多问一个低负担问题，不得虚构此前回复内容。"
        ),
    }[route]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh legacy pending review messages")
    parser.add_argument("--env-file", type=Path, default=PROJECT_DIR / "config" / "local.env")
    parser.add_argument("--max", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-fallbacks", action="store_true")
    args = parser.parse_args(argv)
    if args.max < 1:
        parser.error("--max must be positive")
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")
    load_env(args.env_file)

    from app.outbox import list_review_messages, reject_message
    from app.secondary.message_policy import message_route
    from app.secondary.sender_identity import resolve_sender_identity
    from app.outbox import replace_message_draft
    from review_api.service import regenerate_review_message

    records = list_review_messages(args.max, "pending_review")
    targets = []
    for record in records:
        snapshot = record["crm_snapshot"]
        route = message_route(snapshot, None)
        fallback_reason = str((record.get("edited_output") or {}).get("reason") or "")
        is_fallback = "本地代理暂不可用" in fallback_reason
        if args.retry_fallbacks and not is_fallback:
            continue
        if route in {
            "qualification",
            "conversation_follow_up",
            "recommender_thanks",
            "referred_intro",
            "referral_review",
        }:
            if route == "qualification" and not args.retry_fallbacks:
                continue
            # Preserve an existing human-edited draft on reruns; ambiguous
            # referral messages still must be rejected regardless of draft state.
            if (
                not args.retry_fallbacks
                and route != "referral_review"
                and record.get("edited_output") is not None
            ):
                continue
            targets.append((record, route))

    print(
        f"pending={len(records)} targets={len(targets)} dry_run={args.dry_run} workers={args.workers}",
        flush=True,
    )
    if args.dry_run:
        for record, route in targets:
            print(f"{record['id']} {route}", flush=True)
        print("refreshed=0 rejected=0 failed=0", flush=True)
        return 0

    def process(record_route):
        record, route = record_route
        message_id = record["id"]
        if route == "referral_review":
            reject_message(
                message_id,
                "route-refresh",
                "旧消息不符合新规则：推荐关系方向未确认，已停止发送并转人工核对。",
            )
            return "rejected", message_id, route, None
        try:
            regenerate_review_message(message_id, instruction_for(route), "route-refresh")
        except RuntimeError as exc:
            # If Hermes is unavailable or returns a draft that cannot pass the
            # shared validator, keep the update deterministic and review-only.
            snapshot = record["crm_snapshot"]
            lead_id = str((snapshot.get("lead") or {}).get("id"))
            channel = (snapshot.get("output") or {}).get("type") or record["channel"]
            contact_name = (snapshot.get("contact") or {}).get("name") or ""
            evidence = snapshot.get("crm_message_evidence") or {}
            quote = evidence.get("business_detail_quote") or "the request you shared"
            if route == "qualification":
                sender = resolve_sender_identity((snapshot.get("sales") or {}).get("name")) or {}
                sender_name = sender.get("display_name") or "Aceler International"
                greeting = f"Hello {contact_name}," if contact_name else "Hello,"
                body = f"{greeting}\n\nThank you for your message regarding {quote}. To help us understand whether our materials may be relevant, could you share the product or application you are currently considering?\n\nBest regards,\n{sender_name}\nAceler International"
                body_zh = f"{('您好，' + contact_name + '：') if contact_name else '您好：'}\n\n感谢您关于{quote}的来信。为了帮助我们了解产品是否相关，您能否告知目前考虑的产品或应用？\n\n此致敬礼，\n{sender_name}\nAceler International"
                subject, subject_zh = ("A brief question about your requirements", "关于您需求的简短问题") if channel == "email" else (None, None)
            elif route == "recommender_thanks":
                related = ((snapshot.get("referral_relationship") or {}).get("related_contacts") or [])
                related_name = related[0].get("name") if related and isinstance(related[0], dict) else "your colleague"
                body = f"Hi {contact_name},\n\nThank you for recommending {related_name}. We have noted the introduction and appreciate your help.\n\nBest regards,\nAceler International"
                body_zh = f"您好，{contact_name}：\n\n感谢您推荐 {related_name}。我们已记录这次引荐，非常感谢您的帮助。\n\n此致敬礼，\nAceler International"
                subject = subject_zh = None
            elif route == "conversation_follow_up":
                sender = resolve_sender_identity((snapshot.get("sales") or {}).get("name")) or {}
                sender_name = sender.get("display_name") or "Aceler International"
                greeting = f"Hello {contact_name}," if contact_name else "Hello,"
                body = f"{greeting}\n\nJust following up on our previous reply. Please let us know if any clarification would be helpful.\n\nBest regards,\n{sender_name}\nAceler International"
                body_zh = f"{('您好，' + contact_name + '：') if contact_name else '您好：'}\n\n简单跟进一下我们此前的回复。如有任何需要进一步说明之处，请告诉我们。\n\n此致敬礼，\n{sender_name}\nAceler International"
                subject, subject_zh = ("A brief follow-up", "简短跟进") if channel == "email" else (None, None)
            else:
                sender = resolve_sender_identity((snapshot.get("sales") or {}).get("name")) or {}
                sender_name = sender.get("display_name") or "Aceler International"
                greeting = f"Hello {contact_name}," if contact_name else "Hello,"
                body = f"{greeting}\n\nThank you for your message. We have noted your request regarding {quote}. We will review the requested information internally and follow up after confirmation.\n\nBest regards,\n{sender_name}\nAceler International"
                body_zh = f"{('您好，' + contact_name + '：') if contact_name else '您好：'}\n\n感谢您的来信。我们已记录您关于{quote}的请求，将在内部确认相关信息后跟进。\n\n此致敬礼，\n{sender_name}\nAceler International"
                subject, subject_zh = "Regarding your request", "关于您的请求"
            input_warnings = snapshot.get("warnings") if isinstance(snapshot.get("warnings"), list) else []
            warnings = [warning for warning in input_warnings if warning in {"COMPANY_CONTEXT_MISSING", "COMPANY_CONTEXT_THIN", "CRM_EVIDENCE_REQUIRES_MANUAL_CONFIRMATION"}]
            if "Invalid port" in str(exc):
                fallback_reason = "Hermes代理配置解析失败（Invalid port）；已使用仅基于CRM证据的人工复核草稿。"
            elif "Regenerated message failed validation" in str(exc):
                fallback_reason = "Hermes生成结果未通过规则校验；已使用仅基于CRM证据的人工复核草稿。"
            else:
                fallback_reason = "Hermes生成调用失败；已使用仅基于CRM证据的人工复核草稿。"
            replace_message_draft(message_id, {
                "decision": "generated", "lead_id": lead_id, "output_type": channel,
                "language": "English", "content": {"subject": subject, "subject_zh": subject_zh, "body": body, "body_zh": body_zh},
                "message_goal": (
                    "感谢推荐并记录客户请求，待人工确认后跟进。"
                    if route == "recommender_thanks"
                    else "对销售此前回复做一次简短跟进。"
                    if route == "conversation_follow_up"
                    else "确认客户请求并在内部核实后跟进。"
                ),
                "information_requested": [], "warnings": warnings,
                "reason": fallback_reason, "review_required": True,
            })
        return "refreshed", message_id, route, None

    refreshed = rejected = failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, item) for item in targets]
        for future in as_completed(futures):
            try:
                action, message_id, route, _ = future.result()
                if action == "refreshed":
                    refreshed += 1
                else:
                    rejected += 1
                print(f"{message_id} {route} {action}", flush=True)
            except Exception as exc:  # keep the batch moving; no message is approved
                failed += 1
                print(
                    f"FAILED {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
    print(f"refreshed={refreshed} rejected={rejected} failed={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
