import os
import socket
from typing import Any, Dict, Optional

from uta.tasks.db import TaskDB
from uta.tasks.manager import config_hash


class TaskScheduler:
    def __init__(self, db_path: Optional[str] = None, *, runner_id: Optional[str] = None):
        self.db = TaskDB(db_path)
        self.db.init()
        self.runner_id = runner_id or f"{socket.gethostname()}:{os.getpid()}"

    def heartbeat(
        self,
        *,
        repo_task_id: Optional[int],
        status: str,
        message: Optional[str] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        loaded_config_hash = config_hash(config_snapshot) if config_snapshot else None
        self.db.upsert_heartbeat(
            self.runner_id,
            repo_task_id=repo_task_id,
            pid=os.getpid(),
            hostname=socket.gethostname(),
            status=status,
            message=message,
            loaded_config_hash=loaded_config_hash,
        )

    def acquire_next(self, *, allow_same_repo_concurrency: bool = False, include_failed: bool = False):
        task = self.db.acquire_next_task(
            allow_same_repo_concurrency=allow_same_repo_concurrency,
            include_failed=include_failed,
        )
        if task:
            self.db.add_event(task["id"], None, "scheduler_selected", f"Acquired by {self.runner_id}", stage="acquired")
            self.heartbeat(repo_task_id=task["id"], status="RUNNING", message="task acquired")
        else:
            # Idle state is recorded in runner_heartbeats only — not task_events.
            self.heartbeat(repo_task_id=None, status="IDLE", message="no queued task")
        return task
