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
    renew_delivery_lease,
)
from app.outbox import approve_message
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


class LeaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=20, max_length=256)


class CompleteRequest(LeaseRequest):
    provider_message_id: Optional[str] = Field(default=None, max_length=512)
    provider_thread_id: Optional[str] = Field(default=None, max_length=512)


class FailRequest(LeaseRequest):
    result: Literal["retryable", "permanent", "unknown"]
    error_code: str = Field(min_length=1, max_length=128)
    error_message: str = Field(default="", max_length=2000)


class ApproveRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    subject: Optional[str] = Field(default=None, max_length=998)
    body: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=2000)


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


@app.post("/v1/deliveries/{delivery_id}/heartbeat")
async def heartbeat(
    delivery_id: UUID,
    request: LeaseRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_credentials),
):
    consumer_scopes(credentials)
    expires_at = await run_in_threadpool(
        renew_delivery_lease,
        delivery_id,
        request.worker_id,
        request.lease_token,
    )
    if expires_at is None:
        raise HTTPException(status_code=409, detail="Lease is missing, expired, or owned by another worker")
    return {"delivery_id": str(delivery_id), "lease_expires_at": expires_at}


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
        request.lease_token,
        request.provider_message_id,
        request.provider_thread_id,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Lease is missing, expired, or owned by another worker")
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
        request.lease_token,
        request.result,
        request.error_code,
        request.error_message,
    )
    if result is None:
        raise HTTPException(status_code=409, detail="Lease is missing, expired, or owned by another worker")
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


@app.post("/v1/messages/{message_id}/approve-and-enqueue", status_code=201)
async def approve_and_enqueue(
    message_id: UUID,
    request: ApproveRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_credentials),
):
    require_producer_token(credentials)
    try:
        outbox_id = await run_in_threadpool(
            approve_message,
            message_id,
            request.reviewer,
            request.subject,
            request.body,
            request.note,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"message_id": str(message_id), "delivery_id": str(outbox_id), "status": "queued"}
