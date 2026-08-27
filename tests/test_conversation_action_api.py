import unittest

from review_api.main import app


class ConversationActionApiTest(unittest.TestCase):
    def test_scheduled_preview_routes_are_not_exposed(self):
        paths = app.openapi()["paths"]

        self.assertNotIn("/api/runs", paths)
        self.assertNotIn("/api/runs/{run_id}", paths)
        self.assertNotIn("/api/runs/{run_id}/results", paths)

    def test_action_queue_exposes_only_list_and_terminal_decision(self):
        schema = app.openapi()
        self.assertIn("/api/review/actions", schema["paths"])
        self.assertIn("get", schema["paths"]["/api/review/actions"])
        path = "/api/review/actions/{action_id}/decision"
        self.assertIn(path, schema["paths"])
        self.assertIn("post", schema["paths"][path])
        request = schema["components"]["schemas"]["ActionDecisionRequest"]
        self.assertEqual(
            {"decision", "reviewer", "note"}, set(request["properties"])
        )
        self.assertNotIn("approve", schema["paths"][path])
        self.assertNotIn("send", schema["paths"][path])


if __name__ == "__main__":
    unittest.main()
