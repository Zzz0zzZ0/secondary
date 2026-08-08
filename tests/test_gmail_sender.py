import base64
import os
import unittest
from email import message_from_bytes
from email import policy
from unittest.mock import patch

from app.gmail_sender import build_raw_message


class GmailSenderTest(unittest.TestCase):
    def test_email_uses_configured_sender_and_html_alternative(self):
        with patch.dict(os.environ, {"GMAIL_FROM_ADDRESS": "Chloe <chloe@okgminerals.com>"}):
            raw = build_raw_message(
                {
                    "to": "customer@example.com",
                    "subject": "Subject",
                    "body": "Plain body",
                    "body_html": "<p>HTML body</p>",
                }
            )
        message = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)

        self.assertEqual("Chloe <chloe@okgminerals.com>", str(message["From"]))
        self.assertEqual("Plain body\n", message.get_body("plain").get_content())
        self.assertEqual("<p>HTML body</p>\n", message.get_body("html").get_content())
