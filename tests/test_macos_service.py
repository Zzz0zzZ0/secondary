import unittest
from pathlib import Path

from app.macos_service import background_app_info
from app.macos_service import service_payloads


class MacOSServiceTest(unittest.TestCase):
    def test_service_payloads_keep_poller_awake_and_review_headless(self):
        launcher = Path("/Applications/Twenty Hermes Background.app/launcher")
        payloads = service_payloads(Path("/project"), Path("/logs"), launcher)

        self.assertEqual(
            [
                str(launcher),
                "/usr/bin/caffeinate",
                "-is",
                "/project/bin/hermes-poller",
                "run",
            ],
            payloads["poller"]["ProgramArguments"],
        )
        self.assertTrue(
            all(
                payload["ProgramArguments"][0] == str(launcher)
                for payload in payloads.values()
            )
        )
        self.assertTrue(payloads["poller"]["KeepAlive"])
        self.assertEqual(
            {"REVIEW_UI_OPEN_BROWSER": "false"},
            payloads["review"]["EnvironmentVariables"],
        )
        info = background_app_info()
        self.assertEqual(
            "com.aceler.twenty-hermes.background",
            info["CFBundleIdentifier"],
        )
        self.assertIn("NSLocalNetworkUsageDescription", info)


if __name__ == "__main__":
    unittest.main()
