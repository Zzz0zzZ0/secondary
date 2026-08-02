"""Minimal Outbox email consumer example.

Run from the project root:
    .venv/bin/python -m clients.python.email_consumer_example --once

This example uses the project's Gmail OAuth sender. It will not claim a task
unless OUTBOX_CONSUMER_ENABLED=true.
"""

import argparse
import os
import time

from requests import HTTPError
from requests.exceptions import ConnectionError, Timeout

from app import gmail_sender
from app.outbox import valid_email
from .outbox_client import OutboxClient


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def validate_delivery(item):
    if item.get("channel") != "email":
        raise ValueError("Claimed item is not an email")
    if item.get("provider") != "email":
        raise ValueError("Claimed item is not assigned to email delivery")
    payload = item.get("payload") or {}
    recipient = item.get("recipient")
    if not valid_email(recipient):
        raise ValueError("recipient is not a valid email address")
    if payload.get("to") != recipient:
        raise ValueError("payload.to does not match recipient")
    if not payload.get("subject") or not payload.get("body"):
        raise ValueError("Email subject or body is empty")
    if not item.get("idempotency_key"):
        raise ValueError("idempotency_key is missing")
    return payload


def report_failure(client, item, worker_id, result, code, message):
    client.fail(
        item,
        worker_id,
        result=result,
        error_code=code,
        error_message=str(message)[:2000],
    )


def consume_once(client, worker_id):
    items = client.claim(
        worker_id,
        channels=["email"],
        providers=["email"],
        max_items=1,
        wait_seconds=20,
    )
    if not items:
        print("No email task is currently available.")
        return False

    item = items[0]
    try:
        payload = validate_delivery(item)
        provider_result = gmail_sender.send(payload)
        client.complete(
            item,
            worker_id,
            provider_message_id=provider_result.get("id"),
            provider_thread_id=provider_result.get("threadId"),
        )
        print(f"Completed delivery {item['delivery_id']}.")
    except ValueError as exc:
        report_failure(client, item, worker_id, "permanent", "INVALID_TASK", exc)
    except HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        result = "retryable" if status == 429 or status >= 500 else "permanent"
        report_failure(client, item, worker_id, result, f"GMAIL_HTTP_{status}", exc)
    except (Timeout, ConnectionError) as exc:
        # The request may have reached Gmail. Do not retry automatically.
        report_failure(client, item, worker_id, "unknown", "GMAIL_RESULT_UNKNOWN", exc)
    except Exception as exc:
        # Configuration/OAuth failures happen before Gmail confirms a send.
        report_failure(client, item, worker_id, "retryable", "WORKER_ERROR", exc)
    return True


def main():
    parser = argparse.ArgumentParser(description="Example Gmail Outbox consumer")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim at most one task, then exit",
    )
    args = parser.parse_args()

    if os.getenv("OUTBOX_CONSUMER_ENABLED", "false").lower() != "true":
        raise RuntimeError(
            "Consumer is disabled. Set OUTBOX_CONSUMER_ENABLED=true only "
            "after checking the Gmail send mode."
        )

    base_url = required("OUTBOX_API_URL")
    token = required("OUTBOX_CONSUMER_TOKEN")
    worker_id = required("OUTBOX_WORKER_ID")

    # Check Gmail OAuth and redirect/live safety settings before claiming.
    gmail_sender.preflight()
    client = OutboxClient(base_url, token)

    while True:
        consume_once(client, worker_id)
        if args.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
