import asyncio
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.delivery_service import (
    claim_delivery,
    complete_delivery,
    fail_delivery,
    queue_status,
)
from .auth import consumer_scopes, require_consumer_scope, require_producer_token


app = FastAPI(
    title="Twenty Hermes Outbox Service",
    version="1.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)
bearer = HTTPBearer(auto_error=False)


class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    channels: list[Literal["email", "linkedin"]] = Field(min_length=1, max_length=2)
    providers: list[str] = Field(min_length=1, max_length=10)
    max_items: int = Field(default=1, ge=1, le=10)
    wait_seconds: int = Field(default=20, ge=0, le=20)


class CompleteRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)


class FailRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)


def _credentials(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
):
    return credentials


@app.get("/health")
def health():
    return {"status": "ok", "service": "outbox-api", "version": "1.0.0"}


@app.get("/ready")
async def ready():
    try:
        await run_in_threadpool(queue_status)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Outbox database is unavailable") from exc
    return {"status": "ready"}


@app.post("/v1/deliveries/claim")
async def claim(
    request: ClaimRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_credentials),
):
    scopes = consumer_scopes(credentials)
    require_consumer_scope(scopes, request.channels, request.providers)
    deadline = asyncio.get_running_loop().time() + request.wait_seconds
    items: list[dict[str, Any]] = []
    while True:
        while len(items) < request.max_items:
            item = await run_in_threadpool(
                claim_delivery,
                request.worker_id,
                request.channels,
                request.providers,
            )
            if item is None:
                break
            items.append(item)
        if items or asyncio.get_running_loop().time() >= deadline:
            return {"items": items}
        await asyncio.sleep(1)


@app.post("/v1/deliveries/{delivery_id}/complete")
async def complete(
    delivery_id: UUID,
    request: CompleteRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_credentials),
):
    consumer_scopes(credentials)
    result = await run_in_threadpool(
        complete_delivery,
        delivery_id,
        request.worker_id,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Delivery is no longer sending")
    return {"delivery_id": str(delivery_id), **result}


@app.post("/v1/deliveries/{delivery_id}/fail")
async def fail(
    delivery_id: UUID,
    request: FailRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_credentials),
):
    consumer_scopes(credentials)
    result = await run_in_threadpool(
        fail_delivery,
        delivery_id,
        request.worker_id,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Delivery is no longer sending")
    return {"delivery_id": str(delivery_id), **result}


@app.get("/v1/status")
async def status(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_credentials),
):
    require_producer_token(credentials)
    return {
        "generated_at": datetime.now(timezone.utc),
        "queues": await run_in_threadpool(queue_status),
    }
