from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from uta.ci_plugin.context import RepairContextExporter
from uta.ci_plugin.enforcement import MavenEnforcementRunner, PythonEnforcementRunner
from uta.ci_plugin.protocols.factory import build_registry
from uta.ci_plugin.routes import router
from uta.ci_plugin.service import CiPluginService
from uta.ci_plugin.store import JsonCiTaskStore
from uta.ci_plugin.workspace import GitWorkspaceManager
from uta.config import Settings, settings
from uta.tasks.manager import TaskManager


def create_default_service(config: Settings = settings) -> CiPluginService:
    allowed_hosts = [item.strip() for item in config.ci_allowed_git_hosts.split(",") if item.strip()]
    return CiPluginService(
        workspace_manager=GitWorkspaceManager(
            workspace_root=Path(config.ci_workspace_root),
            allowed_hosts=allowed_hosts,
            git_ssh_key_path=config.ci_git_ssh_key_path,
            command_timeout_seconds=config.ci_git_command_timeout_seconds,
            command_retry_times=config.ci_git_command_retry_times,
            command_retry_delay_seconds=config.ci_git_command_retry_delay_seconds,
        ),
        enforcement_runner=MavenEnforcementRunner(
            command=config.ci_enforcement_command,
            timeout_seconds=config.ci_enforcement_timeout_seconds,
        ),
        python_enforcement_runner=PythonEnforcementRunner(
            command=config.ci_python_enforcement_command,
            timeout_seconds=config.ci_python_enforcement_timeout_seconds,
            coverage_gate=config.ci_diff_coverage_gate,
            mutation_gate=config.ci_diff_mutation_gate,
        ),
        task_manager=TaskManager(config.ci_task_db_path),
        context_exporter=RepairContextExporter(Path(config.ci_context_runtime_root)),
        record_store=JsonCiTaskStore(Path(config.ci_record_store_root)),
        protocols=build_registry(config),
    )


def create_app(service: Optional[CiPluginService] = None, public_base_url: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="unit-test-agent-ci-plugin", version="0.1.0")
    app.state.ci_plugin_service = service
    app.state.ci_plugin_service_factory = create_default_service
    app.state.ci_public_base_url = public_base_url if public_base_url is not None else settings.ci_public_base_url
    app.include_router(router)
    app.include_router(router, prefix="/unit-test")
    return app


app = create_app()
