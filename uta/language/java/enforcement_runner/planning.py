"""Deciding what Maven is asked to enforce, before anything runs.

Given the diff, this module works out the changed production sources, the
Maven modules and PIT target tests that cover them, and the exact argument
vector -- gate properties, target scope, reactor selectors, the filter-diff
preflight command. It reads the repository but owns no enforcement run of its
own; the one process it needs (the preflight) is handed in as ``run_command``
so planning stays testable without Maven.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from uta.enforcement.enforcement import TEST_ENFORCEMENT_USAGE_GUIDE
from uta.language.java.maven_project import _child_text
from uta.language.java.workspace import maven_module_for_path

JAVA_CI_COVERAGE_GATE_PROPERTY = "test.enforcement.diff.coverage.minLines"
JAVA_CI_MUTATION_GATE_PROPERTY = "test.enforcement.pitest.testStrengthThreshold"

# Linux limits each individual argv entry below ARG_MAX. Leave headroom for
# platform differences and Maven launcher expansion when legacy Surefire needs
# the complete positive inventory in one `-Dtest` argument.
_MAX_SUREFIRE_TEST_SELECTOR_BYTES = 96 * 1024


def _validate_command(cmd: List[str]) -> None:
    normalized = " ".join(cmd).strip()
    if normalized == "mvn test":
        raise ValueError(
            "Configured command must run UTA test-enforcement, not plain mvn test. "
            f"See {TEST_ENFORCEMENT_USAGE_GUIDE}"
        )
    has_enforcement_flag = any("test.enforcement.enabled=true" in item for item in cmd)
    has_enforcement_goal = any("test-enforcement" in item or "test.enforcer" in item for item in cmd)
    if not has_enforcement_flag and not has_enforcement_goal:
        raise ValueError(
            "Configured command must run UTA test-enforcement. "
            f"See {TEST_ENFORCEMENT_USAGE_GUIDE}"
        )


def _with_target_tests(cmd: List[str], target_tests: Sequence[str]) -> List[str]:
    updated = list(cmd)
    if not any(item.startswith("-DtargetTests=") for item in updated):
        updated.append(f"-DtargetTests={','.join(target_tests)}")
    if not any(item.startswith("-Dtest=") for item in updated):
        test_selector = ",".join(_surefire_test_selector(target_tests))
        if test_selector:
            updated.append(f"-Dtest={test_selector}")
    # A reactor-wide selected test is absent from upstream modules. Surefire
    # 2.5 treats that as fatal and does not understand the newer property.
    if any(item.startswith("-Dtest=") for item in updated) and not any(
        item.startswith("-DfailIfNoTests=") for item in updated
    ):
        updated.append("-DfailIfNoTests=false")
    if any(item.startswith("-Dtest=") for item in updated) and not any(
        item.startswith("-Dsurefire.failIfNoSpecifiedTests=") for item in updated
    ):
        updated.append("-Dsurefire.failIfNoSpecifiedTests=false")
    return updated


#: A Java FQN, and nothing that could be read as a Maven or shell argument.
#: The package is optional: legacy UTA suites still put test classes in the
#: default package, and `CardTest` is that class's fully qualified name. Demanding
#: a dot dropped those from the command line while the report still listed them
#: as excluded, so the four that mattered ran again and failed again.
_JAVA_FQN = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")


def _is_excludable_test_class(name: str) -> bool:
    """Can this name be handed to Maven as a test class to exclude?

    The exclusion evidence must name the classes that actually left the run, so
    the partition asks this before promising anything: a name that cannot reach
    the command line is retained and reported as retained, never silently
    dropped between the report and the argument vector.
    """
    return bool(_JAVA_FQN.match((name or "").strip()))


def _with_selected_test_classes(
    cmd: List[str], kept: Sequence[str], excluded: Sequence[str]
) -> List[str]:
    """Run exactly the observed passing tests through legacy Surefire.

    `excludedTestClasses` still carries the drop list because that property is
    PIT's, not Surefire's, and PIT reads it at any version.

    `failIfNoTests=false` is required and is the old spelling that Surefire 2.5
    itself suggests -- a positive list is reactor-wide, so a module holding none
    of the named classes runs nothing and would otherwise kill the build.
    """
    keep = [item.strip() for item in kept if _is_excludable_test_class(item)]
    drop = [item.strip() for item in excluded if _is_excludable_test_class(item)]
    if not keep or not drop:
        return list(cmd)

    selector = "-Dtest=" + ",".join(_surefire_test_selector(keep))
    if len(selector.encode("utf-8")) > _MAX_SUREFIRE_TEST_SELECTOR_BYTES:
        return list(cmd)

    updated = _without_maven_property(list(cmd), "test")
    updated.append(selector)

    existing_excluded = _maven_property_value(updated, "excludedTestClasses")
    updated = _without_maven_property(updated, "excludedTestClasses")
    updated.append("-DexcludedTestClasses=" + _merged_csv(existing_excluded, drop))

    if not any(item.startswith("-DfailIfNoTests=") for item in updated):
        updated.append("-DfailIfNoTests=false")
    return updated


def _with_selected_test_file(
    cmd: List[str], inventory_file: Path, excluded: Sequence[str]
) -> List[str]:
    """Use a file as the only positive Surefire inventory.

    Surefire appends `includesFile` to configured includes, so the no-match
    override is essential: without it a POM include could broaden the rerun.
    This remains a positive selector and therefore cannot make a nonstandard
    `@Test` utility class discoverable the way negative-only `-Dtest` did.
    """
    drop = [item.strip() for item in excluded if _is_excludable_test_class(item)]
    if not drop:
        return list(cmd)

    updated = _without_maven_property(list(cmd), "test")
    updated = _without_maven_property(updated, "surefire.includes")
    updated = _without_maven_property(updated, "surefire.includesFile")
    updated.append("-Dsurefire.includes=__uta_no_test_matches__")
    updated.append(f"-Dsurefire.includesFile={inventory_file}")

    existing_excluded = _maven_property_value(updated, "excludedTestClasses")
    updated = _without_maven_property(updated, "excludedTestClasses")
    updated.append("-DexcludedTestClasses=" + _merged_csv(existing_excluded, drop))
    if not any(item.startswith("-DfailIfNoTests=") for item in updated):
        updated.append("-DfailIfNoTests=false")
    if not any(item.startswith("-Dsurefire.failIfNoSpecifiedTests=") for item in updated):
        updated.append("-Dsurefire.failIfNoSpecifiedTests=false")
    return updated


def _maven_property_value(cmd: Sequence[str], property_name: str) -> str:
    prefix = f"-D{property_name}="
    for item in cmd:
        if item.startswith(prefix):
            return item[len(prefix):]
    return ""


def _merged_csv(existing: str, additions: Sequence[str]) -> str:
    values = [item.strip() for item in existing.split(",") if item.strip()]
    for item in additions:
        if item not in values:
            values.append(item)
    return ",".join(values)


def _surefire_test_selector(target_tests: Sequence[str]) -> List[str]:
    selectors = []
    seen = set()
    for target_test in target_tests:
        selector = target_test.rsplit(".", 1)[-1].strip()
        if selector and selector not in seen:
            selectors.append(selector)
            seen.add(selector)
    return selectors


def _with_changed_modules(cmd: List[str], changed_modules: Sequence[str]) -> List[str]:
    if not changed_modules:
        return list(cmd)
    # Keep the Maven reactor selector owned by UTA once target evidence exists.
    # It must match test.enforcement.targetSources; otherwise Maven can verify
    # a module outside the filtered target set and fail on unrelated JaCoCo/PIT output.
    updated = _without_maven_project_selector(cmd)
    updated.extend(["-pl", ",".join(changed_modules)])
    if not any(item in {"-am", "--also-make"} for item in updated):
        updated.append("-am")
    return updated


def _without_maven_project_selector(cmd: Sequence[str]) -> List[str]:
    filtered: List[str] = []
    skip_next = False
    for item in cmd:
        if skip_next:
            skip_next = False
            continue
        if item in {"-pl", "--projects"}:
            skip_next = True
            continue
        if item.startswith("-pl=") or item.startswith("--projects="):
            continue
        filtered.append(item)
    return filtered


def _without_maven_property(cmd: Sequence[str], property_name: str) -> List[str]:
    filtered: List[str] = []
    skip_next = False
    for item in cmd:
        if skip_next:
            skip_next = False
            continue
        if item == f"-D{property_name}":
            skip_next = True
            continue
        if item.startswith(f"-D{property_name}="):
            continue
        filtered.append(item)
    return filtered


def _with_target_sources(cmd: List[str], target_sources: Sequence[str]) -> List[str]:
    if not target_sources:
        return list(cmd)
    updated = _without_maven_property(cmd, "test.enforcement.targetSource")
    updated = _without_maven_property(updated, "test.enforcement.targetSources")
    updated.append(f"-Dtest.enforcement.targetSources={','.join(target_sources)}")
    return updated


def _gate_percent_value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _without_maven_reactor_dependency_selectors(cmd: Sequence[str]) -> List[str]:
    return [
        item
        for item in cmd
        if item not in {"-am", "--also-make", "-amd", "--also-make-dependents"}
    ]


def _replace_maven_goals(cmd: Sequence[str], goal: str) -> List[str]:
    lifecycle_goals = {
        "validate",
        "initialize",
        "generate-sources",
        "process-sources",
        "generate-resources",
        "process-resources",
        "compile",
        "process-classes",
        "generate-test-sources",
        "process-test-sources",
        "generate-test-resources",
        "process-test-resources",
        "test-compile",
        "process-test-classes",
        "test",
        "prepare-package",
        "package",
        "pre-integration-test",
        "integration-test",
        "post-integration-test",
        "verify",
        "install",
        "deploy",
        "clean",
    }
    updated: List[str] = []
    removed_goal = False
    for item in cmd:
        if item in lifecycle_goals or (":" in item and not item.startswith("-")):
            removed_goal = True
            continue
        updated.append(item)
    if removed_goal or goal not in updated:
        updated.append(goal)
    return updated


def _maven_modules_for_changed_files(repo_path: Path, changed_production_java: Sequence[str]) -> List[str]:
    repo = Path(repo_path)
    modules: List[str] = []
    for path_text in changed_production_java:
        module = _maven_module_for_path(repo, path_text)
        if module and module not in modules:
            modules.append(module)
    return modules


def _maven_module_for_path(repo_path: Path, path_text: str) -> Optional[str]:
    return maven_module_for_path(str(repo_path), path_text)


def _pitest_target_tests(
    repo_path: Path,
    changed_java: Optional[List[str]],
    changed_production_java: List[str],
) -> List[str]:
    target_tests = set()
    changed_tests: List[tuple[str, str]] = []
    for path_text in changed_java or []:
        if "src/test/java/" in path_text and path_text.endswith(".java"):
            fqn = _java_fqn_from_path(path_text, "src/test/java/")
            if fqn:
                changed_tests.append((path_text, fqn))

    for path_text in changed_production_java:
        simple_name = Path(path_text).stem
        target_module = _maven_module_for_path(repo_path, path_text)
        matched_changed_test = False
        for test_path, fqn in changed_tests:
            same_module = _maven_module_for_path(repo_path, test_path) == target_module
            if same_module and _java_test_targets_type(
                repo_path / test_path,
                simple_name,
            ):
                target_tests.add(fqn)
                matched_changed_test = True
        if matched_changed_test:
            continue
        for test_path in repo_path.glob(f"**/src/test/java/**/*{simple_name}*Test.java"):
            rel_path = test_path.relative_to(repo_path).as_posix()
            if _maven_module_for_path(repo_path, rel_path) != target_module:
                continue
            fqn = _java_fqn_from_path(
                rel_path,
                "src/test/java/",
            )
            if fqn:
                target_tests.add(fqn)
    return sorted(target_tests)


def _java_test_targets_type(test_path: Path, simple_name: str) -> bool:
    """Recognize a target type from a test name or executable Java reference.

    CI may receive a behavior-named test such as FooFranchiseGuardTest.  For a
    changed test in the same Maven module, a construction, class literal, type
    declaration, inheritance, or static access is sufficient target evidence.
    Comments and arbitrary string mentions are deliberately not accepted.
    """
    if simple_name in test_path.stem:
        return True
    try:
        source = test_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    source = _java_code_without_comments_or_strings(source)
    escaped_name = re.escape(simple_name)
    patterns = (
        rf"\bnew\s+{escaped_name}\b",
        rf"\b{escaped_name}\s*\.class\b",
        rf"\b(?:extends|implements|instanceof)\s+{escaped_name}\b",
        rf"\b{escaped_name}\s*(?:<[^;=(){{}}]*>)?\s+[A-Za-z_$][\w$]*\b",
        rf"\b{escaped_name}\s*\.",
    )
    return any(re.search(pattern, source) is not None for pattern in patterns)


def _java_code_without_comments_or_strings(source: str) -> str:
    """Remove non-code text before applying the lightweight target evidence scan."""
    non_code = re.compile(
        r'"""[\s\S]*?"""|/\*[\s\S]*?\*/|//[^\r\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
    )
    return non_code.sub(lambda match: "\n" * match.group(0).count("\n"), source)


def _java_test_paths_for_fqn(repo_path: Path, test_fqn: str) -> List[str]:
    suffix = f"src/test/java/{test_fqn.replace('.', '/')}.java"
    paths: List[str] = []
    for test_path in repo_path.glob(f"**/{suffix}"):
        try:
            paths.append(test_path.relative_to(repo_path).as_posix())
        except ValueError:
            continue
    if (repo_path / suffix).is_file():
        paths.append(suffix)
    return list(dict.fromkeys(paths))


_SUREFIRE_2_DEFAULT_TEST_CLASS = re.compile(r"^(?:Test\w*|\w*Test|\w*TestCase)$")
_SUREFIRE_3_DEFAULT_TEST_CLASS = re.compile(r"^(?:Test\w*|\w*Test|\w*Tests|\w*TestCase)$")
_NON_MODULE_DIRS = {"target", "node_modules", ".git", ".uta_cache", "src"}


def _declared_test_classes_by_module(
    repo_path: Path,
    module_names: Sequence[str],
    *,
    include_tests_suffix: bool = False,
) -> Optional[Dict[str, Set[str]]]:
    """Name the tests of modules the reactor skipped, read from their sources.

    Maven is fail-fast: a module ordered after a failure writes no Surefire
    reports, so a positive rerun inventory built from reports alone would drop
    it. Its test classes are read from `src/test/java` instead, restricted to
    Surefire's default includes so the rerun selects what the first run would
    have -- a nonstandard `@Test` utility stays out, as it does for reported
    modules.

    Keyed like `_surefire_test_classes_by_module`. Returns None when any name
    cannot be tied to exactly one module directory: an inventory with a hole in
    it is exactly what this exists to prevent.
    """
    names = [name for name in module_names if name]
    if not names:
        return None
    repo = Path(repo_path)
    dirs_by_name = _module_dirs_by_display_name(repo)
    pattern = _SUREFIRE_3_DEFAULT_TEST_CLASS if include_tests_suffix else _SUREFIRE_2_DEFAULT_TEST_CLASS
    modules: Dict[str, Set[str]] = {}
    for name in names:
        candidates = dirs_by_name.get(name)
        if not candidates and " " in name:
            # Maven appends the version when a module's differs from the root's.
            candidates = dirs_by_name.get(name.rsplit(" ", 1)[0])
        if not candidates or len(candidates) != 1:
            return None
        module = next(iter(candidates))
        test_root = repo / module / "src/test/java"
        classes: Set[str] = set()
        for path in sorted(test_root.rglob("*.java")) if test_root.is_dir() else []:
            if not pattern.match(path.stem):
                continue
            fqn = _java_fqn_from_path(path.relative_to(repo).as_posix(), "src/test/java/")
            if fqn:
                classes.add(fqn)
        modules[module] = classes
    return modules


def _module_dirs_by_display_name(repo: Path) -> Dict[str, Set[str]]:
    """Module directories keyed by the name Maven prints: `<name>`, else `artifactId`."""
    dirs: Dict[str, Set[str]] = {}
    for current, subdirs, files in os.walk(repo):
        subdirs[:] = [d for d in subdirs if d not in _NON_MODULE_DIRS and not d.startswith(".")]
        if "pom.xml" not in files:
            continue
        try:
            root = ET.parse(Path(current) / "pom.xml").getroot()
        except (ET.ParseError, OSError):
            continue
        artifact_id = _child_text(root, "artifactId")
        display = _child_text(root, "name").replace("${project.artifactId}", artifact_id) or artifact_id
        if not display or "${" in display:
            continue
        module = Path(current).relative_to(repo).as_posix()
        dirs.setdefault(display, set()).add("" if module == "." else module)
    return dirs


def _java_fqn_from_path(path_text: str, source_root: str) -> Optional[str]:
    if source_root not in path_text or not path_text.endswith(".java"):
        return None
    package_path = path_text.split(source_root, 1)[1][:-5]
    return package_path.replace("/", ".").strip(".")


def _changed_production_java_files(changed_java: Optional[List[str]]) -> Optional[List[str]]:
    if changed_java is None:
        return None
    return [item for item in changed_java if "src/main/java/" in item]



def _explicit_target_sources(cmd: Sequence[str]) -> List[str]:
    """The sources the caller pinned with `-Dtest.enforcement.targetSource(s)=`."""
    sources: List[str] = []
    for item in cmd:
        text = str(item)
        for prefix in ("-Dtest.enforcement.targetSources=", "-Dtest.enforcement.targetSource="):
            if text.startswith(prefix):
                sources.extend(
                    value.strip() for value in text[len(prefix):].split(",") if value.strip()
                )
                break
    return list(dict.fromkeys(sources))


def _changed_files_matching(changed: Sequence[str], explicit: Sequence[str]) -> List[str]:
    """Changed files the caller pinned.

    The two lists can be written against different roots -- the command may
    carry a module-relative path where the diff is repo-relative -- so a plain
    equality test would silently match nothing. Compare by path suffix in both
    directions and keep the diff's own spelling.
    """
    wanted = [value.replace("\\", "/").lstrip("./") for value in explicit]
    matched: List[str] = []
    for path_text in changed:
        normalized = str(path_text).replace("\\", "/")
        if any(normalized.endswith(value) or value.endswith(normalized) for value in wanted):
            matched.append(path_text)
    return matched

def _with_ci_gate_properties(
    cmd: List[str], *, coverage_gate: float, mutation_gate: float
) -> List[str]:
    updated = _without_maven_property(cmd, JAVA_CI_COVERAGE_GATE_PROPERTY)
    updated = _without_maven_property(updated, JAVA_CI_MUTATION_GATE_PROPERTY)
    updated.append(f"-D{JAVA_CI_COVERAGE_GATE_PROPERTY}={_coverage_gate_ratio_value(coverage_gate)}")
    updated.append(f"-D{JAVA_CI_MUTATION_GATE_PROPERTY}={_gate_percent_value(mutation_gate)}")
    return updated


def _coverage_gate_ratio_value(coverage_gate: float) -> str:
    ratio = coverage_gate / 100.0 if coverage_gate > 1.0 else coverage_gate
    return f"{ratio:.4f}".rstrip("0").rstrip(".")


def _changed_java_files(repo_path: Path, base_ref: str) -> Optional[List[str]]:
    from uta.shared.git import git

    completed = git(timeout=30).run(
        repo_path,
        "diff",
        "--name-only",
        f"{base_ref}...HEAD",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return None
    return [
        item.strip()
        for item in completed.stdout.splitlines()
        if item.strip().endswith(".java")
    ]


__all__ = [
    "JAVA_CI_COVERAGE_GATE_PROPERTY",
    "JAVA_CI_MUTATION_GATE_PROPERTY",
    "_changed_java_files",
    "_changed_production_java_files",
    "_coverage_gate_ratio_value",
    "_gate_percent_value",
    "_java_code_without_comments_or_strings",
    "_java_fqn_from_path",
    "_java_test_paths_for_fqn",
    "_java_test_targets_type",
    "_maven_module_for_path",
    "_maven_modules_for_changed_files",
    "_pitest_target_tests",
    "_replace_maven_goals",
    "_surefire_test_selector",
    "_validate_command",
    "_with_changed_modules",
    "_with_ci_gate_properties",
    "_is_excludable_test_class",
    "_with_selected_test_classes",
    "_with_selected_test_file",
    "_with_target_sources",
    "_with_target_tests",
    "_without_maven_project_selector",
    "_without_maven_property",
    "_without_maven_reactor_dependency_selectors",
]
