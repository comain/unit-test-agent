"""Targeted Java test verification for the durable generation cycle."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping


@dataclass(frozen=True)
class JavaTestVerificationPorts:
    run_test_selector: Callable[..., tuple[bool, str]]
    refresh_failure_summary: Callable[..., str]


def verify_tests(
    state: Mapping[str, Any], *, ports: JavaTestVerificationPorts
) -> Dict[str, Any]:
    """Run the stable batch's targeted suite once and route pass or repair."""
    batch = list(state.get("batch") or [])
    if not batch:
        return {
            "phase_outcome": "failed",
            "evidence": {"failure_reason": "verify_tests_requires_stable_batch"},
        }
    repo_path = str(state["repo_path"])
    module = state.get("module")
    test_names = [f"{class_fqn.split('.')[-1]}Test" for class_fqn in batch]
    selector = ",".join(test_names)
    started = time.perf_counter()
    passed, output = ports.run_test_selector(
        repo_path, selector, module=module,
        quality_gate_command=str(state.get("quality_gate_command") or ""),
    )
    elapsed = time.perf_counter() - started
    if not passed:
        output = ports.refresh_failure_summary(
            repo_path, selector, module, str(output or "")
        )
    attempts = int((state.get("attempts_by_phase") or {}).get("fix_tests", 0))
    maximum = int((state.get("max_attempts_by_phase") or {}).get("fix_tests", 3))
    evidence = {
        "tests_pass": bool(passed),
        "test_names": test_names,
        "test_selector": selector,
        "output": str(output or "")[-6000:],
        "elapsed_seconds": elapsed,
        "attempt": attempts,
        "max_attempts": maximum,
    }
    if passed:
        if str(state.get("quality_gate_backend") or "") == "maven_enforcer":
            return {"phase_outcome": "delegated", "evidence": evidence}
        return {"phase_outcome": "passed", "evidence": evidence}
    if attempts >= maximum:
        evidence["failure_reason"] = "test_repair_attempts_exhausted"
        return {"phase_outcome": "failed", "evidence": evidence}
    return {"phase_outcome": "repair", "evidence": evidence}


__all__ = ["JavaTestVerificationPorts", "verify_tests"]
