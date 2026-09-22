"""Language-neutral repair classification.

A failed verification maps to a repair *kind*; the kind selects which repair
prompt a backend uses, resolved from that backend's ``PromptBundle``. Centralizing
this is the first shared step of workflow convergence: today each backend hand-maps
its own reason codes to its own prompt-name literals, which both duplicates the
bundle and lets the two drift. Backends now classify into a ``RepairKind`` and let
the engine pick the prompt from the adapter's bundle.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from uta.shared.languages import PromptBundle


class RepairKind(str, Enum):
    """Why a generated test needs another repair turn.

    ``compile`` covers compile/syntax/runtime/test-execution failures (anything
    that is not a coverage- or mutation-gate shortfall); ``coverage`` and
    ``mutation`` are the two gate shortfalls.
    """

    compile = "compile"
    coverage = "coverage"
    mutation = "mutation"


def repair_prompt_for(bundle: PromptBundle, kind: RepairKind) -> Optional[str]:
    """Return the prompt name a backend uses for one repair kind, or None."""
    if kind is RepairKind.coverage:
        return bundle.fix_coverage
    if kind is RepairKind.mutation:
        return bundle.fix_mutations
    return bundle.fix_compile
