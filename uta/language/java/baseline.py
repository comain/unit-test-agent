"""Prepare and compile a Java project before test generation."""

from __future__ import annotations

from uta.language.java.generation.quality import _ci_incremental_modules_for_batch
from uta.language.java.maven.surefire import relax_surefire_skiptests

import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

from uta.testgen.project_summary_artifacts import (
    maybe_run_opencode_init_slash,
    maybe_run_project_init_command,
)
from uta.shared.config import settings as uta_settings
from uta.testgen.graph.state import AgentState
from uta.testgen.progress import merge_phase_timings as _merge_phase_timings
from uta.testgen.progress import set_stage as _set_stage
from uta.testgen.workspace_guard import git_status_snapshot as _git_status_snapshot

logger = logging.getLogger("uta")


def _upgrade_mockito(repo_path: str) -> bool:
    """Upgrade/normalize Mockito test dependencies in pom.xml files.

    - mockito-all → mockito-core:2.28.2 (with or without version tag)
    - Removes mockito-inline if present (incompatible with Java 8)
    - Pins ByteBuddy + agent to 1.9.10 when Mockito is present, avoiding the
      common runtime mismatch where mockito-core 2.28.2 is paired with
      byte-buddy 1.9.6 and byte-buddy-agent 1.9.10.

    Returns True if any pom was modified.
    """
    import re
    modified = False

    for pom_path in Path(repo_path).rglob("pom.xml"):
        try:
            content = pom_path.read_text(errors="replace")
            original = content
        except Exception:
            continue

        # Replace mockito-all with mockito-core — handle both with and without <version>
        if "mockito-all" in content:
            # Case 1: <artifactId>mockito-all</artifactId> followed by <version>
            content = re.sub(
                r"<artifactId>mockito-all</artifactId>(\s*<version>[^<]+</version>)",
                "<artifactId>mockito-core</artifactId>\n            <version>2.28.2</version>",
                content,
            )
            # Case 2: <artifactId>mockito-all</artifactId> without <version> (inherits from parent)
            content = content.replace(
                "<artifactId>mockito-all</artifactId>",
                "<artifactId>mockito-core</artifactId>",
            )
            logger.info("Upgraded mockito-all → mockito-core in %s", pom_path)

        # Also remove the stale property if present
        content = re.sub(
            r"<mockito-all\.version>[^<]+</mockito-all\.version>",
            "<mockito-core.version>2.28.2</mockito-core.version>",
            content,
        )

        if "<mockito-core.version>" in content and "<byte-buddy.version>" not in content:
            content = content.replace(
                "<mockito-core.version>2.28.2</mockito-core.version>",
                "<mockito-core.version>2.28.2</mockito-core.version>\n        <byte-buddy.version>1.9.10</byte-buddy.version>",
            )

        content = re.sub(
            r"<byte-buddy\.version>[^<]+</byte-buddy\.version>",
            "<byte-buddy.version>1.9.10</byte-buddy.version>",
            content,
        )

        # Upgrade mockito-core 1.x to 2.28.2
        def _bump_mockito(m):
            ver = m.group(1)
            if ver.startswith("1."):
                return "<artifactId>mockito-core</artifactId>\n            <version>2.28.2</version>"
            return m.group(0)

        content = re.sub(
            r"<artifactId>mockito-core</artifactId>\s*<version>([^<]+)</version>",
            _bump_mockito,
            content,
        )

        # NOTE: Do NOT add mockito-inline — it is incompatible with Java 8
        # (causes ClassNotFoundException: mock-maker-default).
        # Instead, the prompt instructs the LLM to never mock concrete classes.

        # Remove mockito-inline if it was previously injected
        if "mockito-inline" in content:
            content = re.sub(
                r"\s*<dependency>\s*<groupId>org\.mockito</groupId>\s*"
                r"<artifactId>mockito-inline</artifactId>\s*"
                r"<version>[^<]+</version>\s*"
                r"(?:<scope>[^<]+</scope>\s*)?"
                r"</dependency>",
                "",
                content,
            )
            logger.info("Removed mockito-inline (Java 8 incompatible) from %s", pom_path)

        if "byte-buddy" in content:
            content = re.sub(
                r"(<artifactId>byte-buddy(?:-agent)?</artifactId>\s*<version>)[^<]+(</version>)",
                r"\g<1>${byte-buddy.version}\2",
                content,
            )

        if content != original:
            pom_path.write_text(content)
            modified = True

    # If no pom.xml has mockito-core at all, add it to the root pom's <dependencies>
    root_pom = Path(repo_path) / "pom.xml"
    if root_pom.exists():
        root_content = root_pom.read_text(errors="replace")
        root_original = root_content
        if "<mockito-core.version>" in root_content and "<byte-buddy.version>" not in root_content:
            root_content = root_content.replace(
                "<mockito-core.version>2.28.2</mockito-core.version>",
                "<mockito-core.version>2.28.2</mockito-core.version>\n        <byte-buddy.version>1.9.10</byte-buddy.version>",
            )
        elif "mockito-core" in root_content and "<byte-buddy.version>" not in root_content and "</properties>" in root_content:
            root_content = root_content.replace(
                "</properties>",
                "        <byte-buddy.version>1.9.10</byte-buddy.version>\n    </properties>",
                1,
            )
        if "mockito-core" not in root_content and "mockito-all" not in root_content:
            mockito_dep = (
                "        <dependency>\n"
                "            <groupId>org.mockito</groupId>\n"
                "            <artifactId>mockito-core</artifactId>\n"
                "            <version>2.28.2</version>\n"
                "            <scope>test</scope>\n"
                "        </dependency>\n"
            )
            # Insert before last </dependencies>
            last_idx = root_content.rfind("</dependencies>")
            if last_idx >= 0:
                root_content = root_content[:last_idx] + mockito_dep + "    " + root_content[last_idx:]
                logger.info("Added mockito-core:2.28.2 to %s (was missing entirely)", root_pom)

        if "mockito-core" in root_content:
            # Define the property here, not only in the branches above: those
            # test the pom as it was *before* mockito-core may have just been
            # added, so a pom that had no mockito at all reaches this point
            # with the property still undefined. Injecting dependencies pinned
            # to `${byte-buddy.version}` then breaks the build outright, and it
            # surfaces as a baseline compile failure that looks like a problem
            # with the repository rather than something we did to its pom.
            if "<byte-buddy.version>" not in root_content and "</properties>" in root_content:
                root_content = root_content.replace(
                    "</properties>",
                    "        <byte-buddy.version>1.9.10</byte-buddy.version>\n    </properties>",
                    1,
                )

            byte_buddy_mgmt = (
                "            <dependency>\n"
                "                <groupId>net.bytebuddy</groupId>\n"
                "                <artifactId>byte-buddy</artifactId>\n"
                "                <version>${byte-buddy.version}</version>\n"
                "                <scope>test</scope>\n"
                "            </dependency>\n"
                "\n"
                "            <dependency>\n"
                "                <groupId>net.bytebuddy</groupId>\n"
                "                <artifactId>byte-buddy-agent</artifactId>\n"
                "                <version>${byte-buddy.version}</version>\n"
                "                <scope>test</scope>\n"
                "            </dependency>\n"
            )
            if "<dependencyManagement>" in root_content and "<artifactId>byte-buddy</artifactId>" not in root_content:
                insert_at = root_content.find("</dependencies>", root_content.find("<dependencyManagement>"))
                if insert_at >= 0:
                    root_content = root_content[:insert_at] + byte_buddy_mgmt + root_content[insert_at:]
                    logger.info("Added ByteBuddy dependencyManagement entries to %s", root_pom)

            byte_buddy_dep = (
                "        <dependency>\n"
                "            <groupId>net.bytebuddy</groupId>\n"
                "            <artifactId>byte-buddy</artifactId>\n"
                "            <scope>test</scope>\n"
                "        </dependency>\n"
                "\n"
                "        <dependency>\n"
                "            <groupId>net.bytebuddy</groupId>\n"
                "            <artifactId>byte-buddy-agent</artifactId>\n"
                "            <scope>test</scope>\n"
                "        </dependency>\n"
            )
            deps_start = root_content.rfind("<dependencies>")
            deps_end = root_content.rfind("</dependencies>")
            deps_block = root_content[deps_start:deps_end] if deps_start >= 0 and deps_end >= 0 else ""
            if deps_block and "<artifactId>byte-buddy</artifactId>" not in deps_block:
                root_content = root_content[:deps_end] + byte_buddy_dep + "    " + root_content[deps_end:]
                logger.info("Added direct ByteBuddy test deps to %s", root_pom)

        if root_content != root_original:
            root_pom.write_text(root_content)
            modified = True

    return modified


def _mockito_api_guidance(repo_path: str) -> str:
    has_mockito_all = False
    has_mockito_core = False
    for pom_path in Path(repo_path).rglob("pom.xml"):
        try:
            content = pom_path.read_text(errors="replace")
        except Exception:
            continue
        has_mockito_all = has_mockito_all or "mockito-all" in content
        has_mockito_core = has_mockito_core or "mockito-core" in content
    if has_mockito_all and not has_mockito_core:
        return (
            "- Detected committed repo dependencies use Mockito 1.x (`mockito-all`).\n"
            "- Use `org.mockito.Matchers` and `org.mockito.runners.MockitoJUnitRunner`.\n"
            "- Do NOT import `org.mockito.ArgumentMatchers` or `org.mockito.junit.MockitoJUnitRunner` unless the committed pom already uses Mockito 2.x."
        )
    return (
        "- Use **Mockito 2.x** for interfaces and abstract collaborators when that fits the repo style.\n"
        "- Use `org.mockito.ArgumentMatchers.any()` — NOT the deprecated `org.mockito.Matchers.any()`."
    )


def _fix_mockito_imports(repo_path: str):
    """Fix existing test files for Mockito 1→2 migration.

    - org.mockito.Matchers → org.mockito.ArgumentMatchers
    - org.mockito.runners.MockitoJUnitRunner → org.mockito.junit.MockitoJUnitRunner
    """
    test_dirs = list(Path(repo_path).rglob("src/test/java"))
    for test_dir in test_dirs:
        for java_file in test_dir.rglob("*.java"):
            try:
                content = java_file.read_text(errors="replace")
                original = content
                content = content.replace(
                    "org.mockito.Matchers",
                    "org.mockito.ArgumentMatchers",
                )
                content = content.replace(
                    "org.mockito.runners.MockitoJUnitRunner",
                    "org.mockito.junit.MockitoJUnitRunner",
                )
                if content != original:
                    java_file.write_text(content)
                    logger.info("Fixed Mockito imports in %s", java_file)
            except Exception:
                continue


_PAIR_SHIM = '''\
package javafx.util;

/**
 * Minimal shim for javafx.util.Pair — allows projects that import this class
 * to compile on OpenJDK/Zulu (which does not bundle JavaFX).
 * Auto-generated by UTA baseline_compile.
 */
public class Pair<K, V> {
    private final K key;
    private final V value;

    public Pair(K key, V value) {
        this.key = key;
        this.value = value;
    }

    public K getKey() { return key; }
    public V getValue() { return value; }

    @Override
    public String toString() { return key + "=" + value; }

    @Override
    public int hashCode() {
        int h = key != null ? key.hashCode() : 0;
        h = 31 * h + (value != null ? value.hashCode() : 0);
        return h;
    }

    @Override
    public boolean equals(Object o) {
        if (this == o) return true;
        if (!(o instanceof Pair)) return false;
        Pair<?, ?> p = (Pair<?, ?>) o;
        return java.util.Objects.equals(key, p.key) && java.util.Objects.equals(value, p.value);
    }
}
'''


def _ensure_javafx_pair(repo_path: str) -> bool:
    """Create a javafx.util.Pair shim if the project uses it.

    Many legacy projects import javafx.util.Pair which is bundled with Oracle JDK 8
    but missing from OpenJDK/Zulu. We create a minimal Pair class in the first module
    that has src/main/java so the import resolves without changing any source files.
    """
    # Quick check: does any Java file import javafx.util.Pair?
    has_javafx = False
    for java_file in Path(repo_path).rglob("src/main/java/**/*.java"):
        try:
            if "javafx.util.Pair" in java_file.read_text(errors="replace"):
                has_javafx = True
                break
        except Exception:
            continue

    if not has_javafx:
        return False

    # Find a suitable module to place the shim — prefer common, then service, then root
    candidates = ["common", "service", "model"]
    target_dir = None
    for mod in candidates:
        d = Path(repo_path) / mod / "src" / "main" / "java"
        if d.exists():
            target_dir = d
            break
    if not target_dir:
        # Fall back to any module with src/main/java
        for d in Path(repo_path).glob("*/src/main/java"):
            target_dir = d
            break
    if not target_dir:
        target_dir = Path(repo_path) / "src" / "main" / "java"

    shim_dir = target_dir / "javafx" / "util"
    shim_file = shim_dir / "Pair.java"
    if shim_file.exists():
        return False

    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_file.write_text(_PAIR_SHIM)
    logger.info("Created javafx.util.Pair shim at %s", shim_file)
    return True


def baseline_compile(state: AgentState) -> Dict[str, Any]:
    """Upgrade Mockito to 2.x if needed, then verify target module compiles."""
    started = time.perf_counter()
    repo_path = state["repo_path"]
    module = state["module"]
    quality_mode = state.get("quality_mode") or "class_batch"
    _set_stage(state, "baseline_compile", "upgrade test deps and verify compile")
    before_deterministic = _git_status_snapshot(repo_path) if state.get("task_id") and state.get("task_db_path") else {}

    # Upgrade Mockito 1.x → 2.x in pom.xml and fix existing test imports
    if quality_mode == "ci_incremental":
        logger.info("ci_incremental mode: preserving committed test dependencies during baseline compile")
    elif _upgrade_mockito(repo_path):
        logger.info("Mockito upgraded to 2.x — fixing existing test imports")
        _fix_mockito_imports(repo_path)

    if relax_surefire_skiptests(repo_path):
        logger.info("Rewrote hardcoded surefire skipTests so UTA can force test execution")

    # Add javafx.util.Pair shim if needed (missing on OpenJDK/Zulu)
    _ensure_javafx_pair(repo_path)

    deterministic_change_paths: List[str] = list(state.get("deterministic_change_paths") or [])
    def _merge_deterministic_changes(before_snapshot: Dict[str, str]) -> List[str]:
        if not before_snapshot:
            return deterministic_change_paths
        after_snapshot = _git_status_snapshot(repo_path)
        changed_paths = sorted(
            path
            for path in set(before_snapshot.keys()) | set(after_snapshot.keys())
            if before_snapshot.get(path) != after_snapshot.get(path)
        )
        if not changed_paths:
            return deterministic_change_paths
        return list(dict.fromkeys(deterministic_change_paths + changed_paths))

    if before_deterministic:
        after_deterministic = _git_status_snapshot(repo_path)
        changed = sorted(
            path
            for path in set(before_deterministic.keys()) | set(after_deterministic.keys())
            if before_deterministic.get(path) != after_deterministic.get(path)
        )
        if changed:
            deterministic_change_paths = list(dict.fromkeys(deterministic_change_paths + changed))
            try:
                from uta.tasks.manager import TaskManager

                TaskManager(state["task_db_path"]).db.add_event(
                    int(state["task_id"]),
                    None,
                    "deterministic_change",
                    "Deterministic baseline setup changed files",
                    stage="baseline_compile",
                    payload={"paths": changed},
                )
            except Exception:
                logger.debug("Failed to record deterministic change audit event", exc_info=True)

    # Try compiling with -am first (builds dependencies too).
    # If that fails due to pre-existing errors in sibling modules,
    # fall back to compiling just the target module (assumes deps are installed).
    from uta.language.java.maven_project import with_default_profile_args
    from uta.language.java.enforcement_runner.execution import _with_maven_central_mirror

    def _mirrored(command):
        """Route Central through the corporate mirror, as Java enforcement does.

        The baseline compile builds its own Maven command and so never picked up
        `maven_central_mirror_url`. On a host without a `<mirror>` in
        settings.xml that sends it straight to repo.maven.apache.org, where an
        internal artifact cannot exist -- and where the JDK's truststore then
        fails the TLS handshake, reporting a PKIX error instead of a missing
        dependency.
        """
        return _with_maven_central_mirror(
            command, Path(repo_path), uta_settings.maven_central_mirror_url
        )

    cmd = _mirrored(
        with_default_profile_args([uta_settings.maven_bin, "compile", "-DskipTests"], Path(repo_path))
    )
    # A CI-incremental run has no configured module: its scope is the classes
    # the diff touched. Compiling the whole reactor for them is the slow path
    # and drags unrelated modules' pre-existing errors into this task's
    # baseline, so derive the modules the batch actually needs.
    compile_modules = [module] if module else []
    if quality_mode == "ci_incremental" and not compile_modules:
        compile_modules = _ci_incremental_modules_for_batch(
            state,
            repo_path,
            list(state.get("explicit_class_fqns") or []),
        )
    if compile_modules:
        cmd.extend(["-pl", ",".join(compile_modules), "-am"])

    def _compile_ok() -> Dict[str, Any]:
        before_init = _git_status_snapshot(repo_path) if state.get("task_id") and state.get("task_db_path") else {}
        maybe_run_opencode_init_slash(repo_path, state.get("session_id"))
        maybe_run_project_init_command(repo_path, uta_settings.opencode_init_command)
        updated_change_paths = _merge_deterministic_changes(before_init)
        return {
            "error": None,
            "current_stage": "baseline_compile",
            "deterministic_change_paths": updated_change_paths,
            "phase_timings": _merge_phase_timings(
                state,
                baseline_compile_seconds=time.perf_counter() - started,
            ),
        }

    try:
        subprocess.run(cmd, cwd=repo_path, capture_output=True, check=True, timeout=600)
        return _compile_ok()
    except subprocess.CalledProcessError as e:
        if compile_modules:
            # Fall back: compile only the target modules (deps must be in local .m2)
            logger.warning("Full compile failed, trying target module only...")
            cmd_fallback = _mirrored(
                with_default_profile_args(
                    [uta_settings.maven_bin, "compile", "-DskipTests", "-pl", ",".join(compile_modules)],
                    Path(repo_path),
                )
            )
            try:
                subprocess.run(cmd_fallback, cwd=repo_path, capture_output=True,
                               check=True, timeout=600)
                logger.info("Target module compiled successfully (without -am)")
                return _compile_ok()
            except subprocess.CalledProcessError as e2:
                stderr = e2.stderr.decode(errors='replace') if e2.stderr else ""
                stdout = e2.stdout.decode(errors='replace') if e2.stdout else ""
                error_msg = stderr or stdout[-2000:] if stdout else "unknown error"
                return {
                    "error": f"Baseline compilation failed: {error_msg[-1000:]}",
                    "current_stage": "baseline_compile",
                    "deterministic_change_paths": deterministic_change_paths,
                    "phase_timings": _merge_phase_timings(
                        state,
                        baseline_compile_seconds=time.perf_counter() - started,
                    ),
                }
        stderr = e.stderr.decode(errors='replace') if e.stderr else ""
        stdout = e.stdout.decode(errors='replace') if e.stdout else ""
        error_msg = stderr or stdout[-2000:] if stdout else "unknown error"
        return {
            "error": f"Baseline compilation failed: {error_msg[-1000:]}",
            "current_stage": "baseline_compile",
            "deterministic_change_paths": deterministic_change_paths,
            "phase_timings": _merge_phase_timings(
                state,
                baseline_compile_seconds=time.perf_counter() - started,
            ),
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "Baseline compilation timed out (10min)",
            "current_stage": "baseline_compile",
            "deterministic_change_paths": deterministic_change_paths,
            "phase_timings": _merge_phase_timings(
                state,
                baseline_compile_seconds=time.perf_counter() - started,
            ),
        }
