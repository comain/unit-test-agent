"""Java enforcement binding implementing the sole neutral enforcement contract."""

from __future__ import annotations

from pathlib import Path

from uta_enforce_core.contracts import (
    EnforcementCapabilities,
    EnforcementInvocationContext,
    EnforcementRequest,
    EnforcementResult,
    EnforcementStatus,
    TargetEnforcementResult,
)
from uta.shared.config import settings


def run_java_enforcement(*args, **kwargs):
    """Run Java enforcement, resolved rather than imported.

    Importing `uta.language.java` here was the last edge holding the
    enforcement/language cycle together: a binding under `uta.enforcement`
    reaching into `uta.language`, while thirteen edges correctly ran the other
    way. Composition already knows where the Java implementation lives, so the
    binding asks instead of knowing -- which is what a binding is for.

    Kept as a module-level function on purpose: tests replace this name on the
    module, and `enforce` looks it up through `sys.modules` so they still can.
    """
    from uta.shared.backends import backend_class

    return backend_class("java", "enforcement_run")(*args, **kwargs)


class JavaEnforcementBinding:
    """Java language enforcement binding for Maven-based test suites."""

    language = "java"

    def capabilities(self) -> EnforcementCapabilities:
        return EnforcementCapabilities(
            language="java",
            supports_coverage=True,
            supports_mutation=True,
            supported_runtime_versions=("8", "11", "17", "21"),
            accepts_sampling_policy=False,
        )

    def enforce(
        self,
        request: EnforcementRequest,
        context: EnforcementInvocationContext,
    ) -> EnforcementResult:
        repo = Path(request.repo_path).resolve()
        command = getattr(settings, "ci_enforcement_command", "mvn verify")
        preserve_scope = False
        for target in request.targets:
            if target.metadata:
                if target.metadata.get("command"):
                    command = str(target.metadata["command"])
                if "preserve_explicit_target_scope" in target.metadata:
                    preserve_scope = bool(target.metadata["preserve_explicit_target_scope"])
        timeout = request.runtime.timeout_seconds if request.runtime else 1800

        # Run java enforcement using the injected runner adapter
        def runner_adapter(cmd, cwd=None, timeout=None, env=None, **kwargs):
            return context.run_command.run(
                cmd,
                cwd=cwd or repo,
                env=env,
                timeout_seconds=timeout,
                is_cancelled=context.is_cancelled,
            )

        import sys

        binding_mod = sys.modules.get("uta.enforcement.bindings.java.binding")
        runner_fn = binding_mod.run_java_enforcement if binding_mod and hasattr(binding_mod, "run_java_enforcement") else run_java_enforcement

        evidence = runner_fn(
            repo_path=repo,
            command=command,
            base_ref=request.base_ref,
            timeout_seconds=timeout,
            run_command=runner_adapter,
            maven_central_mirror_url=getattr(settings, "maven_central_mirror_url", ""),
            preserve_explicit_target_scope=preserve_scope,
        )

        passed = bool(evidence.get("passed", False))
        status = EnforcementStatus.PASSED if passed else EnforcementStatus.FAILED
        if evidence.get("status") == "timeout":
            status = EnforcementStatus.ERROR

        target_results: list[TargetEnforcementResult] = []
        for target in request.targets:
            target_results.append(
                TargetEnforcementResult(
                    target=target,
                    status=status,
                    evidence=evidence,
                )
            )

        return EnforcementResult(
            status=status,
            evidence=evidence,
            target_results=tuple(target_results),
        )


def create_java_enforcement_binding() -> JavaEnforcementBinding:
    """Factory creating the Java language enforcement binding."""
    return JavaEnforcementBinding()
