from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Dict, Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class MavenToolingStatus:
    """Describes whether Maven resolves the Java test-enforcer plugin."""

    available: bool
    reason: str
    artifact_id: str = ""
    version: str = ""


RunMavenCommand = Callable[[List[str], Path], object]


def test_enforcement_tooling_status(
    repo_path: Path,
    *,
    maven_bin: str = "mvn",
    run_maven_command: Optional[RunMavenCommand] = None,
    profile_source_cmd: Optional[Sequence[str]] = None,
) -> MavenToolingStatus:
    """Check if Maven resolves the Java test-enforcer plugin."""

    repo = Path(repo_path)
    if run_maven_command is not None:
        status = _effective_pom_tooling_status(
            repo,
            maven_bin=maven_bin,
            run_maven_command=run_maven_command,
            profile_source_cmd=profile_source_cmd,
        )
        if status is not None:
            return status
    return _raw_pom_tooling_status(repo)


def _effective_pom_tooling_status(
    repo: Path,
    *,
    maven_bin: str,
    run_maven_command: RunMavenCommand,
    profile_source_cmd: Optional[Sequence[str]],
) -> Optional[MavenToolingStatus]:
    with TemporaryDirectory(prefix="uta-effective-pom-") as tmp:
        output = Path(tmp) / "effective-pom.xml"
        cmd = [
            maven_bin,
            "-q",
            "help:effective-pom",
            "-DskipTests",
            "-Dmaven.test.skip=true",
            f"-Doutput={output}",
        ]
        cmd.extend(_maven_context_args(profile_source_cmd or ()))
        cmd = with_default_profile_args(cmd, repo)
        try:
            completed = run_maven_command(cmd, repo)
        except Exception:
            return None
        if getattr(completed, "returncode", 1) != 0 or not output.exists():
            return None
        root = _parse_xml(output)
        if root is None:
            return None
        return _tooling_status_from_roots([root])


def _raw_pom_tooling_status(repo: Path) -> MavenToolingStatus:
    roots: List[ET.Element] = []
    candidates: List[MavenToolingStatus] = []
    for pom in _pom_files(repo):
        root = _parse_xml(pom)
        if root is None:
            continue
        roots.append(root)
    if roots:
        return _tooling_status_from_roots(roots)
    return MavenToolingStatus(
        available=False,
        reason="No readable Maven POM was found",
    )


def _tooling_status_from_roots(roots: Sequence[ET.Element]) -> MavenToolingStatus:
    properties = _collect_properties(roots)
    plugin_candidates: List[MavenToolingStatus] = []
    parent_hints: List[MavenToolingStatus] = []
    for root in roots:
        parent_hints.extend(_parent_hints(root, properties))
        plugin_candidates.extend(_plugin_statuses(root, properties))
    passing = [item for item in plugin_candidates if item.available]
    if passing:
        return passing[0]
    if plugin_candidates:
        candidate = plugin_candidates[0]
        return MavenToolingStatus(
            available=False,
            artifact_id=candidate.artifact_id,
            version=candidate.version,
            reason=(
                f"{candidate.artifact_id} {candidate.version or 'unknown'} is below the required version"
            ),
        )
    if parent_hints:
        return parent_hints[0]
    return MavenToolingStatus(
        available=False,
        reason="No resolved test-enforcer Maven plugin was found",
    )


def with_default_profile_args(cmd: Sequence[str], repo_path: Path) -> List[str]:
    """Add a compile profile when the repo uses resources.${profile.active}."""

    cmd = [str(item) for item in cmd]
    if _has_profile_selector(cmd) or not _requires_profile_active(Path(repo_path)):
        return cmd
    profile = _default_profile(Path(repo_path))
    if not profile:
        return cmd
    return [*cmd, f"-P{profile}"]


def _pom_files(repo: Path) -> Iterable[Path]:
    root_pom = repo / "pom.xml"
    if root_pom.exists():
        yield root_pom
    for pom in sorted(repo.glob("*/pom.xml")):
        yield pom


def _parse_xml(path: Path) -> Optional[ET.Element]:
    try:
        return ET.parse(path).getroot()
    except ET.ParseError:
        return None


def _collect_properties(roots: Sequence[ET.Element]) -> Dict[str, str]:
    properties: Dict[str, str] = {}
    for root in roots:
        props = _find_child(root, "properties")
        if props is None:
            continue
        for child in list(props):
            key = _strip_namespace(child.tag)
            value = (child.text or "").strip()
            if key and value:
                properties[key] = value
    return properties


def _parent_hints(root: ET.Element, properties: Dict[str, str]) -> List[MavenToolingStatus]:
    parent = _find_child(root, "parent")
    if parent is None:
        return []
    artifact_id = _child_text(parent, "artifactId")
    version = _resolve_version(_child_text(parent, "version"), properties)
    if artifact_id == "quality-parent":
        minimum = "1.0.0"
        return [_parent_hint(artifact_id, version, minimum)]
    if artifact_id == "service-parent":
        minimum = "1.0.0"
        return [_parent_hint(artifact_id, version, minimum)]
    return []


def _plugin_statuses(root: ET.Element, properties: Dict[str, str]) -> List[MavenToolingStatus]:
    statuses: List[MavenToolingStatus] = []
    for plugin in _build_plugins(root):
        artifact_id = _child_text(plugin, "artifactId")
        if artifact_id != "test-enforcer":
            continue
        statuses.append(_status_for(artifact_id, _resolve_version(_child_text(plugin, "version"), properties), "1.0.12"))
    return statuses


def _status_for(artifact_id: str, version: str, minimum: str) -> MavenToolingStatus:
    available = _version_at_least(version, minimum)
    return MavenToolingStatus(
        available=available,
        artifact_id=artifact_id,
        version=version,
        reason=f"{artifact_id} {version or 'unknown'} {'meets' if available else 'is below'} required {minimum}",
    )


def _parent_hint(artifact_id: str, version: str, minimum: str) -> MavenToolingStatus:
    if _version_at_least(version, minimum):
        reason = (
            f"{artifact_id} {version or 'unknown'} is present, but Maven did not resolve "
            "test-enforcer; verify the test-enforcement profile is active"
        )
    else:
        reason = (
            f"{artifact_id} {version or 'unknown'} is below the rollout version that introduces "
            "test-enforcer"
        )
    return MavenToolingStatus(
        available=False,
        artifact_id=artifact_id,
        version=version,
        reason=reason,
    )


def _resolve_version(value: str, properties: Dict[str, str]) -> str:
    value = (value or "").strip()
    match = re.fullmatch(r"\$\{([^}]+)\}", value)
    if match:
        return properties.get(match.group(1), value)
    return value


def _version_at_least(version: str, minimum: str) -> bool:
    left = _version_tuple(version)
    right = _version_tuple(minimum)
    return bool(left) and left >= right


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version or "")
    if not parts:
        return ()
    return tuple(int(part) for part in parts[:4])


def _requires_profile_active(repo: Path) -> bool:
    for pom in _pom_files(repo):
        text = pom.read_text(encoding="utf-8", errors="ignore")
        if "${profile.active}" in text or "resources.${profile.active}" in text:
            return True
    return False


def _has_profile_selector(cmd: Sequence[str]) -> bool:
    return any(
        item == "-P"
        or item.startswith("-P")
        or item.startswith("-Dprofile.active=")
        for item in cmd
    )


def _maven_context_args(cmd: Sequence[str]) -> List[str]:
    context: List[str] = []
    with_value = {
        "-P",
        "--activate-profiles",
        "-s",
        "--settings",
        "-gs",
        "--global-settings",
    }
    index = 0
    while index < len(cmd):
        item = str(cmd[index])
        if item in with_value:
            context.append(item)
            if index + 1 < len(cmd):
                context.append(str(cmd[index + 1]))
                index += 2
                continue
        elif (
            item.startswith("-D")
            or item.startswith("-P")
            or item.startswith("--activate-profiles=")
            or item.startswith("--settings=")
            or item.startswith("--global-settings=")
        ):
            context.append(item)
        index += 1
    return context


def _default_profile(repo: Path) -> str:
    profiles = _profile_ids(repo / "pom.xml")
    for preferred in ("dev", "local", "beta", "prod"):
        if preferred in profiles:
            return preferred
    return profiles[0] if profiles else ""


def _profile_ids(pom: Path) -> List[str]:
    root = _parse_xml(pom)
    if root is None:
        return []
    ids: List[str] = []
    for profile in _iter_descendants(root, "profile"):
        profile_id = _child_text(profile, "id")
        if profile_id:
            ids.append(profile_id)
    return ids


def _child_text(parent: ET.Element, child_name: str) -> str:
    child = _find_child(parent, child_name)
    return (child.text or "").strip() if child is not None else ""


def _find_child(parent: ET.Element, child_name: str) -> Optional[ET.Element]:
    for child in list(parent):
        if _strip_namespace(child.tag) == child_name:
            return child
    return None


def _iter_descendants(parent: ET.Element, tag_name: str) -> Iterable[ET.Element]:
    for child in parent.iter():
        if _strip_namespace(child.tag) == tag_name:
            yield child


def _build_plugins(root: ET.Element) -> Iterable[ET.Element]:
    build = _find_child(root, "build")
    if build is None:
        return []
    plugins = _find_child(build, "plugins")
    if plugins is None:
        return []
    return [
        plugin
        for plugin in list(plugins)
        if _strip_namespace(plugin.tag) == "plugin"
    ]


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag
