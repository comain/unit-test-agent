"""Small pure helpers shared by task-domain services."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from uta.tasks.models import CLASS_TASK_STATUSES, normalize_status


def repo_slug(repo_path: str) -> str:
    name = Path(repo_path).resolve().name or "repo"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "repo"


def normalize_repair_repo_identity(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"^https?://[^/@]+@", "https://", text)
    text = re.sub(r"^ssh://git@", "ssh://", text)
    text = re.sub(r"^git@", "", text).rstrip("/")
    return (text[:-4] if text.endswith(".git") else text).lower()


def repair_selection_target_ids(selection: Dict[str, Any]) -> tuple[str, ...]:
    targets = selection.get("targets")
    values = (
        [
            str(item.get("target_id") or item.get("target") or "").strip()
            for item in targets
            if isinstance(item, dict)
        ]
        if isinstance(targets, list)
        else [str(item or "").strip() for item in selection.get("class_fqns") or []]
    )
    return tuple(sorted({value for value in values if value}))


def normalize_class_status(status: str) -> str:
    candidate = (status or "").strip().upper()
    if "RATE" in candidate and "LIMIT" in candidate:
        return "PROVIDER_RATE_LIMITED"
    if "TIMEOUT" in candidate or "STALLED" in candidate:
        return "LLM_STALLED"
    if "BUDGET" in candidate:
        return "BUDGET_EXCEEDED"
    if "UNSAFE" in candidate:
        return "UNSAFE_DIFF"
    if "PROVIDER" in candidate and "ERROR" in candidate:
        return "PROVIDER_ERROR"
    if "MODEL" in candidate and ("NOT_FOUND" in candidate or "NOT FOUND" in candidate):
        return "PROVIDER_ERROR"
    return normalize_status(candidate, CLASS_TASK_STATUSES, fallback="FAIL")


def mutation_display_detail(result: Mapping[str, Any]) -> Optional[str]:
    score = result.get("mutation_score")
    summary = result.get("mutation_summary")
    if score is None or not isinstance(summary, Mapping):
        return None
    candidate_plan = summary.get("candidatePlan")
    if not isinstance(candidate_plan, Mapping):
        return None
    policy = candidate_plan.get("generationPolicy")
    if not isinstance(policy, Mapping):
        return None
    omitted = int(policy.get("omittedByCap") or 0)
    if not bool(policy.get("truncated")) and omitted <= 0:
        return None
    caps = policy.get("caps") if isinstance(policy.get("caps"), Mapping) else {}
    cap = int(caps.get("maxSelected") or policy.get("selectedOpportunities") or 0)
    return f"{score}(hard capped {cap})" if cap > 0 else str(score)


def config_hash(value: Dict[str, Any]) -> str:
    raw = json.dumps(value or {}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


TOKEN_TOTAL_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning")
