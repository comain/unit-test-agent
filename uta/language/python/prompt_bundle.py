"""The Python prompt-name bundle, owned apart from the adapter that publishes it.

Mirrors `uta/language/java/prompt_bundle.py`: the bundle is four prompt names
and a language tag -- data, with no Python detection, workspace, or backend
behaviour attached -- so a caller that needs a prompt name does not have to
construct the composing adapter to reach it.
"""

from __future__ import annotations

from uta.shared.languages import PromptBundle


def python_prompt_bundle() -> PromptBundle:
    """Return the Python prompt bundle.

    A fresh instance per call, matching what ``PythonLanguageAdapter`` returned
    before this moved, so no caller can mutate a shared one. Python has no plan
    stage, so ``plan`` stays unset.
    """
    return PromptBundle(
        language="python",
        generate="python_generate_test",
        fix_compile="python_fix_compile",
        fix_coverage="python_fix_coverage",
        fix_mutations="python_fix_mutations",
    )


__all__ = ["python_prompt_bundle"]
