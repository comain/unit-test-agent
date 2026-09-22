from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest


REAL_E2E_MODES = frozenset({"real", "nightly"})


def real_e2e_mode() -> str | None:
    mode = os.environ.get("UTA_E2E_MODE", "").strip().lower()
    if mode in REAL_E2E_MODES:
        return mode
    return None


def require_real_e2e_mode() -> str:
    mode = real_e2e_mode()
    if mode is None:
        pytest.skip("set UTA_E2E_MODE=real or nightly to run real_e2e tests")
    return mode


def prepare_real_e2e_workspace(
    source: str | Path,
    *,
    workspace_root: str | Path,
    lane_name: str,
    run_id: str,
) -> Path:
    root = Path(workspace_root).expanduser().resolve() / _safe_name(run_id)
    destination = root / _safe_name(lane_name)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_text = str(source)
    source_path = Path(source_text).expanduser()
    if _looks_like_git_url(source_text):
        _git_clone(source_text, destination)
    elif (source_path / ".git").exists():
        _git_clone(str(source_path.resolve()), destination)
    else:
        shutil.copytree(source_path.resolve(), destination, ignore=shutil.ignore_patterns(".git", ".uta_cache", ".uta_reports"))
    return destination


def write_real_e2e_report(
    path: str | Path,
    *,
    lanes: Sequence[Mapping[str, Any]],
    environment: Mapping[str, Any],
    tool_versions: Mapping[str, Any],
) -> Path:
    rows = [_normalize_lane_record(lane) for lane in lanes]
    payload = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "passed": sum(1 for lane in rows if lane.get("status") == "passed"),
            "failed": sum(1 for lane in rows if lane.get("status") == "failed"),
            "skipped": sum(1 for lane in rows if lane.get("status") == "skipped"),
        },
        "environment": dict(environment),
        "toolVersions": dict(tool_versions),
        "lanes": rows,
    }
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return report_path


def collect_tool_versions(commands: Sequence[str] = ("git", "python3", "mvn", "mutmut")) -> dict[str, str]:
    versions: dict[str, str] = {}
    for command in commands:
        executable = shutil.which(command)
        if not executable:
            versions[command] = "missing"
            continue
        args = [executable, "--version"]
        if command == "mvn":
            args = [executable, "-version"]
        try:
            result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=10)
            output = (result.stdout or result.stderr or "").strip().splitlines()
            versions[command] = output[0] if output else f"exit={result.returncode}"
        except Exception as exc:  # noqa: BLE001
            versions[command] = f"error: {exc}"
    return versions


def default_real_e2e_report_path() -> Path:
    return Path(".uta_reports") / "real-e2e" / "real-e2e-results.json"


def new_real_e2e_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def missing_lane(
    name: str,
    *,
    language: str,
    repo: str | Path,
    reason: str,
    base_ref: str = "origin/master",
) -> dict[str, Any]:
    return _normalize_lane_record({
        "name": name,
        "language": language,
        "status": "skipped",
        "reason": reason,
        "repo": str(repo),
        "workspace": "",
        "baseRef": base_ref,
        "artifacts": {},
    })


def git_current_branch(repo: str | Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def run_python_diff_repair_lane(
    *,
    source: str | Path,
    workspace_root: str | Path,
    run_id: str,
    diff_depth: int = 10,
    coverage_gate: float = 80.0,
    mutation_gate: float = 90.0,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Live-OpenCode lane: real repo, real `HEAD~n` diff, real verify + repair.

    Unlike the verification-only real lanes, this drives a live OpenCode fix
    session. The "changed code" is computed from the last ``diff_depth`` commits
    of the cloned repo (``HEAD~n...HEAD``), so mutmut scopes to a real diff and
    surviving mutants trigger a genuine repair turn — the path a synthetic
    throwaway repo cannot reach.

    Requires UTA_E2E_REAL_OPENCODE-style live model access; callers gate on it.

    Target choice matters. Point at a real external app repo via UTA_E2E_PY3_REPO
    for a meaningful run that reaches the mutation stage. The UTA self-repo default
    is only a session-path smoke: ``uta`` is editable-installed in the harness venv,
    so a clone's tests exercise the *installed* package and coverage of the clone's
    file reads 0% (coverage_gate_failed before mutation). A foreign repo is not
    installed in the harness venv, so its clone is the one imported and measured.
    """
    lane = "python3-uta-opencode"
    base_ref = f"HEAD~{max(1, int(diff_depth))}"
    started = time.monotonic()
    try:
        workspace = prepare_real_e2e_workspace(source, workspace_root=workspace_root, lane_name=lane, run_id=run_id)
    except Exception as exc:  # noqa: BLE001
        return {"name": lane, "language": "python", "status": "failed", "reason": f"workspace_error: {exc}", "repo": str(source), "baseRef": base_ref}

    from uta.enforcement.diff import changed_lines_by_file, changed_production_files
    from uta.shared.languages import default_registry

    py_adapter = default_registry().adapter_for("python")
    changed_files = changed_production_files(py_adapter, workspace, base_ref)
    if not changed_files:
        return missing_lane(lane, language="python", repo=workspace, reason="no_changed_python_in_diff", base_ref=base_ref)
    # Bound cost: smallest changed file is most likely to fit one focused repair turn.
    changed_files.sort(key=lambda rel: (workspace / rel).stat().st_size if (workspace / rel).exists() else 0)
    target_file = changed_files[0]
    changed_lines = changed_lines_by_file(workspace, base_ref, [target_file])
    if not (changed_lines.get(target_file) or []):
        return missing_lane(lane, language="python", repo=workspace, reason="no_changed_lines_in_diff", base_ref=base_ref)

    from uta.app.cli import _ensure_model_auth
    from uta.language.python.batch import run_python_batch_generation
    from agent_core.harness.config import generate_opencode_config
    from uta.tasks.manager import TaskManager
    from uta.shared.targets import TargetIdentity

    target = TargetIdentity(language="python", target_id=f"pyfile:{target_file}", display_name=target_file, source_path=target_file, granularity="file")
    db_path = Path(workspace).parent / f"{_safe_name(lane)}-tasks.db"
    manager = TaskManager(db_path)
    rdc_context = {"enforcement": {"evidence": {"changedLines": changed_lines}}}
    task_id = manager.create_task_targets(
        repo_path=str(workspace), targets=[target], language="python",
        base_ref=base_ref, coverage_gate=coverage_gate, mutation_gate=mutation_gate, rdc_context=rdc_context,
    )
    manager.mark_running(task_id, stage="startup")
    # Run the cloned workspace's tests with an interpreter that has the deps
    # available. pytest runs from the workspace cwd, so the clone's package takes
    # sys.path precedence over any editable install of the same package — meaning
    # the diff under test is the clone's, not the source's. For the UTA self-repo
    # the harness interpreter already has every dependency; a foreign
    # UTA_E2E_PY3_REPO must expose its deps to this interpreter (or set UTA_PYTHON_BIN).
    import sys as _sys

    from agent_core.harness.fallback import ProviderRateLimitError

    prior_python_bin = os.environ.get("UTA_PYTHON_BIN")
    os.environ["UTA_PYTHON_BIN"] = _sys.executable

    def _base(extra: dict[str, Any]) -> dict[str, Any]:
        row = {"name": lane, "language": "python", "repo": str(source), "baseRef": base_ref,
               "target": target.target_id, "targetCount": 1,
               "changedLineCount": len(changed_lines.get(target_file) or []),
               "elapsedSeconds": round(time.monotonic() - started, 1)}
        row.update(extra)
        return row

    try:
        generate_opencode_config(str(workspace))
        _ensure_model_auth(str(workspace))
        result = run_python_batch_generation(
            repo_path=workspace, targets=[target], task_id=task_id, task_db_path=db_path,
            coverage_gate=coverage_gate, mutation_gate=mutation_gate, timeout_seconds=timeout_seconds,
        )
        task = manager.get_task(task_id)
        tr = result.results.get(target.target_id, {}) or {}
        ctx_md = workspace / ".uta_cache" / "python" / "mutation_repair" / "mutation-repair-context.md"
        # The lane validates that the live fix-session path EXECUTED over a real
        # diff. Whether the model hit the gate is nondeterministic, so it is
        # reported (taskStatus/mutationScore/gatePassed), not the pass criterion.
        ran_session = len(result.session_ids or []) >= 1
        return _base({
            "status": "passed" if ran_session else "failed",
            "reason": "" if ran_session else "no_opencode_session",
            "taskStatus": task.get("status"), "targetStatus": tr.get("status"),
            "targetReasonCode": tr.get("verification_reason") or "",
            "targetMessage": str(tr.get("verification_message") or "")[:400],
            "testsPass": tr.get("tests_pass"),
            "coverage": tr.get("coverage"),
            "gatePassed": task.get("status") == "COMPLETED",
            "mutationScore": tr.get("mutation_score") if isinstance(tr, dict) else None,
            "sessionCount": len(result.session_ids or []),
            "ranRepairSession": len(result.session_ids or []) > 1,
            "mutationContextWritten": ctx_md.exists(),
            "workspace": str(workspace),
        })
    except ProviderRateLimitError as exc:
        # Provider hiccup (no JSONL before timeout / rate limit) is not a UTA
        # defect — skip rather than red the suite.
        return _base({"status": "skipped", "reason": f"provider_unavailable: {exc}"})
    except Exception as exc:  # noqa: BLE001
        return _base({"status": "failed", "reason": f"run_error: {exc}"})
    finally:
        if prior_python_bin is None:
            os.environ.pop("UTA_PYTHON_BIN", None)
        else:
            os.environ["UTA_PYTHON_BIN"] = prior_python_bin


def _normalize_lane_record(lane: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(lane)
    row.setdefault("name", "")
    row.setdefault("language", "")
    row.setdefault("status", "skipped")
    row.setdefault("reason", "")
    row.setdefault("repo", "")
    row.setdefault("repoIdentity", row.get("repo", ""))
    row.setdefault("branch", "")
    row.setdefault("baseRef", "origin/master")
    row.setdefault("targetCount", _target_count_from_lane(row))
    row.setdefault("elapsedSeconds", 0.0)
    row.setdefault("workspace", "")
    row.setdefault("artifacts", {})
    row["targetCount"] = int(row.get("targetCount") or 0)
    row["elapsedSeconds"] = round(float(row.get("elapsedSeconds") or 0.0), 3)
    row["artifacts"] = dict(row.get("artifacts") or {})
    return row


def _target_count_from_lane(row: Mapping[str, Any]) -> int:
    if row.get("target"):
        return 1
    stages = row.get("stages") or []
    if isinstance(stages, Sequence):
        for stage in stages:
            if not isinstance(stage, Mapping):
                continue
            details = stage.get("details") or {}
            if isinstance(details, Mapping) and details.get("selected_count") is not None:
                return int(details.get("selected_count") or 0)
    return 0


def _looks_like_git_url(value: str) -> bool:
    return "://" in value or value.startswith("git@")


def _git_clone(source: str, destination: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", source, str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(value or "run"))
    return cleaned.strip("-") or "run"
