import hmac
import json
import os
from typing import Optional

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials


def _consumer_tokens():
    raw = os.getenv("OUTBOX_CONSUMER_TOKENS_JSON", "")
    if not raw:
        raise HTTPException(status_code=503, detail="Consumer authentication is not configured")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="Consumer authentication is invalid") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=503, detail="Consumer authentication is invalid")
    return value


def consumer_scopes(credentials: Optional[HTTPAuthorizationCredentials]):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing bearer token")
    for token, scopes in _consumer_tokens().items():
        if hmac.compare_digest(credentials.credentials, str(token)):
            if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
                raise HTTPException(status_code=503, detail="Consumer scopes are invalid")
            return set(scopes)
    raise HTTPException(status_code=401, detail="Invalid bearer token")


def require_consumer_scope(scopes, channels, providers):
    denied = [
        f"{channel}:{provider}"
        for channel in channels
        for provider in providers
        if f"{channel}:{provider}" not in scopes and "*:*" not in scopes
    ]
    if denied:
        raise HTTPException(
            status_code=403,
            detail="Token is not authorized for: " + ", ".join(denied),
        )


def require_producer_token(credentials: Optional[HTTPAuthorizationCredentials]):
    configured = os.getenv("OUTBOX_PRODUCER_TOKEN")
    if not configured:
        raise HTTPException(status_code=503, detail="Producer authentication is not configured")
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not hmac.compare_digest(credentials.credentials, configured)
    ):
        raise HTTPException(status_code=401, detail="Invalid producer bearer token")
