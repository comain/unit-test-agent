"""Language-aware enforcement CLI commands."""

from __future__ import annotations

import json
import os
from pathlib import Path

import click

from uta.shared.config import settings


def register_enforcement_commands(
    main,
    *,
    console,
    normalize_cli_targets,
    resolve_cli_language,
) -> None:
    """Attach enforcement and mutation-evidence commands to main."""
    _normalize_cli_targets = normalize_cli_targets
    _resolve_cli_language = resolve_cli_language

    @main.command("python-mutant-diffs")
    @click.option("--repo", default=".", type=click.Path(exists=True), show_default=True, help="Path to the Python repository")
    @click.option("--target", required=True, help="Python target path or path.py::symbol")
    @click.option("--survivors", "survivors_path", default=None, type=click.Path(dir_okay=False), help="mutmut survivors.json path")
    @click.option("--test-path", "test_paths", multiple=True, help="Focused pytest path used by mutmut runner")
    @click.option("--mutmut-bin", default="mutmut", show_default=True, help="mutmut executable")
    @click.option("--limit-per-symbol", default=3, show_default=True, type=int, help="Representative mutmut show diffs per symbol")
    @click.option("--attempt", default=1, show_default=True, type=int, help="Repair attempt used to select the split mutation group")
    @click.option("--json-output", is_flag=True, help="Print machine-readable JSON")
    def python_mutant_diffs(repo, target, survivors_path, test_paths, mutmut_bin, limit_per_symbol, attempt, json_output):
        """Precompute grouped Python mutmut survivor diffs for mutation repair."""
        from uta.shared.languages import RawTargetSelection, default_registry
        from uta.language.python.mutation_context import build_python_mutation_repair_context
        from uta.language.python.verification.runner import (
            CommandEvidence,
            MutationSummary,
            PythonVerificationResult,
        )
        from uta.language.python.mutation_context import load_python_survivors

        repo_path = Path(repo).expanduser().resolve()
        target_ref = default_registry().adapter_for("python").normalize_target(RawTargetSelection(target=target))
        source_path = target_ref.source_path or target_ref.display_name.split("::", 1)[0]
        survivors = load_python_survivors(repo_path, survivors_path)
        effective_test_paths = list(test_paths or ["tests"])
        command = [mutmut_bin, "run", "--paths-to-mutate", source_path, "--tests-dir", ",".join(effective_test_paths)]
        verification = PythonVerificationResult(
            status="failed",
            reason_code="mutation_gate_failed",
            tests_pass=True,
            mutation=MutationSummary(
                runtime_lane="mutmut-modern",
                generated=len(survivors),
                killed=0,
                survived=len(survivors),
                no_coverage=0,
                rate=0.0,
                gate=100.0,
                passed=False,
                survivors=survivors,
            ),
            commands=[CommandEvidence(name="mutmut_run", command=command, exit_code=1)],
        )
        context = build_python_mutation_repair_context(
            repo=repo_path,
            target_id=target_ref.target_id,
            source_path=source_path,
            test_paths=effective_test_paths,
            verification=verification,
            mutmut_bin=mutmut_bin,
            limit_per_symbol=limit_per_symbol,
            repair_attempt=attempt,
        )
        payload = {
            "artifact": context.artifact_path,
            "groups": [
                {"symbol": group.symbol, "count": group.count, "lines": list(group.lines)}
                for group in context.groups
            ],
            "groupArtifacts": dict(context.group_artifact_paths),
            "reproduceCommand": context.reproduce_command,
        }
        if json_output:
            click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return
        console.print(f"Mutation repair context: {context.artifact_path}")
        if context.group_artifact_paths:
            console.print("Group contexts:")
            for symbol, path in context.group_artifact_paths.items():
                console.print(f"- {symbol}: {path}")


    @main.command("enforce")
    @click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
    @click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
    @click.option("--target", "targets", multiple=True, help="Language-neutral target. For Python use path.py or path.py::symbol.")
    @click.option("--backend", default=None, help="Enforcement backend name")
    @click.option("--test-path", "test_paths", multiple=True, help="Test path to run for Python enforcement. Repeat for multiple paths.")
    @click.option("--base-ref", default="origin/master", show_default=True, help="Git base ref for incremental enforcement")
    @click.option("--coverage-gate", default=settings.coverage_gate, type=float, help="Target coverage percentage")
    @click.option("--mutation-gate", default=settings.mutation_gate, type=float, help="Target mutation score")
    @click.option("--syntax-version", default="python3", show_default=True, help="Python syntax/runtime lane: python3 or python2")
    @click.option("--evidence-output", default=None, type=click.Path(dir_okay=False), help="Optional JSON evidence output file")
    @click.option("--dev-skills-launcher-version", default=None, hidden=True)
    @click.option("--dry-run", is_flag=True, help="Resolve language and targets without running the gate")
    @click.option("--json-output", is_flag=True, help="Print machine-readable JSON")
    def enforce(repo, language, targets, backend, test_paths, base_ref, coverage_gate, mutation_gate, syntax_version, evidence_output, dev_skills_launcher_version, dry_run, json_output):
        """Resolve a language-aware enforcement request."""
        decision = _resolve_cli_language(repo, language, targets=targets)
        normalized = _normalize_cli_targets(decision.language, targets)
        payload = {
            "language": decision.language,
            "backend": backend or ("maven_enforcer" if decision.language == "java" else f"{decision.language}_enforcer"),
            "targets": [target.as_selection() for target in normalized],
            "languageDecision": decision.as_dict(),
            "dryRun": bool(dry_run),
        }
        if dry_run:
            if json_output:
                click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                console.print(f"[green]Resolved {payload['backend']} for language={decision.language}[/green]")
            return
        if decision.language == "python" and payload["backend"] == "python_enforcer":
            from uta.language.python.enforcement import (
                ci_mutation_sampling_requested,
                format_evidence_markers,
                run_python_enforcement,
            )

            evidence = run_python_enforcement(
                repo_path=Path(repo),
                target_values=list(targets),
                test_paths=list(test_paths),
                base_ref=base_ref,
                coverage_gate=coverage_gate,
                mutation_gate=mutation_gate,
                syntax_version=syntax_version,
                dev_skills_launcher_version=dev_skills_launcher_version,
                # The CI adapter runs this in a subprocess for memory and
                # process-group isolation, and the mutation profile has to
                # survive that boundary or CI silently enforces the local
                # (uncapped) profile.
                enable_ci_mutation_sampling=ci_mutation_sampling_requested(os.environ),
            )
            if evidence_output:
                Path(evidence_output).expanduser().write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
            if json_output:
                click.echo(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            else:
                click.echo(format_evidence_markers(evidence), nl=False)
            if not evidence.get("passed"):
                raise click.exceptions.Exit(1)
            return
        if decision.language == "java" and payload["backend"] == "maven_enforcer":
            # Through the contract, not around it: this is the caller that makes
            # the registry and `enforce()` load-bearing rather than ornamental.
            # The Java binding delegates to the same `run_java_enforcement` this
            # branch used to call directly, so the evidence is unchanged.
            from uta.app.enforcement_composition import build_enforcement_registry
            from uta.language.java.enforcement import format_evidence_markers
            from uta_enforce_core.commands import SafeProcessRunner
            from uta_enforce_core.contracts import (
                EnforcementInvocationContext,
                EnforcementRequest,
                EnforcementTarget,
                QualityGates,
                RuntimeSelection,
            )
            from uta_enforce_core.dispatch import enforce as dispatch_enforcement

            request = EnforcementRequest(
                repo_path=Path(repo),
                language="java",
                targets=tuple(
                    EnforcementTarget(
                        language="java",
                        target_id=target.target_id,
                        source_path=getattr(target, "source_path", "") or "",
                    )
                    for target in normalized
                ),
                base_ref=base_ref,
                quality_gates=QualityGates(
                    diff_coverage_min=float(coverage_gate) / 100.0,
                    diff_mutation_min=float(mutation_gate) / 100.0,
                ),
                runtime=RuntimeSelection(
                    timeout_seconds=int(settings.ci_enforcement_timeout_seconds or 1800),
                ),
            )
            result = dispatch_enforcement(
                request,
                registry=build_enforcement_registry(),
                context=EnforcementInvocationContext(run_command=SafeProcessRunner()),
            )
            evidence = dict(result.evidence)
            if evidence_output:
                Path(evidence_output).expanduser().write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
            if json_output:
                click.echo(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            else:
                click.echo(format_evidence_markers(evidence), nl=False)
            if not evidence.get("passed"):
                raise click.exceptions.Exit(1)
            return
        raise click.ClickException(
            f"Language-aware enforcement routing is registered for language={decision.language}; "
            "gate execution is implemented in the enforcement phase."
        )


    @main.command("python-enforce")
    @click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the Python repository")
    @click.option("--target", "targets", multiple=True, help="Python target path.py or path.py::symbol")
    @click.option("--test-path", "test_paths", multiple=True, help="Test path to run. Repeat for multiple paths.")
    @click.option("--base-ref", default="origin/master", show_default=True, help="Git base ref for incremental enforcement")
    @click.option("--coverage-gate", default=settings.coverage_gate, type=float, help="Target coverage percentage")
    @click.option("--mutation-gate", default=settings.mutation_gate, type=float, help="Target mutation score")
    @click.option("--syntax-version", default="python3", show_default=True, help="Python syntax/runtime lane: python3 or python2")
    @click.option("--evidence-output", default=None, type=click.Path(dir_okay=False), help="Optional JSON evidence output file")
    @click.option("--dev-skills-launcher-version", default=None, hidden=True)
    @click.option("--dry-run", is_flag=True, help="Resolve language and targets without running the gate")
    @click.option("--json-output", is_flag=True, help="Print machine-readable JSON")
    def python_enforce(repo, targets, test_paths, base_ref, coverage_gate, mutation_gate, syntax_version, evidence_output, dev_skills_launcher_version, dry_run, json_output):
        """Compatibility alias for Python enforcement routing."""
        return enforce.callback(
            repo=repo,
            language="python",
            targets=targets,
            backend="python_enforcer",
            test_paths=test_paths,
            base_ref=base_ref,
            coverage_gate=coverage_gate,
            mutation_gate=mutation_gate,
            syntax_version=syntax_version,
            evidence_output=evidence_output,
            dev_skills_launcher_version=dev_skills_launcher_version,
            dry_run=dry_run,
            json_output=json_output,
        )





__all__ = ["register_enforcement_commands"]
