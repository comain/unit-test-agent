from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from uta.app.context import RepairContextExporter
from uta.app.protocols.factory import build_registry
from uta.app.routes import router
from uta.app.service import ApiTriggerService
from uta.app.store import JsonCiTaskStore
from uta.app.workspace import GitWorkspaceManager
from uta.shared.config import Settings, settings
from uta.tasks.manager import TaskManager


def create_default_service(config: Settings = settings) -> ApiTriggerService:
    allowed_hosts = [item.strip() for item in config.ci_allowed_git_hosts.split(",") if item.strip()]
    ci_inflight_dir = (
        Path(config.ci_inflight_dir)
        if config.ci_inflight_dir
        else Path(config.ci_record_store_root).parent / "runner" / "ci_inflight"
    )
    return ApiTriggerService(
        workspace_manager=GitWorkspaceManager(
            workspace_root=Path(config.ci_workspace_root),
            allowed_hosts=allowed_hosts,
            git_ssh_key_path=config.ci_git_ssh_key_path,
            git_access_token=config.ci_git_access_token,
            command_timeout_seconds=config.ci_git_command_timeout_seconds,
            command_retry_times=config.ci_git_command_retry_times,
            command_retry_delay_seconds=config.ci_git_command_retry_delay_seconds,
        ),
        task_manager=TaskManager(config.ci_task_db_path),
        context_exporter=RepairContextExporter(Path(config.ci_context_runtime_root)),
        record_store=JsonCiTaskStore(Path(config.ci_record_store_root)),
        inflight_dir=ci_inflight_dir,
        protocols=build_registry(config),
        ci_report_parallel_limit=config.ci_report_parallel_limit,
        async_repair_task_creation=True,
        config=config,
    )


def create_app(service: Optional[ApiTriggerService] = None, public_base_url: Optional[str] = None) -> FastAPI:
    # The API's composition root, matching the CLI's. Both entrypoints have to
    # register, because a workflow started from either asks the same registry.
    from uta.app.persistence import register_task_persistence
    from uta.composition.language_backends import register_language_backends
    from uta.app.agent_core_pin import verify_agent_core_pin

    # Same guard as the CLI's: either entrypoint can start a workflow, so
    # either one can start it on a pair that cannot finish it.
    verify_agent_core_pin()
    register_language_backends()
    register_task_persistence()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        current_service = application.state.api_trigger_service
        if current_service is None:
            current_service = application.state.api_trigger_service_factory()
            application.state.api_trigger_service = current_service
        current_service.recover_ci_queue()
        yield

    app = FastAPI(title="unit-test-agent-ci-plugin", version="0.1.0", lifespan=lifespan)
    app.state.api_trigger_service = service
    app.state.api_trigger_service_factory = create_default_service
    app.state.ci_public_base_url = public_base_url if public_base_url is not None else settings.ci_public_base_url
    app.include_router(router)
    app.include_router(router, prefix="/unit-test")
    return app


app = create_app()
