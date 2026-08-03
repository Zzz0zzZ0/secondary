import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from .db import connect as connect_outbox
from .message_jobs import DEFAULT_STATE_DIR


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_DIR = PROJECT_DIR / "outputs" / "runtime-reports"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _safe_error(exc: Exception) -> str:
    return str(exc).strip()[-1500:] or exc.__class__.__name__


def report_window(
    report_date: date,
    timezone_name: str,
    end_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(report_date, time.min, zone)
    natural_end = start + timedelta(days=1)
    end = end_at.astimezone(zone) if end_at is not None else natural_end
    if end < start or end > natural_end:
        raise ValueError("Report end must fall within the report date")
    return {
        "date": report_date.isoformat(),
        "timezone": timezone_name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "start_utc": _iso_utc(start),
        "end_utc": _iso_utc(end),
    }


def current_report_date(now: datetime, timezone_name: str) -> date:
    return now.astimezone(ZoneInfo(timezone_name)).date()


def _json_value(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _count_rows(connection: sqlite3.Connection, sql: str, params=()) -> Dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in connection.execute(sql, params).fetchall()
    }


def collect_scheduler(
    db_path: Path,
    start_utc: str,
    end_utc: str,
    snapshot_utc: str,
) -> Dict[str, Any]:
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        run_rows = connection.execute(
            """
            SELECT status, started_at, completed_at, result_json, error
            FROM scheduler_run
            WHERE started_at >= ? AND started_at < ?
            ORDER BY started_at
            """,
            (start_utc, end_utc),
        ).fetchall()
        run_counts = Counter(row["status"] for row in run_rows)
        scan = Counter()
        classification_statuses = Counter()
        dispatch_statuses = Counter()
        run_error_counts = Counter()
        run_error_times = {}
        for row in run_rows:
            result = _json_value(row["result_json"], {})
            scan_result = result.get("full_scan") or {}
            for key, value in scan_result.items():
                if isinstance(value, int):
                    scan[key] += value
            for item in result.get("classifications") or []:
                classification_statuses[str(item.get("status") or "unknown")] += 1
            for item in result.get("dispatched") or []:
                dispatch_statuses[str(item.get("status") or "unknown")] += 1
            if row["status"] == "failed":
                error = row["error"] or "unknown scheduler error"
                run_error_counts[error] += 1
                run_error_times[error] = row["completed_at"] or row["started_at"]
        run_errors = [
            {
                "occurred_at": run_error_times[error],
                "stage": "scheduler_run",
                "count": count,
                "error": error,
            }
            for error, count in run_error_counts.most_common(10)
        ]

        events = _count_rows(
            connection,
            """
            SELECT event_type, count(*)
            FROM secondary_lead_event
            WHERE event_at >= ? AND event_at < ?
            GROUP BY event_type
            ORDER BY event_type
            """,
            (start_utc, end_utc),
        )
        backlog = _count_rows(
            connection,
            """
            SELECT status, count(*)
            FROM secondary_lead_state
            GROUP BY status
            ORDER BY status
            """,
        )
        aged_before = _iso_utc(
            datetime.fromisoformat(snapshot_utc) - timedelta(hours=24)
        )
        aged_waiting_review = connection.execute(
            """
            SELECT count(*)
            FROM secondary_lead_state
            WHERE status = 'waiting_review' AND updated_at < ?
            """,
            (aged_before,),
        ).fetchone()[0]
        lead_errors = [
            {
                "occurred_at": row["updated_at"],
                "stage": "secondary_lead",
                "lead_id": row["lead_id"],
                "count": 1,
                "error": row["last_error"],
            }
            for row in connection.execute(
                """
                SELECT lead_id, updated_at, last_error
                FROM secondary_lead_state
                WHERE last_error IS NOT NULL
                  AND updated_at >= ? AND updated_at < ?
                ORDER BY updated_at DESC
                LIMIT 20
                """,
                (start_utc, end_utc),
            ).fetchall()
        ]
        token_rows = connection.execute(
            """
            SELECT usage_json FROM secondary_lead_classification
            WHERE classified_at >= ? AND classified_at < ?
            UNION ALL
            SELECT usage_json FROM analysis_result
            WHERE generated_at >= ? AND generated_at < ?
            """,
            (start_utc, end_utc, start_utc, end_utc),
        ).fetchall()
        usage = Counter()
        for row in token_rows:
            value = _json_value(row[0], {})
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                usage[key] += int(value.get(key) or 0)
            usage["estimated_cost_usd"] += float(
                value.get("estimated_cost_usd") or 0
            )
        scheduler_state = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT key, value FROM scheduler_state
                WHERE key IN (
                  'runtime_mode', 'remote_last_checked_at',
                  'remote_retry_at', 'remote_last_error'
                )
                """
            ).fetchall()
        }
        latest_completed = connection.execute(
            """
            SELECT completed_at FROM scheduler_run
            WHERE status = 'completed' AND completed_at < ?
            ORDER BY completed_at DESC LIMIT 1
            """,
            (end_utc,),
        ).fetchone()
        return {
            "status": "ok",
            "runs": dict(run_counts),
            "latest_completed_at": latest_completed[0] if latest_completed else None,
            "runtime": scheduler_state,
            "scan": dict(scan),
            "classification_statuses": dict(classification_statuses),
            "dispatch_statuses": dict(dispatch_statuses),
            "events": events,
            "usage": dict(usage),
            "backlog_at_snapshot": backlog,
            "aged_waiting_review_over_24h": int(aged_waiting_review),
            "errors": (run_errors + lead_errors)[:20],
        }
    finally:
        connection.close()


def _pg_counts(cursor, sql: str, params=()) -> Dict[str, int]:
    cursor.execute(sql, params)
    return {str(row[0]): int(row[1]) for row in cursor.fetchall()}


def collect_outbox(
    connect: Callable[[], Any],
    start_utc: str,
    end_utc: str,
    snapshot_utc: str,
) -> Dict[str, Any]:
    with connect() as connection, connection.cursor() as cursor:
        generated_by_channel = _pg_counts(
            cursor,
            """
            SELECT channel, count(*) FROM sales_automation.message_version
            WHERE created_at >= %s AND created_at < %s
            GROUP BY channel ORDER BY channel
            """,
            (start_utc, end_utc),
        )
        decisions = _pg_counts(
            cursor,
            """
            SELECT decision, count(*) FROM sales_automation.message_approval
            WHERE approved_at >= %s AND approved_at < %s
            GROUP BY decision ORDER BY decision
            """,
            (start_utc, end_utc),
        )
        entered_outbox = _pg_counts(
            cursor,
            """
            SELECT channel, count(*) FROM sales_automation.delivery_outbox
            WHERE created_at >= %s AND created_at < %s
            GROUP BY channel ORDER BY channel
            """,
            (start_utc, end_utc),
        )
        sent_by_channel = _pg_counts(
            cursor,
            """
            SELECT channel, count(*) FROM sales_automation.delivery_outbox
            WHERE sent_at >= %s AND sent_at < %s
            GROUP BY channel ORDER BY channel
            """,
            (start_utc, end_utc),
        )
        attempt_statuses = _pg_counts(
            cursor,
            """
            SELECT status, count(*) FROM sales_automation.delivery_attempt
            WHERE completed_at >= %s AND completed_at < %s
            GROUP BY status ORDER BY status
            """,
            (start_utc, end_utc),
        )
        review_backlog = _pg_counts(
            cursor,
            """
            SELECT review_status, count(*)
            FROM sales_automation.message_version
            GROUP BY review_status ORDER BY review_status
            """,
        )
        outbox_backlog = _pg_counts(
            cursor,
            """
            SELECT status, count(*) FROM sales_automation.delivery_outbox
            GROUP BY status ORDER BY status
            """,
        )
        aged_before = datetime.fromisoformat(snapshot_utc) - timedelta(hours=24)
        cursor.execute(
            """
            SELECT count(*) FROM sales_automation.message_version
            WHERE review_status = 'pending_review' AND created_at < %s
            """,
            (aged_before,),
        )
        aged_pending_review = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT
              COALESCE(crm_snapshot->'sales'->>'name', '未分配'),
              channel,
              count(*)
            FROM sales_automation.message_version
            WHERE created_at >= %s AND created_at < %s
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            (start_utc, end_utc),
        )
        sales_breakdown = [
            {"sales": row[0], "channel": row[1], "generated": int(row[2])}
            for row in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT attempt.completed_at, version.lead_id,
                   COALESCE(version.crm_snapshot->'sales'->>'name', '未分配'),
                   outbox.channel, attempt.status,
                   attempt.error_code, attempt.error_message
            FROM sales_automation.delivery_attempt attempt
            JOIN sales_automation.delivery_outbox outbox
              ON outbox.id = attempt.outbox_id
            JOIN sales_automation.message_version version
              ON version.id = outbox.message_version_id
            WHERE attempt.completed_at >= %s AND attempt.completed_at < %s
              AND attempt.status IN ('failed', 'unknown')
            ORDER BY attempt.completed_at DESC LIMIT 20
            """,
            (start_utc, end_utc),
        )
        errors = [
            {
                "occurred_at": row[0].isoformat() if row[0] else None,
                "stage": "delivery",
                "lead_id": row[1],
                "sales": row[2],
                "channel": row[3],
                "status": row[4],
                "count": 1,
                "error_code": row[5],
                "error": row[6],
            }
            for row in cursor.fetchall()
        ]
    return {
        "status": "ok",
        "generated_by_channel": generated_by_channel,
        "review_decisions": decisions,
        "entered_outbox_by_channel": entered_outbox,
        "sent_by_channel": sent_by_channel,
        "attempt_statuses": attempt_statuses,
        "review_backlog_at_snapshot": review_backlog,
        "outbox_backlog_at_snapshot": outbox_backlog,
        "aged_pending_review_over_24h": aged_pending_review,
        "sales_breakdown": sales_breakdown,
        "errors": errors,
    }


def _total(values: Dict[str, int]) -> int:
    return sum(int(value) for value in values.values())


def _overall_status(scheduler: Dict[str, Any], outbox: Dict[str, Any]) -> str:
    if scheduler.get("status") != "ok" or outbox.get("status") != "ok":
        return "异常"
    if (
        scheduler.get("runs", {}).get("failed", 0)
        or scheduler.get("runtime", {}).get("runtime_mode") == "local_only"
        or outbox.get("attempt_statuses", {}).get("failed", 0)
        or outbox.get("attempt_statuses", {}).get("unknown", 0)
        or outbox.get("outbox_backlog_at_snapshot", {}).get("failed", 0)
        or outbox.get("outbox_backlog_at_snapshot", {}).get("unknown", 0)
    ):
        return "异常"
    if (
        scheduler.get("aged_waiting_review_over_24h", 0)
        or outbox.get("aged_pending_review_over_24h", 0)
        or outbox.get("outbox_backlog_at_snapshot", {}).get("retry_wait", 0)
    ):
        return "有风险"
    return "正常"


def build_report(
    report_date: date,
    timezone_name: str = DEFAULT_TIMEZONE,
    state_dir: Path = DEFAULT_STATE_DIR,
    now: Optional[datetime] = None,
    outbox_connect: Callable[[], Any] = connect_outbox,
    window_end: Optional[datetime] = None,
) -> Dict[str, Any]:
    generated_at = now or datetime.now(timezone.utc)
    window = report_window(report_date, timezone_name, window_end)
    snapshot_utc = _iso_utc(generated_at)
    try:
        scheduler = collect_scheduler(
            state_dir / "poller.sqlite3",
            window["start_utc"],
            window["end_utc"],
            snapshot_utc,
        )
    except Exception as exc:
        scheduler = {"status": "error", "error": _safe_error(exc)}
    try:
        outbox = collect_outbox(
            outbox_connect,
            window["start_utc"],
            window["end_utc"],
            snapshot_utc,
        )
    except Exception as exc:
        outbox = {"status": "error", "error": _safe_error(exc)}
    return {
        "schema_version": "1.0",
        "report_id": f"{window['date']}-{timezone_name.replace('/', '-')}",
        "generated_at": generated_at.astimezone(
            ZoneInfo(timezone_name)
        ).isoformat(),
        "window": {key: window[key] for key in ("date", "timezone", "start", "end")},
        "complete": scheduler.get("status") == "ok" and outbox.get("status") == "ok",
        "overall_status": _overall_status(scheduler, outbox),
        "scheduler": scheduler,
        "outbox": outbox,
        "snapshot_note": "积压指标统计于报告生成时，不回溯重建历史时点。",
    }


def _metric(value: Any) -> str:
    return str(value if value is not None else "—")


def render_markdown(report: Dict[str, Any]) -> str:
    scheduler = report["scheduler"]
    outbox = report["outbox"]
    window = report["window"]
    lines = [
        f"# Twenty Hermes 当日运行报告 · {window['date']}",
        "",
        f"- 结论：**{report['overall_status']}**",
        f"- 数据完整：{'是' if report['complete'] else '否'}",
        f"- 统计窗口：{window['start']} ～ {window['end']}",
        f"- 生成时间：{report['generated_at']}",
        f"- 说明：{report['snapshot_note']}",
        "",
        "## 运行状态",
        "",
    ]
    if scheduler.get("status") != "ok":
        lines.append(f"调度数据读取失败：{scheduler.get('error', '未知错误')}")
    else:
        runs = scheduler.get("runs", {})
        usage = scheduler.get("usage", {})
        lines.extend(
            [
                "| 指标 | 数量 |",
                "| --- | ---: |",
                f"| 调度运行 | {_total(runs)} |",
                f"| 运行成功 | {runs.get('completed', 0)} |",
                f"| 运行失败 | {runs.get('failed', 0)} |",
                f"| 扫描发现 | {scheduler.get('scan', {}).get('inserted', 0)} |",
                f"| 完成分类 | {_total(scheduler.get('classification_statuses', {}))} |",
                f"| 消息调度 | {_total(scheduler.get('dispatch_statuses', {}))} |",
                f"| Token | {usage.get('total_tokens', 0)} |",
                f"| 预估成本 USD | {float(usage.get('estimated_cost_usd', 0)):.6f} |",
            ]
        )
    lines.extend(["", "## 审核与投递", ""])
    if outbox.get("status") != "ok":
        lines.append(f"Outbox 数据读取失败：{outbox.get('error', '未知错误')}")
    else:
        decisions = outbox.get("review_decisions", {})
        attempts = outbox.get("attempt_statuses", {})
        lines.extend(
            [
                "| 指标 | 数量 |",
                "| --- | ---: |",
                f"| 新生成待审消息 | {_total(outbox.get('generated_by_channel', {}))} |",
                f"| 审核批准 | {decisions.get('approved', 0)} |",
                f"| 审核拒绝 | {decisions.get('rejected', 0)} |",
                f"| 进入 Outbox | {_total(outbox.get('entered_outbox_by_channel', {}))} |",
                f"| 发送成功 | {_total(outbox.get('sent_by_channel', {}))} |",
                f"| 投递重试 | {attempts.get('retry_wait', 0)} |",
                f"| 投递失败 | {attempts.get('failed', 0)} |",
                f"| 投递状态未知 | {attempts.get('unknown', 0)} |",
            ]
        )
    lines.extend(["", "## 当前积压", ""])
    if scheduler.get("status") == "ok" and outbox.get("status") == "ok":
        lines.extend(
            [
                "| 指标 | 数量 |",
                "| --- | ---: |",
                f"| 待人工审核 | {outbox.get('review_backlog_at_snapshot', {}).get('pending_review', 0)} |",
                f"| 待审核超过 24h | {outbox.get('aged_pending_review_over_24h', 0)} |",
                f"| Outbox 排队 | {outbox.get('outbox_backlog_at_snapshot', {}).get('queued', 0)} |",
                f"| Outbox 重试等待 | {outbox.get('outbox_backlog_at_snapshot', {}).get('retry_wait', 0)} |",
                f"| Outbox 失败 | {outbox.get('outbox_backlog_at_snapshot', {}).get('failed', 0)} |",
                f"| Outbox 状态未知 | {outbox.get('outbox_backlog_at_snapshot', {}).get('unknown', 0)} |",
                f"| 调度侧失败线索 | {scheduler.get('backlog_at_snapshot', {}).get('failed', 0)} |",
            ]
        )
    else:
        lines.append("数据源不完整，当前积压不可判定。")
    errors = (scheduler.get("errors") or []) + (outbox.get("errors") or [])
    lines.extend(["", "## 异常明细", ""])
    if not errors:
        lines.append("本报告窗口内没有异常明细。")
    else:
        lines.extend(["| 最近时间 | 阶段 | 次数 | 线索 | 销售人员 | 错误 |", "| --- | --- | ---: | --- | --- | --- |"])
        for item in errors[:20]:
            error = str(item.get("error") or item.get("error_code") or "未知错误")
            error = error.replace("|", "\\|").replace("\n", " ")[:300]
            lines.append(
                "| {time} | {stage} | {count} | {lead} | {sales} | {error} |".format(
                    time=_metric(item.get("occurred_at")),
                    stage=_metric(item.get("stage")),
                    count=_metric(item.get("count") or 1),
                    lead=_metric(item.get("lead_id")),
                    sales=_metric(item.get("sales")),
                    error=error,
                )
            )
    sales = outbox.get("sales_breakdown") or []
    lines.extend(["", "## 销售人员与渠道", ""])
    if not sales:
        lines.append("本报告窗口内没有新生成消息，或 Outbox 数据不可用。")
    else:
        lines.extend(["| 销售人员 | 渠道 | 新生成消息 |", "| --- | --- | ---: |"])
        for item in sales:
            lines.append(
                f"| {item['sales']} | {item['channel']} | {item['generated']} |"
            )
    return "\n".join(lines) + "\n"


def write_report(
    report: Dict[str, Any],
    report_dir: Path = DEFAULT_REPORT_DIR,
) -> Dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    report_dir.chmod(0o700)
    stem = report["window"]["date"]
    paths = {
        "json": report_dir / f"{stem}.json",
        "markdown": report_dir / f"{stem}.md",
    }
    contents = {
        "json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        "markdown": render_markdown(report),
    }
    for kind, path in paths.items():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(contents[kind], encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    return paths


def list_reports(report_dir: Path = DEFAULT_REPORT_DIR):
    records = []
    for path in sorted(report_dir.glob("*.json"), reverse=True) if report_dir.exists() else []:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records.append(
            {
                "date": report.get("window", {}).get("date") or path.stem,
                "generated_at": report.get("generated_at"),
                "overall_status": report.get("overall_status"),
                "complete": report.get("complete", False),
            }
        )
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Twenty Hermes daily report")
    parser.add_argument("--date", help="Report date in YYYY-MM-DD; defaults to today")
    parser.add_argument(
        "--timezone",
        default=os.getenv("TWENTY_BUSINESS_TIMEZONE", DEFAULT_TIMEZONE),
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.getenv("HERMES_POLL_STATE_DIR", str(DEFAULT_STATE_DIR))),
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path(os.getenv("HERMES_RUNTIME_REPORT_DIR", str(DEFAULT_REPORT_DIR))),
    )
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    today = current_report_date(now, args.timezone)
    target_date = (
        date.fromisoformat(args.date)
        if args.date
        else today
    )
    if target_date > today:
        parser.error("Report date cannot be in the future")
    report = build_report(
        target_date,
        args.timezone,
        args.state_dir,
        now,
        window_end=now if target_date == today else None,
    )
    paths = write_report(report, args.report_dir)
    print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False))
    return 0 if report["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
