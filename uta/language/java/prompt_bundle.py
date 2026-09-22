"""The Java prompt-name bundle, owned apart from the adapter that publishes it.

The bundle is four prompt names and a language tag -- data, with no Java
detection, workspace, or backend behaviour attached. It used to be reachable
only by constructing ``JavaLanguageAdapter``, which made every phase that
needed a prompt name depend on the composing adapter. Keeping the data here
lets the composition root inject it downward into the phase ports instead.
"""

from __future__ import annotations

from uta.shared.languages import PromptBundle


def java_prompt_bundle() -> PromptBundle:
    """Return the Java prompt bundle.

    A fresh instance per call, matching what ``JavaLanguageAdapter`` returned
    before this moved, so no caller can mutate a shared one.
    """
    return PromptBundle(
        language="java",
        plan="plan_tests",
        generate="generate_test",
        fix_compile="fix_compile",
        fix_coverage="fix_coverage",
        fix_mutations="fix_mutations",
    )


__all__ = ["java_prompt_bundle"]
