"""UTA-managed Python test environments selected by application identity.

Recipes are trusted deployment configuration, not target-repository input.
They let CI enforcement and repair use the same cached interpreter without
putting application branches in the language-agnostic task workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Any, Dict, Mapping, Optional


PYTHON_ENVIRONMENT_RECIPE_SNAPSHOT_KEY = "python_environment_recipe"


@dataclass(frozen=True)
class PythonEnvironmentRecipe:
    base_python: str
    venv: str
    packages: tuple[str, ...]
    profile: str
    ci_mutation_max_selected: int | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PythonEnvironmentRecipe":
        base_python = str(value.get("python") or value.get("base_python") or "").strip()
        venv = str(value.get("venv") or "").strip()
        profile = str(value.get("profile") or "").strip()
        packages = tuple(str(item).strip() for item in value.get("packages") or () if str(item).strip())
        raw_ci_cap = value.get("ci_mutation_max_selected")
        ci_cap = None if raw_ci_cap in (None, "") else int(raw_ci_cap)
        if ci_cap is not None and ci_cap <= 0:
            raise ValueError("Python environment recipe CI mutation cap must be positive")
        if not base_python or not venv or not profile or not packages:
            raise ValueError(
                "Python environment recipe requires python, venv, profile, and non-empty packages"
            )
        return cls(
            base_python=base_python,
            venv=venv,
            packages=packages,
            profile=profile,
            ci_mutation_max_selected=ci_cap,
        )

    @property
    def python_bin(self) -> str:
        return (Path(self.venv).expanduser() / "bin" / "python").as_posix()

    @property
    def mutmut_bin(self) -> str:
        return (Path(self.venv).expanduser() / "bin" / "mutmut").as_posix()

    @property
    def environment_profile(self) -> str:
        return self.profile

    @property
    def setup_command(self) -> tuple[str, ...]:
        command = [
            self.base_python,
            "-m",
            "uta.language.python.environment_bootstrap",
            "--venv",
            self.venv,
            "--python",
            self.base_python,
        ]
        for package in self.packages:
            command.extend(("--package", package))
        return tuple(command)

    def runtime_overrides(self) -> Dict[str, Any]:
        overrides = {
            "python_bin": self.python_bin,
            "mutmut_bin": self.mutmut_bin,
            "setup_command": self.setup_command,
            "dependency_overlay_enabled": False,
            "environment_profile": self.environment_profile,
        }
        if self.ci_mutation_max_selected is not None:
            overrides["ci_mutation_max_selected"] = self.ci_mutation_max_selected
        return overrides

    def environment(self) -> Dict[str, str]:
        return {
            "UTA_PYTHON_BIN": self.python_bin,
            "UTA_PYTHON_MUTMUT_BIN": self.mutmut_bin,
            "UTA_PYTHON_SETUP_COMMAND": shlex.join(self.setup_command),
            "UTA_PYTHON_DEPENDENCY_OVERLAY_ENABLED": "0",
            "UTA_PYTHON_ENVIRONMENT_PROFILE": self.environment_profile,
        }

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "python": self.base_python,
            "venv": self.venv,
            "packages": list(self.packages),
            "profile": self.profile,
        }
        if self.ci_mutation_max_selected is not None:
            payload["ci_mutation_max_selected"] = self.ci_mutation_max_selected
        return payload


class PythonEnvironmentRecipeResolver:
    """Resolve an exact app-name mapping into a managed Python environment."""

    def __init__(self, recipes: Optional[Mapping[str, Mapping[str, Any]]] = None) -> None:
        self._recipes = {
            str(app_name).strip(): PythonEnvironmentRecipe.from_dict(recipe)
            for app_name, recipe in (recipes or {}).items()
            if str(app_name).strip()
        }

    def resolve(self, app_name: str) -> Optional[PythonEnvironmentRecipe]:
        return self._recipes.get(str(app_name or "").strip())


def apply_python_environment_recipe(
    recipe: PythonEnvironmentRecipe,
    *,
    environ: Mapping[str, str],
) -> Dict[str, str]:
    selected = dict(environ)
    selected.update(recipe.environment())
    return selected


__all__ = [
    "PYTHON_ENVIRONMENT_RECIPE_SNAPSHOT_KEY",
    "PythonEnvironmentRecipe",
    "PythonEnvironmentRecipeResolver",
    "apply_python_environment_recipe",
]
