#!/usr/bin/env python3
"""Read-only tertiary-lead trial.

This script is intentionally outside the Hermes scheduler. It reads Twenty and
the Outbox database, classifies NEW/NO_REPLY records into test queues, and
writes a JSON report. It never writes CRM, Outbox, or the production SQLite
state database.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TRIAL_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TRIAL_DIR.parent
DEFAULT_ENV_FILE = PROJECT_DIR / "config" / "local.env"
DEFAULT_OUTPUT = TRIAL_DIR / "outputs" / "latest.json"


def load_env_file(path: Path) -> None:
    """Load simple KEY=value entries without evaluating shell commands."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            value = shlex.split(raw_value, comments=True)[0] if raw_value.strip() else ""
        except ValueError as exc:
            raise ValueError(f"Invalid env value for {key}") from exc
        os.environ.setdefault(key, value)


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _channel(row: dict[str, Any]) -> str | None:
    email = row.get("email")
    linkedin = row.get("linkedin_url")
    if email:
        return "email"
    if linkedin:
        return "linkedin"
    return None


def _age_bucket(last_follow_up: date | None, today: date) -> str | None:
    if last_follow_up is None:
        return None
    age = max((today - last_follow_up).days, 0)
    if age <= 2:
        return "0-2d"
    if age <= 5:
        return "3-5d"
    if age <= 10:
        return "6-10d"
    if age <= 30:
        return "11-30d"
    return "31d+"


def classify_record(
    row: dict[str, Any],
    *,
    sent_lead_ids: set[str] | None,
    outbox_available: bool,
    today: date,
) -> dict[str, Any]:
    """Classify one CRM record without generating or scheduling a message."""
    lead_id = str(row["lead_id"])
    lifecycle = str(row.get("lifecycle") or "")
    last_response = _parse_date(row.get("last_response"))
    last_follow_up = _parse_date(row.get("last_follow_up"))
    sent = lead_id in (sent_lead_ids or set())
    has_channel = _channel(row) is not None
    age_bucket = _age_bucket(last_follow_up, today)

    if last_response is not None:
        queue = "response_conflict"
        action = "人工确认回复后转入有效商机"
    elif sent:
        queue = "contacted_without_crm_followup"
        action = "核对发送事实与 CRM 跟进日期，禁止自动发送"
    elif lifecycle == "NEW":
        if not outbox_available:
            queue = "contact_fact_unknown"
            action = "Outbox 不可核对，先确认是否已经联系"
        else:
            queue = "uncontacted" if has_channel else "missing_channel"
            action = "人工复核后生成首信" if has_channel else "补充邮箱或 LinkedIn"
    elif lifecycle == "NO_REPLY" and last_follow_up is not None:
        if not has_channel:
            queue = "missing_channel"
            action = "补充邮箱或 LinkedIn"
        elif age_bucket in {"0-2d", "3-5d"}:
            queue = "no_response_waiting"
            action = "等待跟进窗口，不生成消息"
        elif age_bucket == "31d+":
            queue = "reactivation_or_close"
            action = "人工决定重新激活或关闭"
        else:
            queue = "no_response_due"
            action = "人工复核后生成下一次跟进"
    else:
        queue = "contact_fact_unknown"
        action = "没有 CRM 跟进日期，先核对联系事实"

    return {
        "lead_id": lead_id,
        "lifecycle": lifecycle,
        "follow_up_tier": row.get("follow_up_tier"),
        "name": row.get("name"),
        "company": row.get("company"),
        "email": row.get("email"),
        "linkedin_url": row.get("linkedin_url"),
        "last_follow_up": last_follow_up.isoformat() if last_follow_up else None,
        "last_response": last_response.isoformat() if last_response else None,
        "last_sent_at": row.get("last_sent_at"),
        "age_bucket": age_bucket,
        "has_channel": has_channel,
        "queue": queue,
        "action": action,
    }


def classify_records(
    rows: Iterable[dict[str, Any]],
    *,
    sent_lead_ids: set[str] | None,
    outbox_available: bool,
    today: date,
    sample_limit: int,
) -> tuple[Counter[str], dict[str, list[dict[str, Any]]]]:
    counts: Counter[str] = Counter()
    samples: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = classify_record(
            row,
            sent_lead_ids=sent_lead_ids,
            outbox_available=outbox_available,
            today=today,
        )
        counts[item["queue"]] += 1
        if len(samples[item["queue"]]) < sample_limit:
            samples[item["queue"]].append(item)
    return counts, dict(samples)


def _crm_connection():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Missing psycopg. Run: ./scripts/setup_python.sh") from exc
    required = ["TWENTY_DB_HOST", "TWENTY_DB_NAME", "TWENTY_DB_USER"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing CRM configuration: " + ", ".join(missing))
    return psycopg.connect(
        host=os.environ["TWENTY_DB_HOST"],
        port=int(os.getenv("TWENTY_DB_PORT", "5432")),
        dbname=os.environ["TWENTY_DB_NAME"],
        user=os.environ["TWENTY_DB_USER"],
        password=os.getenv("TWENTY_DB_PASSWORD"),
        sslmode=os.getenv("TWENTY_DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("TWENTY_DB_CONNECT_TIMEOUT", "5")),
    )


def read_crm_rows() -> list[dict[str, Any]]:
    schema = os.getenv("TWENTY_WORKSPACE_SCHEMA", "")
    suffix = schema.removeprefix("workspace_")
    if not suffix or not suffix.isalnum() or schema != f"workspace_{suffix}":
        raise RuntimeError("TWENTY_WORKSPACE_SCHEMA is missing or invalid")
    query = f'''
        SELECT
          person.id::text AS lead_id,
          trim(concat_ws(' ', person."nameFirstName", person."nameLastName")) AS name,
          person."lifeCycle"::text AS lifecycle,
          person."followUpTier"::text AS follow_up_tier,
          person."lastFollowUp" AS last_follow_up,
          person."lastResponse" AS last_response,
          COALESCE(NULLIF(person."emailsPrimaryEmail", ''), NULLIF(person."emailsAdditionalEmails"->>0, '')) AS email,
          COALESCE(NULLIF(person."linkedinLinkPrimaryLinkUrl", ''), NULLIF(person."linkedinLinkSecondaryLinks"->0->>'url', '')) AS linkedin_url,
          company.name AS company,
          person."companyId"::text AS company_id
        FROM "{schema}".person person
        LEFT JOIN "{schema}".company company
          ON company.id = person."companyId"
         AND company."deletedAt" IS NULL
        WHERE person."deletedAt" IS NULL
          AND person."lifeCycle"::text IN ('NEW', 'NO_REPLY')
        ORDER BY person.id::text
    '''
    with _crm_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("BEGIN READ ONLY")
            cursor.execute("SET LOCAL statement_timeout = '30s'")
            cursor.execute(query)
            columns = [item.name for item in cursor.description]
            rows = [dict(zip(columns, values)) for values in cursor.fetchall()]
            cursor.execute("COMMIT")
    for row in rows:
        row.pop("company_id", None)
    return rows


def _outbox_connection():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Missing psycopg. Run: ./scripts/setup_python.sh") from exc
    required = ["OUTBOX_DB_HOST", "OUTBOX_DB_NAME", "OUTBOX_DB_USER"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing Outbox configuration: " + ", ".join(missing))
    return psycopg.connect(
        host=os.environ["OUTBOX_DB_HOST"],
        port=int(os.getenv("OUTBOX_DB_PORT", "5432")),
        dbname=os.environ["OUTBOX_DB_NAME"],
        user=os.environ["OUTBOX_DB_USER"],
        password=os.getenv("OUTBOX_DB_PASSWORD"),
        sslmode=os.getenv("OUTBOX_DB_SSLMODE", "prefer"),
        connect_timeout=int(os.getenv("OUTBOX_DB_CONNECT_TIMEOUT", "5")),
    )


def read_sent_leads() -> tuple[set[str], dict[str, str], str | None]:
    query = """
        SELECT mv.lead_id, max(d.sent_at) AS last_sent_at
        FROM sales_automation.delivery_outbox d
        JOIN sales_automation.message_version mv ON mv.id = d.message_version_id
        WHERE d.status = 'sent'
        GROUP BY mv.lead_id
    """
    try:
        with _outbox_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("BEGIN READ ONLY")
                cursor.execute("SET LOCAL statement_timeout = '10s'")
                cursor.execute(query)
                rows = cursor.fetchall()
                cursor.execute("COMMIT")
        sent = {str(row[0]) for row in rows}
        dates = {str(row[0]): row[1].isoformat() if row[1] else None for row in rows}
        return sent, dates, None
    except Exception as exc:  # read-only trial should remain usable for CRM audit
        return set(), {}, f"{type(exc).__name__}: {exc}"


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def build_report(rows: list[dict[str, Any]], *, sample_limit: int, today: date) -> dict[str, Any]:
    sent_ids, sent_dates, outbox_error = read_sent_leads()
    for row in rows:
        if row["lead_id"] in sent_dates:
            row["last_sent_at"] = sent_dates[row["lead_id"]]
    counts, samples = classify_records(
        rows,
        sent_lead_ids=sent_ids,
        outbox_available=outbox_error is None,
        today=today,
        sample_limit=sample_limit,
    )
    return {
        "schema_version": "tertiary-trial.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "main_flow_integrated": False,
        "sources": {
            "crm": "Twenty PostgreSQL person table",
            "outbox": "sales_automation.delivery_outbox status=sent",
            "outbox_error": outbox_error,
        },
        "summary": {
            "crm_records": len(rows),
            "sent_lead_ids": len(sent_ids),
            "by_queue": dict(sorted(counts.items())),
        },
        "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only tertiary lead trial")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args(argv)
    if args.sample_limit < 0:
        parser.error("--sample-limit must be >= 0")
    load_env_file(args.env_file)
    rows = read_crm_rows()
    report = build_report(rows, sample_limit=args.sample_limit, today=date.today())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
