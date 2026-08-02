import warnings
from datetime import datetime
from typing import Optional


def notify_secondary_outbox_event(
    message_version_id,
    event: str,
    event_at: Optional[datetime] = None,
) -> bool:
    try:
        from .secondary_scheduler import record_outbox_signal

        return record_outbox_signal(
            str(message_version_id),
            event,
            event_at,
        )
    except Exception as exc:
        warnings.warn(
            f"Could not record secondary-lead Outbox signal: {exc}",
            RuntimeWarning,
        )
        return False

