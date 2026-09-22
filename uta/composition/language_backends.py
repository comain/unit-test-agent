"""The language backend table, and the one place that knows it.

This lived in `uta/shared/backends.py`, whose docstring argued the entries
were strings so "the static graph is acyclic". That was true of the graph and
false of the program: fourteen real edges from the lowest layer into
`uta.language` were simply invisible. Once the dependency scanner learned to
read the table, they stopped being invisible and the claim stopped being
true of the graph either.

The edge mattered more than it looked. `uta.shared` had exactly one outbound
edge to any other `uta` package, and it was this table; dropping it collapses
the recorded five-package cycle -- enforcement, language, shared, tasks,
testgen -- to two. So `uta.shared` keeps the mechanism and loses the
knowledge, and the knowledge lives here, above everything it names.

Registration is explicit and happens at each entrypoint. `make_backend`
raises when nothing has registered, so a missed call fails at first use with
a message naming what *is* registered, rather than silently taking a default.
"""

from __future__ import annotations

from uta.shared.backends import register_backend


#: ``(language, role) -> "module.path:AttributeName"``.
#:
#: Strings rather than imports, so registering a backend does not import every
#: language's toolchain -- a Java run must not load the Python mutation stack.
#: The scanner reads these as lazy edges, which is what they are.
LANGUAGE_BACKENDS: dict[tuple[str, str], str] = {
    ("java", "adapter"): "uta.language.java.adapter:JavaLanguageAdapter",
    ("python", "adapter"): "uta.language.python.adapter:PythonLanguageAdapter",
    ("java", "parse"): "uta.language.java.parse:JavaParseProvider",
    ("python", "parse"): "uta.language.python.parse:PythonParseProvider",
    ("java", "scoring"): "uta.language.java.scoring:JavaTargetScorer",
    ("python", "scoring"): "uta.language.python.scoring:PythonTargetScorer",
    ("java", "context_factory"): "uta.language.java.context:JavaContextProviderFactory",
    ("python", "context_factory"): "uta.language.python.context:PythonContextProviderFactory",
    ("java", "validation"): "uta.language.java.validation:JavaMarkdownPlanContextExtractor",
    ("python", "validation"): "uta.language.python.validation:PythonPayloadPlanContextExtractor",
    ("java", "project_summary_factory"): "uta.language.java.project_summary:JavaProjectSummaryProviderFactory",
    ("python", "project_summary_factory"): "uta.language.python.project_summary:PythonProjectSummaryProviderFactory",
    ("java", "enforcement_run"): "uta.language.java.enforcement:run_java_enforcement",
    ("java", "output_evidence"): "uta.language.java.ci_evidence:JavaRawOutputEvidence",
    ("java", "coverage_recompute"): "uta.language.java.coverage_recompute:JavaProjectCoverageRecomputer",
    ("java", "workspace_policy"): "uta.language.java.workspace:JavaWorkspacePolicy",
    ("python", "workspace_policy"): "uta.language.python.workspace:PythonWorkspacePolicy",
    ("java", "batch_generator"): "uta.language.java.batch:JavaBatchGenerator",
    ("python", "batch_generator"): "uta.language.python.generation:PythonBatchGenerator",
    ("java", "generation_backend"): "uta.language.java.generation_backend",
    ("python", "generation_backend"): "uta.language.python.generation_backend",
}


def register_language_backends() -> None:
    """Install the built-in backends. Idempotent; called at every entrypoint."""
    for (language, role), target in LANGUAGE_BACKENDS.items():
        register_backend(language, role, target)


__all__ = ["LANGUAGE_BACKENDS", "register_language_backends"]
