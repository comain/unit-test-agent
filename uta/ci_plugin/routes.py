from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from uta.ci_plugin.fix_sessions import (
    CreateFixSessionRequest,
    FixSessionMessageRequest,
    append_message,
    retry_session,
)
from uta.ci_plugin.models import utc_now
from uta.ci_plugin.reporting import CiReportRenderer
from uta.ci_plugin.service import CiPluginService, FixSessionRateLimitError, FixSessionUnsupportedError

router = APIRouter()
renderer = CiReportRenderer()


def get_ci_plugin_service(request: Request) -> CiPluginService:
    service = request.app.state.ci_plugin_service
    if service is None:
        service = request.app.state.ci_plugin_service_factory()
        request.app.state.ci_plugin_service = service
    return service


async def _handle_trigger(
    request: Request,
    background_tasks: BackgroundTasks,
    service: CiPluginService,
    protocol_name: str,
) -> JSONResponse:
    protocol = service.protocols.get(protocol_name) if service.protocols else None
    if protocol is None:
        return JSONResponse({"error": f"unknown CI protocol: {protocol_name}"}, status_code=404)

    body = await request.body()
    headers = request.headers
    try:
        protocol.verify(body, headers)
        trigger = protocol.parse_trigger(body, headers)
    except Exception as exc:  # noqa: BLE001
        resp = protocol.error_response(exc)
        return JSONResponse(resp.body, status_code=resp.status_code)

    if trigger is None:
        resp = protocol.ignored_response()
        return JSONResponse(resp.body, status_code=resp.status_code)

    public_base_url = _public_base_url(request)
    record = service.submit(trigger, public_base_url=public_base_url, run_inline=False, protocol=protocol_name)
    background_tasks.add_task(service.run_check, record.task_id, public_base_url=public_base_url)
    task_url = record.task_url or _absolute_url(request, f"/task-status/{record.task_id}")
    report_url = record.report_url or task_url
    resp = protocol.trigger_response(record, task_url=task_url, report_url=report_url)
    return JSONResponse(resp.body, status_code=resp.status_code)


@router.post("/api/v1/{protocol_name}/webhook")
async def trigger_from_protocol(
    protocol_name: str,
    request: Request,
    background_tasks: BackgroundTasks,
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> JSONResponse:
    return await _handle_trigger(request, background_tasks, service, protocol_name)


@router.get("/healthz")
def healthz(service: CiPluginService = Depends(get_ci_plugin_service)) -> Dict[str, object]:
    return service.health()


@router.get("/healthcheck.html", response_class=HTMLResponse)
def healthcheck_html() -> HTMLResponse:
    return HTMLResponse("<!doctype html><html><body>ok</body></html>")


@router.get("/readyz")
def readyz(response: Response, service: CiPluginService = Depends(get_ci_plugin_service)) -> Dict[str, object]:
    health = service.health()
    if not ((health.get("runner") or {}).get("ready")):
        response.status_code = 503
    return health


@router.get("/docs/test-enforce-usage.md")
def test_enforcement_usage_doc() -> PlainTextResponse:
    path = Path(__file__).resolve().parents[2] / "docs" / "test-enforce-usage.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="test-enforcement usage doc not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@router.get("/jobs/recent.html", response_class=HTMLResponse)
def recent_jobs_page(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=200, ge=1, le=1000),
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> HTMLResponse:
    generated_at = utc_now()
    since = generated_at - timedelta(hours=hours)
    records = service.recent_records(since=since, limit=limit)
    return HTMLResponse(
        renderer.recent_jobs_html(records, since=since, generated_at=generated_at, hours=hours, limit=limit)
    )


@router.get("/jobs/recent/data")
def recent_jobs_data(
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=200, ge=1, le=1000),
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> Dict[str, Any]:
    generated_at = utc_now()
    since = generated_at - timedelta(hours=hours)
    records = service.recent_records(since=since, limit=limit)
    return renderer.recent_jobs_detail(records, since=since, generated_at=generated_at, hours=hours, limit=limit)


@router.get("/task-status/{task_id}", response_class=HTMLResponse)
def task_status(task_id: str, service: CiPluginService = Depends(get_ci_plugin_service)) -> HTMLResponse:
    record = _get_record_or_404(service, task_id)
    return HTMLResponse(renderer.status_html(record))


@router.get("/task-status/{task_id}/data")
def task_status_data(task_id: str, service: CiPluginService = Depends(get_ci_plugin_service)) -> Dict[str, Any]:
    record = _get_record_or_404(service, task_id)
    return renderer.detail(record)


@router.get("/reports/{task_id}/index.html", response_class=HTMLResponse)
def report_page(task_id: str, service: CiPluginService = Depends(get_ci_plugin_service)) -> HTMLResponse:
    record = _get_record_or_404(service, task_id)
    return HTMLResponse(renderer.report_html(record))


@router.get("/reports/{task_id}/detail")
def report_detail(task_id: str, service: CiPluginService = Depends(get_ci_plugin_service)) -> Dict[str, Any]:
    record = _get_record_or_404(service, task_id)
    return renderer.detail(record)


@router.get("/reports/{task_id}/fix-sessions/{session_id}/progress", response_class=HTMLResponse)
def fix_session_progress_page(
    task_id: str,
    session_id: str,
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> HTMLResponse:
    record = _get_record_or_404(service, task_id)
    progress = service.repair_progress(record, session_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="fix session not found")
    return HTMLResponse(renderer.repair_progress_html(progress))


@router.get("/reports/{task_id}/fix-sessions/{session_id}/progress/data")
def fix_session_progress_data(
    task_id: str,
    session_id: str,
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> Dict[str, Any]:
    record = _get_record_or_404(service, task_id)
    progress = service.repair_progress(record, session_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="fix session not found")
    return progress


@router.post("/reports/{task_id}/fix-sessions")
def create_report_fix_session(
    task_id: str,
    payload: CreateFixSessionRequest,
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> Dict[str, Any]:
    record = _get_record_or_404(service, task_id)
    try:
        return service.create_fix_session(record, payload)
    except FixSessionUnsupportedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FixSessionRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.post("/reports/{task_id}/fix-sessions/{session_id}/messages")
def append_report_fix_session_message(
    task_id: str,
    session_id: str,
    payload: FixSessionMessageRequest,
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> Dict[str, Any]:
    record = _get_record_or_404(service, task_id)
    session = append_message(record, session_id, payload.message)
    if session is None:
        raise HTTPException(status_code=404, detail="fix session not found")
    service.save(record)
    return {"session": session}


@router.post("/reports/{task_id}/fix-sessions/{session_id}/retry")
def retry_report_fix_session(
    task_id: str,
    session_id: str,
    payload: FixSessionMessageRequest,
    service: CiPluginService = Depends(get_ci_plugin_service),
) -> Dict[str, Any]:
    record = _get_record_or_404(service, task_id)
    session = retry_session(record, session_id, payload.message)
    if session is None:
        raise HTTPException(status_code=404, detail="fix session not found")
    service.save(record)
    return {"session": session}


def _get_record_or_404(service: CiPluginService, task_id: str):
    record = service.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="task not found")
    return record


def _absolute_url(request: Request, path: str) -> str:
    return _public_base_url(request) + path


def _public_base_url(request: Request) -> str:
    configured = getattr(request.app.state, "ci_public_base_url", "")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")
