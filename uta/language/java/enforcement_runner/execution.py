"""Process-level concerns for one enforcement invocation.

The mirror settings file and the JAVA_HOME-adjusted environment are about how
Maven is launched, not about what it is asked to enforce. Both are pure
functions of configuration, so the runner can build them without holding a
process open.
"""

from __future__ import annotations

from html import escape
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence
import xml.etree.ElementTree as ET


_EARLY_ARGLINE = "${argLine}"
_LATE_ARGLINE = "@{argLine}"


def _with_jacoco_argline_bridge(cmd: Sequence[str], repo_path: Path) -> List[str]:
    """Keep JaCoCo attached when a project expands Surefire argLine too early.

    JaCoCo's prepare-agent goal writes the ``argLine`` Maven property during
    the lifecycle. A POM that embeds ``${argLine}`` expands it while Maven
    builds the model, before that goal runs; an additional javaagent then
    starts normally while JaCoCo silently disappears. UTA supplies the JaCoCo
    agent as a user property for that legacy shape, leaving the POM's agents in
    place. The relative destination is intentional: each Surefire fork writes
    to its own module's ``target`` directory in a reactor build.
    """
    updated = list(cmd)
    if "verify" not in updated or not any(
        item == "-Dtest.enforcement.enabled=true" for item in updated
    ):
        return updated
    if any(item.startswith("-DargLine=") for item in updated):
        return updated
    if not _reactor_uses_early_argline(repo_path):
        return updated

    agent = _resolve_jacoco_agent(updated)
    if agent is None:
        return updated
    updated.append(
        f"-DargLine=-javaagent:{agent}=destfile=target/jacoco.exec,append=true"
    )
    return updated


def _reactor_uses_early_argline(repo_path: Path) -> bool:
    for pom in Path(repo_path).rglob("pom.xml"):
        if any(part in {".git", "target"} for part in pom.relative_to(repo_path).parts):
            continue
        try:
            root = ET.parse(pom).getroot()
        except (ET.ParseError, OSError):
            continue
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] != "argLine":
                continue
            value = "".join(element.itertext())
            if _EARLY_ARGLINE in value and _LATE_ARGLINE not in value:
                return True
    return False


def _resolve_jacoco_agent(cmd: Sequence[str]) -> Optional[Path]:
    repositories: List[Path] = []
    prefix = "-Dmaven.repo.local="
    repositories.extend(
        Path(item[len(prefix):]).expanduser()
        for item in cmd
        if item.startswith(prefix) and item[len(prefix):]
    )
    home = Path.home()
    repositories.extend([home / "internal_repository", home / ".m2/repository"])

    for repository in repositories:
        candidates = list(
            repository.glob(
                "org/jacoco/org.jacoco.agent/*/org.jacoco.agent-*-runtime.jar"
            )
        )
        if candidates:
            return max(candidates, key=_jacoco_agent_version_key).resolve()
    return None


def _jacoco_agent_version_key(path: Path) -> tuple[int, ...]:
    match = re.search(r"org\.jacoco\.agent-([0-9.]+)-runtime\.jar$", path.name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _with_maven_central_mirror(
    cmd: Sequence[str], repo_path: Path, maven_central_mirror_url: str
) -> List[str]:
    """Route Central through the configured corporate mirror without copying credentials."""
    if not maven_central_mirror_url:
        return list(cmd)

    settings_dir = Path(repo_path) / ".uta_cache" / "maven"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "central-mirror-global-settings.xml"
    settings_path.write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">',
                "  <mirrors>",
                "    <mirror>",
                "      <id>uta-central-mirror</id>",
                "      <name>UTA Maven Central mirror</name>",
                f"      <url>{escape(maven_central_mirror_url)}</url>",
                "      <mirrorOf>central</mirrorOf>",
                "    </mirror>",
                "  </mirrors>",
                "</settings>",
                "",
            ]
        ),
        encoding="utf-8",
    )

    updated: List[str] = []
    skip_next = False
    for item in cmd:
        if skip_next:
            skip_next = False
            continue
        if item in {"-gs", "--global-settings"}:
            skip_next = True
            continue
        if item.startswith("--global-settings="):
            continue
        updated.append(item)
    updated.extend(["-gs", str(settings_path)])
    return updated


def _command_env(java_home: str) -> Optional[Dict[str, str]]:
    """The Java environment one Maven enforcement run should see.

    Delegates to `RepositoryJavaRuntimeResolver` so there is one place that
    knows how a Java home becomes an environment. It also validates that the
    home actually contains an executable `bin/java`, which the previous
    inline version did not -- a mistyped path used to surface as a confusing
    Maven failure much later.
    """
    if not java_home:
        return None
    from uta.language.java.runtime import RepositoryJavaRuntimeResolver

    return RepositoryJavaRuntimeResolver(java_home).environment_for("Maven enforcement")


__all__ = [
    "_command_env",
    "_with_jacoco_argline_bridge",
    "_with_maven_central_mirror",
]
