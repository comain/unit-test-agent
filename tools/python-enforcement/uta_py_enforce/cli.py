#!/usr/bin/env python3
"""Lightweight UTA Python test-enforcement tool.

This module is intentionally isolated from the full UTA app so local development
environments can sparse-checkout only ``tools/python-enforcement``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from uta_py_enforce import __version__
from uta_py_enforce.api import create_python_enforcement_binding
from uta_enforce_core.commands import SafeProcessRunner
from uta_enforce_core.contracts import (
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementTarget,
    QualityGates,
    RuntimeSelection,
)
from uta_enforce_core.diff import changed_lines_by_file, changed_production_python_files, git_output
from uta_enforce_core.dispatch import enforce
from uta_enforce_core.evidence import (
    BACKEND,
    SCHEMA_VERSION,
    aggregate_coverage,
    aggregate_mutation,
    finalize,
    format_evidence_markers,
)
from uta_enforce_core.registry import EnforcementRegistry
from uta_enforce_core.targets import target_payload, target_source_path

CORE_VERSION = __version__


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run lightweight UTA Python test enforcement.")
    parser.add_argument("--repo", default=".", help="Target Python repository.")
    parser.add_argument("--target", action="append", default=[], help="Python target path.py or path.py::symbol. Repeatable.")
    parser.add_argument("--test-path", action="append", default=[], help="Test path to run. Repeatable.")
    parser.add_argument("--base-ref", default="origin/master", help="Git base ref for incremental enforcement.")
    parser.add_argument("--coverage-gate", default="95", type=float, help="Coverage gate percentage.")
    parser.add_argument("--mutation-gate", default="95", type=float, help="Mutation gate percentage.")
    parser.add_argument("--syntax-version", default="python3", choices=("python3", "python2"), help="Python runtime lane.")
    parser.add_argument("--evidence-output", help="Optional JSON evidence output path.")
    parser.add_argument("--dev-skills-launcher-version", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Resolve changed targets without running the gate.")
    parser.add_argument("--json-output", action="store_true", help="Print machine-readable JSON only.")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    evidence = run_enforcement(args, repo)
    if args.evidence_output:
        Path(args.evidence_output).expanduser().write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.json_output:
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    else:
        print(format_evidence_markers(evidence), end="")
    return 0 if evidence.get("passed") else 1



def run_enforcement(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    """Build a request, dispatch it once, and present what comes back.

    This is a composition adapter, deliberately: it parses flags, decides the
    four things that make a run meaningless, and hands the rest to the sole
    contract. It used to drive `run_coverage`/`run_mutation`/`strict_test_candidates`
    itself, which made the tool local developers actually run a second
    implementation of the gate -- the exact thing the contract exists to
    prevent, in the place it was least likely to be noticed.
    """
    base_commit = git_output(repo, "rev-parse", "--verify", args.base_ref)
    head_commit = git_output(repo, "rev-parse", "HEAD")
    target_paths = resolve_targets(repo, args.target, args.base_ref)
    changed_files = changed_production_python_files(repo, args.base_ref)
    changed_line_map = changed_lines_by_file(repo, args.base_ref, sorted(set(changed_files + target_paths)))

    common = {
        "schemaVersion": SCHEMA_VERSION,
        "evidenceId": "",
        "language": "python",
        "backend": BACKEND,
        "repo": str(repo),
        "baseRef": args.base_ref,
        "baseCommit": base_commit,
        "headRef": "HEAD",
        "headCommit": head_commit,
        "changedProductionFiles": changed_files,
        "changedLines": changed_line_map,
        "targets": [target_payload(path) for path in target_paths],
        "coverage": None,
        "mutation": None,
        "commands": [],
        "artifacts": {},
        "setup": {"tool": "lightweight_tool", "coreVersion": CORE_VERSION},
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "utaVersion": "lightweight",
        "enforcementCoreVersion": CORE_VERSION,
    }
    if args.dev_skills_launcher_version:
        common["devSkillsLauncherVersion"] = args.dev_skills_launcher_version

    refusal = _refuse(common, args, base_commit, head_commit, target_paths)
    if refusal is not None:
        return refusal

    request = EnforcementRequest(
        repo_path=repo,
        language="python",
        targets=tuple(
            EnforcementTarget(
                language="python",
                target_id=path,
                source_path=path,
                test_paths=tuple(args.test_path or ()),
            )
            for path in target_paths
        ),
        test_paths=tuple(args.test_path or ()),
        base_ref=args.base_ref,
        quality_gates=QualityGates(
            diff_coverage_min=float(args.coverage_gate) / 100.0,
            diff_mutation_min=float(args.mutation_gate) / 100.0,
        ),
        runtime=RuntimeSelection(
            syntax_version=args.syntax_version,
            timeout_seconds=int(os.environ.get("UTA_PYTHON_GATE_TIMEOUT_SECONDS") or "7200"),
        ),
    )

    registry = EnforcementRegistry([create_python_enforcement_binding()])
    context = EnforcementInvocationContext(run_command=SafeProcessRunner())
    result = enforce(request, registry=registry, context=context)

    evidence = dict(result.evidence)
    target_results = list(evidence.get("targets") or [])
    passed = str(evidence.get("status") or "") == "passed"
    reason_code, summary = _diagnosis(evidence, target_results, passed=passed)

    return finalize(
        {
            **common,
            "status": evidence.get("status"),
            "passed": passed,
            "reasonCode": reason_code,
            "summary": summary,
            "targetResults": target_results,
            # Aggregated from the per-target evidence rather than taken from
            # the binding's roll-up, which drops `candidatePlan`.
            "coverage": aggregate_coverage(target_results, float(args.coverage_gate)),
            "mutation": aggregate_mutation(target_results, float(args.mutation_gate)),
            "commands": evidence.get("commands") or [],
            "artifacts": evidence.get("artifacts") or {},
        }
    )


def _refuse(
    common: dict[str, Any],
    args: argparse.Namespace,
    base_commit: str,
    head_commit: str,
    target_paths: Sequence[str],
) -> dict[str, Any] | None:
    """The conditions under which a verdict would be meaningless.

    Same four the full product refuses, for the same reason: an unresolvable
    base ref makes the diff empty, and an empty diff looks exactly like a clean
    change.
    """
    if not base_commit:
        return finalize({**common, "status": "command_error", "passed": False, "reasonCode": "missing_base_ref", "summary": f"Base ref is not available: {args.base_ref}"})
    if not head_commit:
        return finalize({**common, "status": "command_error", "passed": False, "reasonCode": "missing_head_commit", "summary": "Unable to resolve HEAD"})
    if not target_paths:
        return finalize({**common, "status": "passed", "passed": True, "reasonCode": "no_changed_python_targets", "summary": "Python enforcement passed; no changed production Python files"})
    if args.dry_run:
        return finalize({**common, "status": "passed", "passed": True, "reasonCode": "dry_run", "summary": "Lightweight Python enforcement dry run passed"})
    return None


#: The binding reports only these at the top level; anything more specific has
#: to come from the target that actually failed.
_GENERIC_REASONS = {"", "passed", "quality_gate_failed", "failed"}
_REASON_ALIASES = {"no_targets": "no_changed_python_targets"}


def _diagnosis(
    evidence: Mapping[str, Any],
    target_results: Sequence[Mapping[str, Any]],
    *,
    passed: bool,
) -> tuple[str, str]:
    top = _REASON_ALIASES.get(str(evidence.get("reasonCode") or ""), str(evidence.get("reasonCode") or ""))
    if passed:
        return (top or "passed"), "Python enforcement passed"
    for target in target_results:
        reason = str(target.get("reasonCode") or "")
        if reason and reason != "passed":
            message = str(target.get("message") or "")
            return reason, message or f"Python enforcement failed: {reason}"
    reason = top if top not in _GENERIC_REASONS else "quality_gate_failed"
    return reason, f"Python enforcement failed: {reason}"


def resolve_targets(repo: Path, explicit_targets: Sequence[str], base_ref: str) -> list[str]:
    if explicit_targets:
        return [target_source_path(value) for value in explicit_targets]
    return changed_production_python_files(repo, base_ref)
