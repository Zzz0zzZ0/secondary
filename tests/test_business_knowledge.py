import json
import unittest

from app.business_knowledge import load_snapshot


class BusinessKnowledgeTest(unittest.TestCase):
    def test_notes_snapshot_is_compact_versioned_and_routed(self):
        snapshot = load_snapshot(("notes_semantic", "notes_draft"))

        self.assertEqual("business-knowledge-v1", snapshot["version"])
        self.assertEqual(
            ("notes_semantic", "notes_draft"), snapshot["scopes"]
        )
        self.assertEqual(
            ("company.positioning", "company.production_role"),
            snapshot["routing"]["direct_answer"],
        )
        self.assertEqual(
            ("product.non_catalog",),
            snapshot["routing"]["internal_task"],
        )
        self.assertEqual(64, len(snapshot["sha256"]))
        payload = json.loads(snapshot["prompt_text"])
        self.assertEqual(3, len(payload["approved_business_knowledge"]))


if __name__ == "__main__":
    unittest.main()
