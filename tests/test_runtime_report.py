import json
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app.runtime_report import (
    _overall_status,
    build_report,
    current_report_date,
    report_window,
    write_report,
)
from app.secondary_scheduler import SecondaryLeadScheduler


class RuntimeReportTests(unittest.TestCase):
    def test_window_uses_shanghai_natural_day(self):
        window = report_window(date(2026, 8, 2), "Asia/Shanghai")

        self.assertEqual(window["start"], "2026-08-02T00:00:00+08:00")
        self.assertEqual(window["end"], "2026-08-03T00:00:00+08:00")
        self.assertEqual(window["start_utc"], "2026-08-01T16:00:00+00:00")
        self.assertEqual(
            current_report_date(
                datetime(2026, 8, 3, 0, 10, tzinfo=timezone.utc),
                "Asia/Shanghai",
            ),
            date(2026, 8, 3),
        )

    def test_current_day_window_ends_at_trigger_time(self):
        window = report_window(
            date(2026, 8, 3),
            "Asia/Shanghai",
            datetime(2026, 8, 3, 3, 25, tzinfo=timezone.utc),
        )

        self.assertEqual(window["start"], "2026-08-03T00:00:00+08:00")
        self.assertEqual(window["end"], "2026-08-03T11:25:00+08:00")
        self.assertEqual(window["end_utc"], "2026-08-03T03:25:00+00:00")

    def test_missing_outbox_is_marked_incomplete_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            SecondaryLeadScheduler(state_dir=state_dir)
            with sqlite3.connect(state_dir / "poller.sqlite3") as connection:
                connection.execute(
                    """
                    INSERT INTO scheduler_run
                      (id, trigger, status, started_at, completed_at, result_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "run-1",
                        "run",
                        "completed",
                        "2026-08-01T17:00:00+00:00",
                        "2026-08-01T17:05:00+00:00",
                        json.dumps(
                            {
                                "mode": "online",
                                "full_scan": {"seen": 12, "inserted": 2},
                                "classifications": [{"status": "scheduled"}],
                                "dispatched": [{"status": "waiting_review"}],
                            }
                        ),
                    ),
                )

            def unavailable():
                raise RuntimeError("outbox unavailable")

            report = build_report(
                date(2026, 8, 2),
                state_dir=state_dir,
                now=datetime(2026, 8, 3, 0, 10, tzinfo=timezone.utc),
                outbox_connect=unavailable,
            )

            self.assertFalse(report["complete"])
            self.assertEqual(report["overall_status"], "异常")
            self.assertEqual(report["scheduler"]["runs"], {"completed": 1})
            self.assertEqual(report["scheduler"]["scan"]["seen"], 12)
            self.assertEqual(report["outbox"]["status"], "error")
            self.assertNotIn("generated_by_channel", report["outbox"])

            paths = write_report(report, Path(temporary) / "reports")
            self.assertTrue(paths["json"].is_file())
            self.assertTrue(paths["markdown"].is_file())
            self.assertIn("数据完整：否", paths["markdown"].read_text())
            self.assertEqual(paths["json"].stat().st_mode & 0o777, 0o600)

    def test_recovered_runs_do_not_make_system_overall_abnormal(self):
        scheduler = {
            "status": "ok",
            "runs": {"completed": 90, "failed": 2},
            "runtime": {"runtime_mode": "online"},
            "backlog_at_snapshot": {"failed": 0},
            "errors": [
                {
                    "stage": "scheduler_run",
                    "error": "Recovered after scheduler restart",
                },
                {
                    "stage": "secondary_lead",
                    "error": "CRM contains DO_NOT_CONTACT",
                },
            ],
        }
        outbox = {
            "status": "ok",
            "attempt_statuses": {},
            "outbox_backlog_at_snapshot": {},
        }

        self.assertEqual(_overall_status(scheduler, outbox), "正常")


if __name__ == "__main__":
    unittest.main()
