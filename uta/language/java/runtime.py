"""Repository-scoped Java runtime selection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


class RepositoryJavaRuntimeResolver:
    """Resolve an exact repository identity to a validated Java environment."""

    def __init__(
        self,
        default_java_home: str,
        repository_java_homes: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.default_java_home = str(default_java_home or "").strip()
        self.repository_java_homes = {
            str(repository).strip(): str(java_home).strip()
            for repository, java_home in (repository_java_homes or {}).items()
            if str(repository).strip()
        }

    def resolve(self, repository: str) -> str:
        identity = str(repository or "").strip()
        return self.repository_java_homes.get(identity, self.default_java_home)

    def environment_for(
        self,
        repository: str,
        *,
        environ: Optional[Mapping[str, str]] = None,
        java_home: Optional[str] = None,
    ) -> dict[str, str]:
        identity = str(repository or "").strip()
        selected = str(java_home if java_home is not None else self.resolve(identity)).strip()
        env = dict(os.environ if environ is None else environ)
        if not selected:
            return env

        java = Path(selected) / "bin" / "java"
        if not java.is_file() or not os.access(java, os.X_OK):
            label = identity or "configured Java runtime"
            raise RuntimeError(f"Java home for {label} does not contain executable bin/java: {selected}")

        selected_bin = str(Path(selected) / "bin")
        known_java_bins = {
            str(Path(home) / "bin")
            for home in [self.default_java_home, *self.repository_java_homes.values(), env.get("JAVA_HOME", "")]
            if home
        }
        path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
        env["JAVA_HOME"] = selected
        env["PATH"] = os.pathsep.join(
            [selected_bin, *[entry for entry in path_entries if entry not in known_java_bins]]
        )
        return env
