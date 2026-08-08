import json
import os
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.message_jobs import DEFAULT_STATE_DIR
from app.outbox import (
    approve_message,
    get_review_message,
    list_outbox_deliveries,
    list_review_messages,
    reject_message,
    save_message_edit,
)
from app.secondary_scheduler import SecondaryLeadScheduler
from app.runtime_report import (
    DEFAULT_REPORT_DIR,
    build_report,
    current_report_date,
    list_reports,
    write_report,
)
from .service import manager, regenerate_review_message


WEB_DIST = Path(__file__).resolve().parent.parent / "review_web" / "dist"
RUNTIME_REPORT_DIR = Path(
    os.getenv("HERMES_RUNTIME_REPORT_DIR", str(DEFAULT_REPORT_DIR))
).resolve()
app = FastAPI(title="Twenty → Hermes Review", docs_url="/api/docs")


@lru_cache(maxsize=1)
def _polling_store() -> SecondaryLeadScheduler:
    return SecondaryLeadScheduler(
        state_dir=Path(os.getenv("HERMES_POLL_STATE_DIR", str(DEFAULT_STATE_DIR)))
    )


class StartRunRequest(BaseModel):
    limit: int = Field(default=5, ge=1, le=20)
    sort: Literal["next_action", "recently_classified", "random"] = "next_action"


class ClassificationReviewRequest(BaseModel):
    lead_type: Literal[
        "no_current_demand",
        "unknown_demand",
        "referred",
        "below_moq",
    ]
    reviewer: str = Field(default="review-ui", min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)


class ReviewModeRequest(BaseModel):
    enabled: bool
    actor: str = Field(default="review-ui", min_length=1, max_length=200)


class MessageEditRequest(BaseModel):
    subject: Optional[str] = Field(default=None, max_length=500)
    body: str = Field(min_length=1, max_length=20000)


class MessageDecisionRequest(MessageEditRequest):
    reviewer: str = Field(default="review-ui", min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)


class MessageRejectRequest(BaseModel):
    reviewer: str = Field(default="review-ui", min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=2000)


class MessageRegenerateRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    reviewer: str = Field(default="review-ui", min_length=1, max_length=200)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "send_enabled": False,
        "review": _polling_store().review_configuration(),
    }


@app.post("/api/runs", status_code=202)
def start_run(request: StartRunRequest):
    try:
        inputs = _polling_store().preview_scheduled_inputs(
            request.limit,
            request.sort,
        )
        return manager.start(
            request.limit,
            request.sort,
            inputs,
            source="scheduled",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    try:
        return manager.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="测试任务不存在") from exc


@app.get("/api/runs/{run_id}/results")
def get_results(run_id: str):
    try:
        return {"run_id": run_id, "records": manager.results(run_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="测试任务不存在") from exc


@app.get("/api/polling/status")
def polling_status():
    return _polling_store().status()


@app.post("/api/polling/review-mode")
def set_polling_review_mode(request: ReviewModeRequest):
    try:
        return _polling_store().set_human_review_enabled(
            request.enabled,
            request.actor,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/polling/dashboard")
def polling_dashboard(
    days: int = Query(default=30, ge=1, le=120),
    run_limit: int = Query(default=10, ge=1, le=100),
):
    return _polling_store().dashboard(days, run_limit)


@app.get("/api/polling/runs")
def polling_runs(limit: int = Query(default=20, ge=1, le=100)):
    return {"runs": _polling_store().runs(limit)}


@app.get("/api/runtime-reports")
def runtime_reports():
    return {"records": list_reports(RUNTIME_REPORT_DIR)}


@app.post("/api/runtime-reports/generate")
def generate_runtime_report():
    timezone_name = os.getenv("TWENTY_BUSINESS_TIMEZONE", "Asia/Shanghai")
    now = datetime.now(timezone.utc)
    report = build_report(
        current_report_date(now, timezone_name),
        timezone_name=timezone_name,
        state_dir=Path(
            os.getenv("HERMES_POLL_STATE_DIR", str(DEFAULT_STATE_DIR))
        ),
        now=now,
        window_end=now,
    )
    write_report(report, RUNTIME_REPORT_DIR)
    return {
        "date": report["window"]["date"],
        "generated_at": report["generated_at"],
        "overall_status": report["overall_status"],
        "complete": report["complete"],
    }


def _runtime_report_path(report_date: str, suffix: str) -> Path:
    try:
        date.fromisoformat(report_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="报告日期格式错误") from exc
    path = RUNTIME_REPORT_DIR / f"{report_date}.{suffix}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="运行报告不存在")
    return path


@app.get("/api/runtime-reports/{report_date}")
def runtime_report_json(report_date: str, download: bool = False):
    path = _runtime_report_path(report_date, "json")
    if download:
        return FileResponse(
            path,
            media_type="application/json",
            filename=path.name,
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="运行报告 JSON 损坏") from exc


@app.get("/api/runtime-reports/{report_date}/markdown")
def runtime_report_markdown(report_date: str, download: bool = False):
    path = _runtime_report_path(report_date, "md")
    if download:
        return FileResponse(path, media_type="text/markdown", filename=path.name)
    return PlainTextResponse(
        path.read_text(encoding="utf-8"),
        media_type="text/markdown",
    )


@app.get("/api/polling/leads/{lead_id}")
def polling_lead_detail(lead_id: str):
    try:
        return _polling_store().lead_detail(lead_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="二级线索不存在") from exc


@app.post("/api/polling/leads/{lead_id}/classification")
def confirm_polling_classification(
    lead_id: str,
    request: ClassificationReviewRequest,
):
    try:
        return _polling_store().confirm_classification(
            lead_id,
            request.lead_type,
            request.reviewer,
            request.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="二级线索不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/polling/queue")
def polling_queue(
    limit: int = Query(default=50, ge=1, le=500),
    status: str = "",
):
    statuses = [value.strip() for value in status.split(",") if value.strip()]
    try:
        records = (
            _polling_store().queue(limit, statuses)
            if statuses
            else _polling_store().queue(limit)
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"records": records}


@app.get("/api/review/messages")
def review_messages(
    limit: int = Query(default=50, ge=1, le=200),
    status: Literal["pending_review", "approved", "rejected"] = (
        "pending_review"
    ),
):
    try:
        return {
            "records": list_review_messages(limit, status),
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/outbox/deliveries")
def outbox_deliveries(limit: int = Query(default=100, ge=1, le=500)):
    try:
        return {"records": list_outbox_deliveries(limit)}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/review/messages/{message_id}")
def review_message_detail(message_id: str):
    try:
        message = get_review_message(message_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if message is None:
        raise HTTPException(status_code=404, detail="待审消息不存在")
    return message


@app.patch("/api/review/messages/{message_id}")
def edit_review_message(
    message_id: str,
    request: MessageEditRequest,
):
    try:
        return save_message_edit(
            message_id,
            request.subject,
            request.body,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/review/messages/{message_id}/regenerate")
def regenerate_message(
    message_id: str,
    request: MessageRegenerateRequest,
):
    try:
        return regenerate_review_message(
            message_id,
            request.instruction,
            request.reviewer,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="待审消息不存在",
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/review/messages/{message_id}/approve")
def approve_review_message(
    message_id: str,
    request: MessageDecisionRequest,
):
    try:
        outbox_id = approve_message(
            message_id,
            request.reviewer,
            request.subject,
            request.body,
            request.note,
        )
        return {
            "message_id": message_id,
            "review_status": "approved",
            "outbox_id": str(outbox_id),
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/review/messages/{message_id}/reject")
def reject_review_message(
    message_id: str,
    request: MessageRejectRequest,
):
    try:
        return reject_message(
            message_id,
            request.reviewer,
            request.note,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


app.mount("/", StaticFiles(directory=WEB_DIST, html=True, check_dir=False), name="web")
