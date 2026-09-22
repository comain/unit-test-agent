"""Clearing what a previous run left in `target/`.

Only needed when one enforcement invocation is followed by another over the same
checkout. JaCoCo's agent appends to `jacoco.exec`, and Surefire's reports stay on
disk, so a second run that inherits both would report the first run's coverage
and the first run's failures. The deletion itself already exists for the
generation path; this module reuses it per Maven module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from uta.language.java.maven.jacoco import _clear_jacoco_artifacts


def _clear_module_test_artifacts(repo_path: Path, modules: Sequence[str]) -> None:
    bases = [Path(repo_path)]
    for module in modules or []:
        module_base = Path(repo_path) / module
        if module_base != bases[0]:
            bases.append(module_base)
    for module_base in bases:
        if not module_base.is_dir():
            continue
        _clear_jacoco_artifacts(module_base, module_base / "target" / "jacoco.exec")


__all__ = ["_clear_module_test_artifacts"]
