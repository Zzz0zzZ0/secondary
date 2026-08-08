import base64
import json
import os
from email.message import EmailMessage
from pathlib import Path
from urllib.request import getproxies


GMAIL_SCOPE = ["https://www.googleapis.com/auth/gmail.send"]


def https_proxy_url():
    return os.getenv("GMAIL_HTTPS_PROXY") or getproxies().get("https")


def credential_paths():
    config_home = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    credential_dir = config_home / "twenty-hermes" / "credentials"
    client = Path(os.getenv("GMAIL_CLIENT_SECRET_PATH", credential_dir / "gmail-client.json"))
    token = Path(os.getenv("GMAIL_TOKEN_PATH", credential_dir / "gmail-token.json"))
    return client, token


def authorize():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError("Missing Gmail dependencies. Run: ./scripts/setup_python.sh") from exc
    client_path, token_path = credential_paths()
    if not client_path.exists():
        raise RuntimeError(f"Gmail OAuth client file not found: {client_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), GMAIL_SCOPE)
    credentials = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    token_path.chmod(0o600)
    return token_path


def load_credentials():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError("Missing Gmail dependencies. Run: ./scripts/setup_python.sh") from exc
    _, token_path = credential_paths()
    if not token_path.exists():
        raise RuntimeError("Gmail is not authorized. Run the gmail-auth command first.")
    credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPE)
    if credentials.expired and credentials.refresh_token:
        proxy_url = https_proxy_url()
        if proxy_url:
            import requests
            session = requests.Session()
            session.proxies.update({"http": proxy_url, "https": proxy_url})
            credentials.refresh(Request(session=session))
        else:
            credentials.refresh(Request())
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
    if not credentials.valid:
        raise RuntimeError("Gmail credentials are invalid; run gmail-auth again")
    return credentials


def preflight():
    # Validate safety gates and OAuth before an Outbox item is claimed.
    prepare_payload({"to": "validation@example.invalid", "subject": "validation", "body": "validation"})
    load_credentials()


def prepare_payload(payload):
    mode = os.getenv("EMAIL_SEND_MODE", "redirect")
    if mode not in ("redirect", "live"):
        raise RuntimeError("EMAIL_SEND_MODE must be redirect or live")
    prepared = dict(payload)
    original_to = payload["to"]
    if mode == "redirect":
        test_recipient = os.getenv("EMAIL_TEST_RECIPIENT")
        if not test_recipient:
            raise RuntimeError("EMAIL_TEST_RECIPIENT is required in redirect mode")
        prepared["to"] = test_recipient
        prepared["subject"] = f"[TEST][Original: {original_to}] {payload['subject']}"
        prepared["body"] = (
            f"TEST REDIRECT — original recipient: {original_to}\n\n" + payload["body"]
        )
    elif os.getenv("EMAIL_LIVE_SEND_ENABLED", "false").lower() != "true":
        raise RuntimeError("Live sending requires EMAIL_LIVE_SEND_ENABLED=true")
    return prepared


def build_raw_message(payload):
    message = EmailMessage()
    message["To"] = payload["to"]
    message["Subject"] = payload["subject"]
    from_address = os.getenv("GMAIL_FROM_ADDRESS")
    if from_address:
        message["From"] = from_address
    message.set_content(payload["body"])
    if payload.get("body_html"):
        message.add_alternative(payload["body_html"], subtype="html")
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def send(payload):
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Missing Gmail dependencies. Run: ./scripts/setup_python.sh") from exc
    prepared = prepare_payload(payload)
    credentials = load_credentials()
    endpoint = os.getenv(
        "GMAIL_API_ENDPOINT",
        "https://www.googleapis.com/gmail/v1/users/me/messages/send",
    )
    session = requests.Session()
    proxy_url = https_proxy_url()
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})
    response = session.post(
        endpoint,
        headers={"Authorization": f"Bearer {credentials.token}"},
        json={"raw": build_raw_message(prepared)},
        timeout=(10, 30),
    )
    response.raise_for_status()
    return response.json()


def preview(payload):
    prepared = prepare_payload(payload)
    return json.dumps(prepared, ensure_ascii=False, indent=2)
