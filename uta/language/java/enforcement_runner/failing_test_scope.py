"""Deciding which failing tests the change is answerable for.

The enforcement run tolerates a red suite (`-Dmaven.test.failure.ignore=true`),
which is what lets a module with legacy rot reach PIT at all. The price is that
the two gates then judge different test sets: JaCoCo credits the lines a failing
test executed before it failed, while PIT drops those tests entirely.

This module names the set that leaves both stages. A failing test class is the
change's business when the diff touched it, when it references a changed
production class, or when UTA generated it in this task -- otherwise it is taken
to have been red already and is excluded from both. That is an estimate, not a
measurement: a test broken through Spring/Dubbo wiring names nothing and is
touched by nothing, so it can be excluded wrongly. ADR-015 records why the
measured alternative was rejected, and why every excluded class is named in the
report.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import AbstractSet, Mapping, Optional, Sequence, Tuple

from uta.language.java.enforcement_runner.planning import (
    _is_excludable_test_class,
    _java_fqn_from_path,
    _java_test_paths_for_fqn,
    _java_test_targets_type,
)


@dataclass(frozen=True)
class FailingTestPartition:
    """What the run failed on, split by whether the change is answerable for it."""

    excluded: Tuple[str, ...] = ()
    collateral: Tuple[str, ...] = ()
    retained: Tuple[str, ...] = ()
    #: Empty on a normal partition; otherwise why `excluded` is empty. Reaches
    #: the report, so "nothing to exclude" is never confused with "could not tell".
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.all_excluded)

    @property
    def all_excluded(self) -> Tuple[str, ...]:
        return self.excluded + self.collateral


def partition_failing_tests(
    failing_test_classes: Sequence[str],
    *,
    repo_path: Path,
    changed_java: Optional[Sequence[str]],
    changed_production_java: Optional[Sequence[str]],
    generated_test_classes: Sequence[str] = (),
) -> FailingTestPartition:
    if not failing_test_classes:
        return FailingTestPartition(reason="no-failures")
    if changed_java is None:
        # No diff, so relatedness cannot be evaluated at all. Excluding on that
        # basis would be a guess about which the report could say nothing.
        return FailingTestPartition(
            retained=tuple(failing_test_classes), reason="no-diff-available"
        )

    changed_test_fqns = {
        fqn
        for fqn in (
            _java_fqn_from_path(path_text, "src/test/java/")
            for path_text in changed_java
            if "src/test/java/" in path_text and path_text.endswith(".java")
        )
        if fqn
    }
    generated = {fqn.strip() for fqn in generated_test_classes if fqn and fqn.strip()}
    changed_simple_names = [
        Path(path_text).stem for path_text in (changed_production_java or [])
    ]

    excluded: list[str] = []
    retained: list[str] = []
    for test_fqn in failing_test_classes:
        fqn = (test_fqn or "").strip()
        if not fqn:
            continue
        if not _is_excludable_test_class(fqn):
            # Naming it as excluded would be a promise the command line cannot
            # keep, and the report would then describe a run that never happened.
            retained.append(fqn)
            continue
        if fqn in changed_test_fqns or fqn in generated:
            retained.append(fqn)
            continue
        if _references_any(repo_path, fqn, changed_simple_names):
            retained.append(fqn)
            continue
        excluded.append(fqn)

    if not excluded:
        return FailingTestPartition(
            retained=tuple(retained), reason="all-failures-related"
        )
    return FailingTestPartition(excluded=tuple(excluded), retained=tuple(retained))


def _without_exclusions(partition: FailingTestPartition, reason: str) -> FailingTestPartition:
    """Take the exclusion back, keeping every class named as retained.

    The evidence has to describe the run that happened, so a refused exclusion
    reports its classes as retained and says why -- never as excluded.
    """
    return replace(
        partition,
        excluded=(),
        collateral=(),
        retained=partition.retained + partition.excluded,
        reason=reason,
    )


def _expand_colliding_simple_name_exclusions(
    partition: FailingTestPartition,
    classes_by_module: Mapping[str, AbstractSet[str]],
) -> FailingTestPartition:
    """Exclude every test represented by an ambiguous Surefire simple name.

    `-Dtest` speaks simple names in both dialects -- `!FooTest` to drop one and
    `FooTest` to keep one -- so two classes named `FooTest` in different packages
    are one word to Surefire. If one is an unrelated baseline failure, exclude
    every class represented by that word. This deliberately gives up coverage
    contributed by the colliding green class instead of retaining the red one.
    """
    if not partition.all_excluded:
        return partition

    excluded = set(partition.excluded)
    known: set[str] = set()
    for module_classes in classes_by_module.values():
        known |= set(module_classes)
    by_simple_name = _classes_by_simple_name(known)
    colliding_simple = {
        name
        for name in {fqn.rsplit(".", 1)[-1] for fqn in excluded}
        if len(by_simple_name.get(name, ())) > 1
    }
    expanded: set[str] = set()
    for name in colliding_simple:
        expanded.update(by_simple_name[name])
    added = sorted(expanded - excluded)
    if not added:
        return partition
    return replace(
        partition,
        collateral=partition.collateral + tuple(added),
        retained=tuple(fqn for fqn in partition.retained if fqn not in expanded),
        reason="simple-name-collision-expanded",
    )


def _without_module_emptying_exclusions(
    partition: FailingTestPartition,
    classes_by_module: Mapping[str, AbstractSet[str]],
) -> FailingTestPartition:
    """Keep every exclusion that would leave its module with no tests at all.

    Maven applies `-Dtest` to the whole reactor, so this has to be judged per
    module rather than repo-wide: a module whose only test is excluded runs
    nothing, Surefire aborts it with `No tests were executed!`, and every module
    ordered after it is skipped -- so the gates never report. PIT has the same
    objection from the other side (`failWhenNoMutations`), and an empty Surefire
    selection would pass vacuously anyway.

    A module that would be emptied keeps all of its failing tests, which is
    exactly the pre-exclusion behaviour for that module and nothing worse. The
    other modules still get the benefit. Only when that leaves nothing to
    exclude does the whole re-run fall away, and then the reason says so.
    """
    if not partition.all_excluded:
        return partition

    excluded = set(partition.all_excluded)
    known = {
        fqn
        for module_classes in classes_by_module.values()
        for fqn in module_classes
    }
    by_simple_name = _classes_by_simple_name(known)
    forced_collision_names = {
        name
        for name, fqns in by_simple_name.items()
        if len(fqns) > 1 and fqns <= excluded
    }

    protected: set[str] = set()
    for module_classes in classes_by_module.values():
        if module_classes and not (set(module_classes) - excluded):
            protected |= {
                fqn
                for fqn in set(module_classes) & excluded
                if fqn.rsplit(".", 1)[-1] not in forced_collision_names
            }
    if not protected:
        return partition

    kept = tuple(fqn for fqn in partition.excluded if fqn not in protected)
    collateral = tuple(
        fqn for fqn in partition.collateral if fqn not in protected
    )
    retained = partition.retained + tuple(
        fqn for fqn in partition.excluded if fqn in protected
    )
    if not kept and not collateral:
        return replace(
            partition,
            excluded=(),
            collateral=(),
            retained=retained,
            reason="would-empty-module-suite",
        )
    return replace(
        partition,
        excluded=kept,
        collateral=collateral,
        retained=retained,
    )


def _classes_by_simple_name(classes: AbstractSet[str]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for fqn in classes:
        grouped.setdefault(fqn.rsplit(".", 1)[-1], set()).add(fqn)
    return grouped


def _references_any(repo_path: Path, test_fqn: str, changed_simple_names: Sequence[str]) -> bool:
    """Does this failing test name any changed production type?

    Reuses the relation `_pitest_target_tests` already trusts, so "related" means
    the same thing everywhere in the runner: a construction, class literal,
    inheritance, typed declaration, or a matching test name -- never a comment or
    an arbitrary string.
    """
    if not changed_simple_names:
        return False
    for test_path in _java_test_paths_for_fqn(repo_path, test_fqn):
        absolute = Path(repo_path) / test_path
        for simple_name in changed_simple_names:
            if _java_test_targets_type(absolute, simple_name):
                return True
    return False


__all__ = [
    "FailingTestPartition",
    "partition_failing_tests",
    "_expand_colliding_simple_name_exclusions",
    "_without_exclusions",
    "_without_module_emptying_exclusions",
]
