"""Application-side view of the CI language-handler seam.

The protocols and the registry are defined once, in :mod:`uta.enforcement.ci`,
because the concrete handlers under ``uta.language.java`` and
``uta.language.python`` subclass ``BaseCiLanguageHandler`` and must not import
the application layer that composes them. This module is an explicit re-export
so ``uta.app`` composition keeps its own import path without declaring a second
``EnforcementRunner`` protocol of the same name.
"""

from __future__ import annotations

from uta.enforcement.ci import (
    BaseCiLanguageHandler,
    CiLanguageHandler,
    CiLanguageHandlerRegistry,
    EnforcementRunner,
)

__all__ = [
    "BaseCiLanguageHandler",
    "CiLanguageHandler",
    "CiLanguageHandlerRegistry",
    "EnforcementRunner",
]
