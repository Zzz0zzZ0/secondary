import unittest

from outbox_api.main import CompleteRequest, FailRequest, app


class OutboxApiContractTest(unittest.TestCase):
    def test_complete_and_fail_only_require_worker_id(self):
        schema = app.openapi()
        complete = schema["components"]["schemas"]["CompleteRequest"]
        failed = schema["components"]["schemas"]["FailRequest"]

        self.assertIn("/v1/deliveries/{delivery_id}/heartbeat", schema["paths"])
        self.assertIn("/v1/deliveries/{delivery_id}/complete", schema["paths"])
        self.assertIn("/v1/deliveries/{delivery_id}/fail", schema["paths"])
        self.assertEqual({"worker_id"}, set(complete["properties"]))
        self.assertEqual({"worker_id"}, set(complete["required"]))
        self.assertEqual({"worker_id"}, set(failed["properties"]))
        self.assertEqual({"worker_id"}, set(failed["required"]))

        self.assertEqual("worker-1", CompleteRequest(worker_id="worker-1").worker_id)
        self.assertEqual("worker-1", FailRequest(worker_id="worker-1").worker_id)


if __name__ == "__main__":
    unittest.main()
