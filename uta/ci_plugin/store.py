from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from uta.ci_plugin.models import CiTaskRecord

LOGGER = logging.getLogger(__name__)


class JsonCiTaskStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.tasks_root = self.root / "tasks"
        self.tasks_root.mkdir(parents=True, exist_ok=True)

    def save(self, record: CiTaskRecord) -> None:
        path = self._path(record.task_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(record.model_dump_json(), encoding="utf-8")
        tmp_path.replace(path)

    def load(self, task_id: str) -> Optional[CiTaskRecord]:
        path = self._path(task_id)
        if not path.exists():
            return None
        return CiTaskRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_records(
        self,
        *,
        since: Optional[datetime] = None,
        limit: int = 200,
    ) -> list[CiTaskRecord]:
        records: list[CiTaskRecord] = []
        for path in self.tasks_root.glob("*.json"):
            try:
                record = CiTaskRecord.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                LOGGER.warning("ci_record_load_failed path=%s", path, exc_info=True)
                continue
            if since and _aware_datetime(record.created_at) < _aware_datetime(since):
                continue
            records.append(record)
        records.sort(key=lambda item: _aware_datetime(item.created_at), reverse=True)
        return records[: max(1, int(limit))]

    def _path(self, task_id: str) -> Path:
        safe_task_id = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(task_id)).strip("-")
        return self.tasks_root / f"{safe_task_id or 'task'}.json"


def _aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
