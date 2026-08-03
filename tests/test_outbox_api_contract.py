import unittest

from outbox_api.main import app


class OutboxApiContractTest(unittest.TestCase):
    def test_existing_lease_request_fields_remain_compatible(self):
        schema = app.openapi()
        complete = schema["components"]["schemas"]["CompleteRequest"]
        properties = complete["properties"]
        required = set(complete["required"])

        self.assertIn("/v1/deliveries/{delivery_id}/heartbeat", schema["paths"])
        self.assertIn("/v1/deliveries/{delivery_id}/complete", schema["paths"])
        self.assertIn("/v1/deliveries/{delivery_id}/fail", schema["paths"])
        self.assertIn("worker_id", properties)
        self.assertIn("lease_token", properties)
        self.assertIn("provider_message_id", properties)
        self.assertIn("provider_thread_id", properties)
        self.assertIn("worker_id", required)
        self.assertIn("lease_token", required)


if __name__ == "__main__":
    unittest.main()
