import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.secondary_scheduler import SecondaryLeadScheduler


class MessageDispatchConcurrencyTest(unittest.TestCase):
    def test_workers_claim_distinct_due_leads_and_isolate_failures(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            scheduler = SecondaryLeadScheduler(
                state_dir=Path(directory), environment={"HERMES_MESSAGE_WORKERS": "3"},
                now=lambda: now,
            )
            for index in range(8):
                scheduler._upsert_discovered({
                    "lead": {"id": f"lead-{index}"},
                    "source_version": {"record_id": f"lead-{index}", "updated_at": now.isoformat()},
                })
            with scheduler._connect() as connection:
                connection.execute("UPDATE secondary_lead_state SET status='scheduled',next_action_at=?", (now.isoformat(),))
                connection.execute("UPDATE secondary_lead_state SET next_action_at=? WHERE lead_id='lead-7'", ((now + timedelta(days=1)).isoformat(),))
            barrier = threading.Barrier(3)
            lock = threading.Lock()
            seen = []
            active = 0
            peak = 0

            def dispatch(row):
                nonlocal active, peak
                with lock:
                    seen.append(row["lead_id"])
                    position = len(seen)
                    active += 1
                    peak = max(peak, active)
                try:
                    if position <= 3:
                        barrier.wait(timeout=1)
                    if row["lead_id"] == "lead-4":
                        raise RuntimeError("Temporary generation error")
                    return {"lead_id": row["lead_id"], "status": "waiting_review"}
                finally:
                    with lock:
                        active -= 1

            scheduler._dispatch_one = dispatch
            results = scheduler.dispatch_due(limit=20)

            self.assertEqual(3, peak)
            self.assertCountEqual([f"lead-{i}" for i in range(7)], seen)
            self.assertEqual(7, len(results))
            self.assertEqual(1, sum(r["status"] == "retry_wait" for r in results))
            with scheduler._connect() as connection:
                failed = connection.execute("SELECT status,next_action_at,generation_retry_at FROM secondary_lead_state WHERE lead_id='lead-4'").fetchone()
            self.assertEqual("scheduled", failed["status"])
            self.assertEqual(failed["next_action_at"], now.isoformat())
            self.assertGreater(failed["generation_retry_at"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
