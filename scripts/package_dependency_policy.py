#!/usr/bin/env python3
"""Which import edges this repository allows, as data rather than assertions.

The scanner next door reports what the edges *are*. This says which of them are
permitted, and it is deliberately a table you can read in one sitting: rules
scattered across a test file drift, and nobody can answer "what is the
architecture?" by reading them.

Three things are load-bearing here.

**Lazy is not a loophole.** A function-local import inverts a dependency just
as thoroughly as a module-level one; it only fails later. Rules apply to eager
and lazy edges alike. Type-only edges are exempt because nothing executes --
they show ownership, not runtime direction.

**Exceptions are exact.** Every entry names one importer and one imported
module, plus an owner, a reason, and the milestone that deletes it. A
package-level exemption is how a temporary seam quietly becomes the
architecture, so `check` refuses to honour one. `stale_exceptions` reports
entries that no longer match anything, because expired seams left in the table
are how it stops being readable.

**One diagnostic is exempt by name.** `uta assess` reads OpenCode's own
database on purpose (ADR-013). The exemption is two named files, and the rule
that makes it safe -- one importer, none of them in a runtime lane -- is
checked rather than asserted in prose.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    name: str
    describes: str
    applies: Callable[[str, str, str], bool]  # importer, imported, origin


def _under(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


RULES: Tuple[Rule, ...] = (
    Rule(
        "tasks-imports-testgen",
        "task persistence stores records; it does not know the workflow that produced them",
        lambda importer, imported, origin: _under(importer, "uta.tasks") and _under(imported, "uta.testgen"),
    ),
    Rule(
        "tasks-imports-agent-core",
        "task persistence receives neutral product records, never agent-core types",
        lambda importer, imported, origin: _under(importer, "uta.tasks") and origin == "agent_core",
    ),
    Rule(
        "testgen-imports-enforcement-binding",
        "testgen reaches enforcement through the neutral contract, never a binding",
        lambda importer, imported, origin: _under(importer, "uta.testgen") and _under(imported, "uta.enforcement.bindings"),
    ),
    Rule(
        "testgen-imports-language-binding",
        "app selects and injects the generation binding (ADR-012 amendment)",
        lambda importer, imported, origin: _under(importer, "uta.testgen") and _under(imported, "uta.language"),
    ),
    Rule(
        "contract-imports-product",
        "the sole enforcement contract depends on nothing above it",
        lambda importer, imported, origin: _under(importer, "uta_enforce_core")
        and (_under(imported, "uta") or origin == "agent_core" or _under(imported, "uta_py_enforce")),
    ),
    Rule(
        "distributed-imports-product",
        "the sparse-checkout client runs without UTA or agent-core installed",
        lambda importer, imported, origin: _under(importer, "uta_py_enforce")
        and (_under(imported, "uta") or origin == "agent_core"),
    ),
    Rule(
        "binding-imports-app",
        "a binding does not import the composition root that built it",
        lambda importer, imported, origin: (
            _under(importer, "uta.enforcement.bindings") or _under(importer, "uta.language")
        )
        and _under(imported, "uta.app"),
    ),
)

#: Rule name used for wildcard re-exports, which is a property of the edge
#: rather than of its direction.
WILDCARD_RULE = "wildcard-re-export"


# --------------------------------------------------------------------------
# Exceptions -- exact, owned, dated
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Exception_:
    rule: str
    importer: str
    imported: str
    owner: str
    reason: str
    delete_by: str


#: All baseline package exceptions have been eliminated; the tree has zero exceptions.
EXCEPTIONS: Tuple[Exception_, ...] = ()


#: Names that mean a concrete harness rather than the neutral one.
CONCRETE_HARNESS_NAMES = (
    "OpenCodeClient",
    "OpenCodeAuthClient",
    "OpenCodeProcess",
    "generate_opencode_config",
)

#: Files still naming a concrete harness, deleted in Slice 7 when the neutral
#: lifecycle API lands. Listed by path so a *new* file naming one still fails.
#: Empty, and meant to stay that way. Every entry here was a migration seam for
#: a call site that reached past the neutral harness API; all are gone. A new
#: entry means a new one appeared, which is a decision, not a detail.
CONCRETE_HARNESS_EXCEPTIONS: Dict[str, str] = {}


@dataclass(frozen=True)
class CycleException:
    """One package cycle that is known, owned, and scheduled for deletion."""

    members: Tuple[str, ...]
    owner: str
    reason: str
    delete_by: str


#: The cycles the tree still has. Recorded the same way prohibited edges are:
#: exactly, with an owner and the slice that removes them. A cycle absent from
#: this list fails the gate.
#: Empty, and the tree currently has no cycle at either judged depth.
#:
#: It held a five-package, thirty-four-module component until the composition
#: table moved out of `uta.shared`. What remained after that were three edges
#: pointing from enforcement into language while thirteen pointed correctly the
#: other way; all three are now resolved through composition rather than
#: imported. A new entry here means a new cycle, which is a decision.
CYCLE_EXCEPTIONS: Tuple[CycleException, ...] = (
)

#: Depths the gate judges cycles at. Package level catches layering inversions;
#: module level catches the pairs that hide inside one package.
CYCLE_DEPTHS = (2, 3)


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    rule: str
    importer: str
    imported: str
    line: int
    describes: str

    def location(self) -> str:
        return f"{self.importer.replace('.', '/')}.py:{self.line}"


@dataclass(frozen=True)
class HarnessNameHit:
    path: str
    name: str
    line: int


def _exception_keys() -> set:
    return {(item.rule, item.importer, item.imported) for item in EXCEPTIONS}


def check(edges: Iterable) -> List[Violation]:
    """Every edge that breaks a rule and is not a recorded exception."""
    allowed = _exception_keys()
    violations: List[Violation] = []

    for edge in edges:
        # Type-only edges execute nothing, so they cannot invert a runtime
        # dependency. Every other kind -- lazy and dynamic included -- is judged.
        if edge.kind == "type_only" or edge.unresolved:
            continue

        if edge.wildcard and edge.origin == "internal":
            if (WILDCARD_RULE, edge.importer, edge.imported) not in allowed:
                violations.append(
                    Violation(WILDCARD_RULE, edge.importer, edge.imported, edge.line,
                              "wildcard re-exports make the public surface accidental")
                )

        for rule in RULES:
            if not rule.applies(edge.importer, edge.imported, edge.origin):
                continue
            if (rule.name, edge.importer, edge.imported) in allowed:
                continue
            violations.append(
                Violation(rule.name, edge.importer, edge.imported, edge.line, rule.describes)
            )

    return sorted(violations, key=lambda v: (v.importer, v.line, v.rule))


def stale_exceptions(edges: Iterable) -> List[str]:
    """Recorded exceptions that no longer match any edge.

    An expired seam left in the table is how the table stops being readable.
    """
    seen = set()
    for edge in edges:
        if edge.kind == "type_only" or edge.unresolved:
            continue
        if edge.wildcard and edge.origin == "internal":
            seen.add((WILDCARD_RULE, edge.importer, edge.imported))
        for rule in RULES:
            if rule.applies(edge.importer, edge.imported, edge.origin):
                seen.add((rule.name, edge.importer, edge.imported))

    return sorted(
        f"{item.rule}: {item.importer} -> {item.imported}"
        for item in EXCEPTIONS
        if (item.rule, item.importer, item.imported) not in seen
    )


def render_violations(violations: Sequence[Violation]) -> str:
    if not violations:
        return "no prohibited package edges\n"
    lines = ["prohibited package edges:"]
    for violation in violations:
        lines.append(f"  {violation.location()}  {violation.rule}")
        lines.append(f"      imports {violation.imported}")
        lines.append(f"      {violation.describes}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Concrete harness names
# --------------------------------------------------------------------------


def find_concrete_harness_names(root: Path, *, relative_to: Optional[Path] = None) -> List[HarnessNameHit]:
    """Concrete harness identifiers in real code, ignoring prose.

    Matching the substring would ban explaining why the ban exists, so this
    reads identifiers out of the parse tree rather than grepping.
    """
    root = Path(root)
    base = Path(relative_to) if relative_to else root
    hits: List[HarnessNameHit] = []

    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(base).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            found: List[Tuple[str, int]] = []
            if isinstance(node, ast.ImportFrom):
                found = [(alias.name, node.lineno) for alias in node.names if alias.name in CONCRETE_HARNESS_NAMES]
            elif isinstance(node, ast.Name) and node.id in CONCRETE_HARNESS_NAMES:
                found = [(node.id, node.lineno)]
            elif isinstance(node, ast.Attribute) and node.attr in CONCRETE_HARNESS_NAMES:
                found = [(node.attr, node.lineno)]
            for name, line in found:
                hits.append(HarnessNameHit(relative, name, line))

    return [hit for hit in hits if hit.path not in CONCRETE_HARNESS_EXCEPTIONS]


def _cycle_key(members: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted(members))


def unapproved_cycles(edges: Iterable, *, depth: Optional[int] = None) -> List[List[str]]:
    """Cycles the tree has that no exception covers."""
    import check_package_dependencies as scanner

    approved = {_cycle_key(item.members) for item in CYCLE_EXCEPTIONS}
    depths = (depth,) if depth is not None else CYCLE_DEPTHS

    found: List[List[str]] = []
    seen = set()
    for level in depths:
        for cycle in scanner.find_cycles(edges, depth=level):
            key = _cycle_key(cycle)
            if key in approved or key in seen:
                continue
            seen.add(key)
            found.append(sorted(cycle))
    return sorted(found)


def stale_cycle_exceptions(edges: Iterable) -> List[str]:
    """Recorded cycles that no longer exist -- debt that was paid but not filed."""
    import check_package_dependencies as scanner

    live = set()
    for level in CYCLE_DEPTHS:
        for cycle in scanner.find_cycles(edges, depth=level):
            live.add(_cycle_key(cycle))

    return sorted(
        " <-> ".join(item.members)
        for item in CYCLE_EXCEPTIONS
        if _cycle_key(item.members) not in live
    )


def stale_harness_exceptions(repo_root: Path) -> List[str]:
    """Harness exemptions naming files that are not there any more."""
    repo_root = Path(repo_root)
    return sorted(
        path for path in CONCRETE_HARNESS_EXCEPTIONS if not (repo_root / path).exists()
    )
