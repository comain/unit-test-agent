from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from pydantic import BaseModel

RunCommand = Callable[..., "subprocess.CompletedProcess[str]"]
DEFAULT_ENFORCEMENT_OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024

TEST_ENFORCEMENT_USAGE_GUIDE = str(
    Path(__file__).resolve().parents[2] / "docs" / "test-enforce-usage.md"
)


def run_bounded_command(
    run_command: RunCommand,
    cmd: Sequence[str],
    *,
    cwd: Path | str,
    timeout: int,
    env: Optional[Dict[str, str]] = None,
    max_output_bytes: int = DEFAULT_ENFORCEMENT_OUTPUT_LIMIT_BYTES,
    stall_seconds: float = 0,
) -> subprocess.CompletedProcess[str]:
    """Run enforcement without retaining an unbounded child stream in memory.

    Real enforcement output is spooled to temporary files. Injected command
    runners keep the legacy capture-output contract used by tests and callers.
    """
    if run_command is not subprocess.run:
        return run_command(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )

    if stall_seconds > 0:
        return _run_with_stall_watchdog(
            cmd, cwd=cwd, env=env, timeout=timeout,
            stall_seconds=stall_seconds, max_output_bytes=max_output_bytes,
        )

    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            completed = run_command(
                list(cmd),
                cwd=str(cwd),
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            exc.stdout = _bounded_stream_text(stdout_file, max_output_bytes)
            exc.stderr = _bounded_stream_text(stderr_file, max_output_bytes)
            raise
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout=_bounded_stream_text(stdout_file, max_output_bytes),
            stderr=_bounded_stream_text(stderr_file, max_output_bytes),
        )


def _run_with_stall_watchdog(
    cmd: Sequence[str], *, cwd: Path | str, env: Optional[Dict[str, str]],
    timeout: float, stall_seconds: float, max_output_bytes: int,
) -> subprocess.CompletedProcess[str]:
    started = last_output = time.monotonic()
    previous_size = 0
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            list(cmd), cwd=str(cwd), env=env, stdout=stdout_file,
            stderr=stderr_file, start_new_session=True,
        )
        stalled = False
        try:
            while proc.poll() is None:
                now = time.monotonic()
                size = os.fstat(stdout_file.fileno()).st_size + os.fstat(stderr_file.fileno()).st_size
                if size != previous_size:
                    previous_size, last_output = size, now
                if now - started >= timeout or now - last_output >= stall_seconds:
                    _, descendants = _linux_process_tree_rss_bytes(proc.pid)
                    _terminate_process_tree_groups(proc, descendants)
                    if now - started >= timeout:
                        raise subprocess.TimeoutExpired(
                            list(cmd), timeout,
                            output=_bounded_stream_text(stdout_file, max_output_bytes),
                            stderr=_bounded_stream_text(stderr_file, max_output_bytes),
                        )
                    stalled = True
                    break
                time.sleep(min(0.5, stall_seconds))
            proc.wait()
        except BaseException:
            if proc.poll() is None:
                _, descendants = _linux_process_tree_rss_bytes(proc.pid)
                _terminate_process_tree_groups(proc, descendants)
            raise
        stderr = _bounded_stream_text(stderr_file, max_output_bytes)
        if stalled:
            stderr += f"\nUTA_ENFORCEMENT_STALLED stalled-after={stall_seconds:g}s"
        return subprocess.CompletedProcess(
            list(cmd), int(proc.returncode or 0),
            _bounded_stream_text(stdout_file, max_output_bytes), stderr,
        )


def run_resource_bounded_command(
    cmd: Sequence[str],
    *,
    cwd: Path | str,
    timeout: int,
    env: Optional[Dict[str, str]] = None,
    memory_limit_bytes: int,
    poll_seconds: float = 0.5,
    max_output_bytes: int = DEFAULT_ENFORCEMENT_OUTPUT_LIMIT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run a command in its own process group and cap aggregate Linux RSS."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd),
            stdout=stdout_file,
            stderr=stderr_file,
            env=env,
            start_new_session=True,
        )
        exhausted_rss = 0
        while proc.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                _terminate_process_group(proc)
                raise subprocess.TimeoutExpired(
                    list(cmd),
                    timeout,
                    output=_bounded_stream_text(stdout_file, max_output_bytes),
                    stderr=_bounded_stream_text(stderr_file, max_output_bytes),
                )
            rss, descendants = _linux_process_tree_rss_bytes(proc.pid)
            if rss is not None and rss >= memory_limit_bytes:
                exhausted_rss = rss
                _terminate_process_tree_groups(proc, descendants)
                break
            time.sleep(poll_seconds)
        proc.wait()
        stdout = _bounded_stream_text(stdout_file, max_output_bytes)
        stderr = _bounded_stream_text(stderr_file, max_output_bytes)
        if exhausted_rss:
            marker = (
                f"UTA_RESOURCE_EXHAUSTED process-group-rss={exhausted_rss} "
                f"limit={memory_limit_bytes}"
            )
            stderr = f"{stderr}\n{marker}".strip()
            return subprocess.CompletedProcess(list(cmd), 137, stdout=stdout, stderr=stderr)
        return subprocess.CompletedProcess(list(cmd), int(proc.returncode or 0), stdout=stdout, stderr=stderr)


@contextlib.contextmanager
def process_tree_memory_guard(
    proc: subprocess.Popen[Any],
    *,
    memory_limit_bytes: int,
    poll_seconds: float = 0.5,
):
    """Kill ``proc``'s process tree if its aggregate RSS crosses the limit.

    ``run_resource_bounded_command`` polls inline because it owns its own wait
    loop. A caller that blocks in ``communicate()`` cannot poll, and cannot
    switch to a poll loop either: it reads through pipes, so draining has to
    keep happening or the child deadlocks on a full pipe buffer. The same bound
    is therefore applied from a watchdog thread, over the same process-tree RSS
    walk and the same group kill, so the two lanes cannot disagree about what
    "over the limit" means.

    Yields a dict whose ``exhausted_rss`` is set to the observed total when the
    guard fires, and stays 0 otherwise.
    """
    state: Dict[str, int] = {"exhausted_rss": 0}
    if memory_limit_bytes <= 0:
        yield state
        return
    done = threading.Event()

    def watch() -> None:
        while not done.wait(poll_seconds):
            if proc.poll() is not None:
                return
            rss, descendants = _linux_process_tree_rss_bytes(proc.pid)
            if rss is not None and rss >= memory_limit_bytes:
                state["exhausted_rss"] = rss
                _terminate_process_tree_groups(proc, descendants)
                return

    watcher = threading.Thread(target=watch, name="uta-memory-guard", daemon=True)
    watcher.start()
    try:
        yield state
    finally:
        done.set()
        watcher.join(timeout=poll_seconds * 4)


def _linux_process_tree_rss_bytes(root_pid: int) -> tuple[Optional[int], set[int]]:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None, set()
    process_rows: Dict[int, tuple[int, int]] = {}
    for stat_path in proc_root.glob("[0-9]*/stat"):
        try:
            raw = stat_path.read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2 :].split()
            process_rows[int(stat_path.parent.name)] = (int(fields[1]), max(0, int(fields[21])))
        except (OSError, ValueError, IndexError):
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent_pid, _) in process_rows.items():
            if parent_pid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    total_pages = sum(process_rows.get(pid, (0, 0))[1] for pid in descendants)
    return total_pages * os.sysconf("SC_PAGE_SIZE"), descendants


def _terminate_process_tree_groups(proc: subprocess.Popen[Any], descendants: set[int]) -> None:
    # Verifier commands create their own sessions so their grandchildren do not
    # remain in the outer enforcement process group. Terminate each observed
    # descendant group before terminating the root group.
    for pid in sorted(descendants - {proc.pid}, reverse=True):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    _terminate_process_group(proc)


def _terminate_process_group(proc: subprocess.Popen[Any], grace_seconds: float = 2.0) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        if proc.poll() is None:
            proc.kill()
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def _bounded_stream_text(stream: Any, max_bytes: int) -> str:
    stream.flush()
    size = stream.tell()
    stream.seek(0)
    if size <= max_bytes:
        payload = stream.read()
    else:
        head_size = max_bytes // 4
        tail_size = max_bytes - head_size
        head = stream.read(head_size)
        stream.seek(-tail_size, 2)
        tail = stream.read(tail_size)
        marker = f"\n... output truncated: {size - max_bytes} bytes omitted ...\n".encode()
        payload = head + marker + tail
    return payload.decode("utf-8", errors="replace")


class QualityGateStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    timeout = "timeout"
    command_error = "command_error"
    missing_evidence = "missing_evidence"
    skipped = "skipped"


class QualityGateResult(BaseModel):
    status: QualityGateStatus
    passed: bool
    command: List[str]
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    summary: str = ""
    usage_guide: str = TEST_ENFORCEMENT_USAGE_GUIDE
    language: str = "java"
    backend: str = "maven_enforcer"
    evidence: Optional[Dict[str, Any]] = None


def json_object(payload: str) -> Optional[Dict[str, Any]]:
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def has_cli_option(cmd: Sequence[str], option: str) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in cmd)


@dataclass(frozen=True)
class ValidationVerdict:
    """Result of validating an enforcement evidence payload."""

    passed: bool
    reason_code: str
    message: str = ""


def finalize_evidence(evidence: Mapping[str, Any], *, evidence_id_prefix: str) -> Dict[str, Any]:
    """Stamp a deterministic content-hash evidenceId onto an evidence payload."""
    payload = dict(evidence)
    canonical = json.dumps(
        {key: value for key, value in payload.items() if key != "evidenceId"},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload["evidenceId"] = f"{evidence_id_prefix}:{digest[:16]}"
    return payload


def git_output(repo: Path, *args: str) -> str:
    try:
        from uta.shared.git import git

        completed = git(timeout=30).run(
            repo,
            *args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def validate_evidence_envelope(
    evidence: Mapping[str, Any],
    *,
    language: str,
    backend: str,
    schema_version: int,
    expected_head: Optional[str] = None,
) -> Optional[ValidationVerdict]:
    """Shared envelope checks every language evidence payload must pass.

    Returns a failing verdict, or None when language-specific gate checks may
    proceed (including the language's own passed-status verdict).
    """
    display = language.capitalize()
    if int(evidence.get("schemaVersion") or 0) != schema_version:
        return ValidationVerdict(False, "unknown_schema_version", f"Unsupported {display} enforcement evidence schema version")
    if evidence.get("language") != language or evidence.get("backend") != backend:
        return ValidationVerdict(False, "wrong_backend", f"Evidence is not {display} enforcement evidence")
    if expected_head and evidence.get("headCommit") != expected_head:
        return ValidationVerdict(False, "stale_head", "Evidence head commit does not match the expected branch head")
    if evidence.get("status") != "passed" or evidence.get("passed") is not True:
        return ValidationVerdict(
            False,
            str(evidence.get("reasonCode") or "failed"),
            str(evidence.get("summary") or f"{display} enforcement did not pass"),
        )
    return None


def evidence_marker_header(language: str, evidence: Mapping[str, Any]) -> str:
    return (
        f"[test-enforcer] {language} enforcement {evidence.get('status')} "
        f"reason={evidence.get('reasonCode')} schema={evidence.get('schemaVersion')}"
    )


def evidence_marker_payload(language: str, evidence: Mapping[str, Any]) -> str:
    return f"UTA_{language.upper()}_ENFORCEMENT_EVIDENCE={json.dumps(dict(evidence), ensure_ascii=False, sort_keys=True)}"
