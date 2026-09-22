"""Construction of the collaborators `ApiTriggerService` runs on.

The service is the application's composition root for CI checks, and a
composition root grows: every new protocol, language handler or enforcement
binding adds another constructor call to `__init__`. That growth is what
pushed `service.py` past the 600-line guardrail while the *service* -- submit,
claim, run, report -- had not itself grown.

So the two are separated by what changes them. `service.py` holds behaviour:
the CI check lifecycle and the record it keeps. This module holds the wiring
that behaviour runs on -- which language handlers exist by default, which
protocol registry is used when the caller supplies none, and the neutral
context provider used for records whose protocol was never registered.

Nothing here is a new decision. The defaults are exactly the ones `__init__`
used to inline; naming them means a reader can see the whole default
composition in one place, and a test can build one piece of it without
constructing a service.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from uta.app.ci import CiLanguageHandler, CiLanguageHandlerRegistry, EnforcementRunner
from uta.app.context import assemble_base_context
from uta.app.protocols import CiContextProvider, ProtocolRegistry
from uta.shared.ci_models import CiTaskRecord


class ProtocolNeutralContextProvider(CiContextProvider):
    """Fallback context provider for records without a registered protocol."""

    name = "manual"

    def build_context(
        self,
        record: CiTaskRecord,
        *,
        user_context: Optional[str] = None,
        commit_messages: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        return assemble_base_context(
            record,
            issue={"id": record.request.jira_id, "description": None, "source": None, "kind": "issue"},
            user_context=user_context,
            commit_messages=commit_messages,
        )


def build_language_handler_registry(
    *,
    config: Optional[Any] = None,
    language_handlers: Optional[Iterable[CiLanguageHandler]] = None,
    enforcement_runner: Optional[EnforcementRunner] = None,
    python_enforcement_runner: Optional[EnforcementRunner] = None,
) -> CiLanguageHandlerRegistry:
    """The registry the service runs on: caller-supplied handlers, or the defaults.

    The Java and Python handlers are imported here rather than at module import
    so that selecting a backend stays a decision of this composition step, and
    a caller injecting its own handlers never pays for either toolchain.
    """
    if language_handlers is not None:
        return CiLanguageHandlerRegistry(language_handlers)

    from uta.language.java.ci import JavaCiLanguageHandler
    from uta.language.python.ci import PythonCiLanguageHandler

    java_runner = enforcement_runner
    python_runner = python_enforcement_runner
    if config is not None:
        from uta.language.java.enforcement_runner import MavenEnforcementRunner
        from uta.language.python.enforcement_runner import PythonEnforcementRunner
        from uta.tasks.hanging_test_quarantine import SQLiteHangingTestQuarantine

        java_runner = java_runner or MavenEnforcementRunner(
            command=config.ci_enforcement_command,
            timeout_seconds=config.ci_enforcement_timeout_seconds,
            java_home=config.daemon_java_home,
            coverage_gate=config.ci_diff_coverage_gate,
            mutation_gate=config.ci_diff_mutation_gate,
            maven_central_mirror_url=config.maven_central_mirror_url,
            # CI is the gate: run the plugin over the changed modules and take
            # its verdict. Test generation and repair build their own runners
            # and stay in targeted mode.
            full_run=True,
            exclude_unrelated_failing_tests=config.ci_failing_test_exclusion_enabled,
            stall_detection_enabled=config.ci_enforcement_stall_detection_enabled,
            stall_seconds=config.ci_enforcement_stall_seconds,
            stall_retries=config.ci_enforcement_stall_retries,
            hanging_test_quarantine_enabled=config.ci_hanging_test_quarantine_enabled,
            quarantine_ttl_days=config.ci_hanging_test_quarantine_ttl_days,
            quarantine_store=(
                SQLiteHangingTestQuarantine(config.ci_task_db_path)
                if config.ci_hanging_test_quarantine_enabled
                else None
            ),
        )
        python_runner = python_runner or PythonEnforcementRunner(
            command=config.ci_python_enforcement_command,
            timeout_seconds=config.ci_python_enforcement_timeout_seconds,
            coverage_gate=config.ci_diff_coverage_gate,
            mutation_gate=config.ci_python_diff_mutation_gate,
            memory_limit_mb=config.ci_python_enforcement_memory_limit_mb,
        )

    # The resolver is what makes `repository_java_homes` mean anything: it
    # rebinds the Java handler's runner per repository and records the chosen
    # JDK in the task's config snapshot. Without it the setting parses, the
    # resolver class exists, and nothing ever consults either -- a repository
    # pinned to JDK 25 quietly runs on the daemon default.
    java_runtime_resolver = None
    python_runtime_resolver = None
    if config is not None:
        from uta.language.java.runtime import RepositoryJavaRuntimeResolver

        java_runtime_resolver = RepositoryJavaRuntimeResolver(
            config.daemon_java_home,
            config.repository_java_homes,
        )
        from uta.language.python.environment_recipes import PythonEnvironmentRecipeResolver

        python_runtime_resolver = PythonEnvironmentRecipeResolver(
            config.python_environment_recipes
        )

    return CiLanguageHandlerRegistry(
        (
            JavaCiLanguageHandler(java_runner, java_runtime_resolver),
            PythonCiLanguageHandler(python_runner, python_runtime_resolver),
        )
    )


def build_protocol_registry(protocols: Optional[ProtocolRegistry] = None) -> ProtocolRegistry:
    """Protocol registry drives inbound parsing, result reporting, and the
    repair-context provider. Concrete adapters are provided by app wiring or
    tests; the service core stays protocol-neutral, so the default is empty
    rather than a guess about which protocol a deployment speaks.
    """
    return protocols if protocols is not None else ProtocolRegistry()


__all__ = [
    "ProtocolNeutralContextProvider",
    "build_language_handler_registry",
    "build_protocol_registry",
]
