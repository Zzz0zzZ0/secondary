"""Resolve CRM foreign-key relationships without asking the model to guess direction."""
import copy


def crm_referral_context(record):
    links = record.get("crm_referral") or {}
    current_id = str((record.get("contact") or {}).get("id") or (record.get("lead") or {}).get("id") or record.get("lead_id") or "")
    groups = {}
    for key in ("recommended_by", "referred_contacts"):
        groups[key] = [copy.deepcopy(person) for person in links.get(key, [])
                       if person.get("id") and str(person["id"]) != current_id
                       and str(person.get("name") or "").strip()]
    incoming, outgoing = groups["recommended_by"], groups["referred_contacts"]
    if not incoming and not outgoing:
        return None
    return {
        "current_contact_role": "both" if incoming and outgoing else "referred" if incoming else "recommender",
        "related_contacts": incoming + outgoing,
        **groups,
        "source": "crm.person.recommendedById",
    }


def referral_context(record, classification=None):
    return (crm_referral_context(record) or record.get('referral_context')
            or (classification or {}).get('referral_relationship'))


def is_recommender(record, classification=None):
    context = referral_context(record, classification) or {}
    return (context.get('current_contact_role') == 'recommender'
            or record.get('message_route') in {'recommender_thanks', 'referral_handoff'}
            or record.get('conversation_action') == 'referral')


def read_referral_history(lead_id):
    """Read the recipient's CRM Notes; read failures must never mean no history."""
    from notes_trial import notes_trial as notes
    rows = notes.read_records(1, str(lead_id))
    record = rows[0] if rows else None
    return record, referral_history_check(record)


def referral_history_check(record):
    import hashlib
    import json
    from datetime import datetime, timezone
    from notes_trial import notes_trial as notes
    history = notes.recent_email_history(record) if record else []
    outbound = [item for item in history if item.get('direction') == 'FA' and not item.get('transport_event')]
    return {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'source_hash': hashlib.sha256(json.dumps(history, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
        'has_outbound': bool(outbound),
        'outbound_count': len(outbound),
    }
