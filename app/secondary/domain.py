import hashlib


LEAD_TYPES = {
    "no_current_demand",
    "unknown_demand",
    "referred",
    "below_moq",
}
LEAD_TYPE_LABELS = {
    "no_current_demand": "暂无需求",
    "unknown_demand": "未知需求",
    "referred": "（被）推荐",
    "below_moq": "订量不够",
}
MESSAGE_SUBTYPES = {
    "unknown_demand": "UNKNOWN_DEMAND",
    "referred": "RECOMMENDED",
    "below_moq": "INSUFFICIENT_ORDER",
}
INTERVAL_RANGES_DAYS = {
    "no_current_demand": (30, 40),
    "unknown_demand": (3, 7),
    "referred": (1, 3),
    "below_moq": (60, 90),
}
QUEUE_STATUSES = {
    "settling",
    "classifying",
    "scheduled",
    "generating",
    "waiting_review",
    "waiting_delivery",
    "needs_review",
    "needs_contact",
    "paused",
    "converted",
    "failed",
}


def interval_days(lead_type: str, lead_id: str, sequence: int) -> int:
    if sequence >= 3:
        low, high = 90, 120
    elif sequence == 2:
        low, high = 60, 90
    elif sequence == 1 and lead_type in {"unknown_demand", "referred"}:
        low, high = 30, 45
    else:
        low, high = INTERVAL_RANGES_DAYS[lead_type]
    seed = hashlib.sha256(
        f"{lead_id}:{lead_type}:{sequence}".encode("utf-8")
    ).hexdigest()
    return low + (int(seed[:8], 16) % (high - low + 1))
