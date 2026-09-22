#!/usr/bin/env python3
"""Read every import in the tree and say what kind of dependency it is.

This exists because `uta`'s package graph cycles, and the cycles are invisible
at startup: they are held open by function-local imports that only fail when
somebody calls the function. A checker that looked at runtime imports would
report a clean tree. So this one parses instead, and records three kinds of
edge separately:

* **eager** -- runs when the module is imported, including inside `try` and
  inside `if sys.version_info`. Anything at module scope that is not the
  type-checking guard runs.
* **lazy** -- inside a function, method, or lambda body. Still a dependency;
  it just fails at the first call rather than at startup. Not forgiven.
* **type_only** -- under `if TYPE_CHECKING:`. Nothing executes, so it cannot
  cause an import cycle, though it still shows ownership.

Nothing here imports the code it reads. A checker with side effects is not a
gate -- and half of what it scans would refuse to import anyway, since the
distributed tree is meant to run without `uta` on the path.

Standard library only, by design: this runs in CI before dependencies are
guaranteed, and `ast` is the whole requirement.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


DEFAULT_ROOTS = ("uta", "tools/python-enforcement")

AGENT_CORE_PREFIX = "agent_core"

#: A dotted module path -- at least two segments, so a bare word or a sentence
#: fragment is not mistaken for a module.
_MODULE_TARGET = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


class ScanError(RuntimeError):
    """A file could not be parsed.

    Raised rather than skipped: a file the checker cannot read is a file whose
    edges nobody is checking, which is exactly where a banned import would
    survive.
    """


@dataclass(frozen=True)
class ImportEdge:
    importer: str
    imported: str
    line: int
    kind: str      # eager | lazy | type_only
    origin: str    # internal | agent_core | third_party | stdlib
    wildcard: bool
    #: Reached through `importlib.import_module` / `__import__` rather than an
    #: import statement. Still an edge -- the runtime dependency is identical,
    #: only the spelling hides it from a walker that looks at Import nodes.
    dynamic: bool = False
    #: A dynamic import whose module name is not a literal, so it cannot be
    #: resolved. Recorded so a human can look, never silently dropped.
    unresolved: bool = False

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "importer": self.importer,
            "imported": self.imported,
            "line": self.line,
            "kind": self.kind,
            "origin": self.origin,
            "wildcard": self.wildcard,
        }
        if self.dynamic:
            payload["dynamic"] = True
        if self.unresolved:
            payload["unresolved"] = True
        return payload

    def sort_key(self) -> Tuple[str, str, int]:
        return (self.importer, self.imported, self.line)


# -- classification ----------------------------------------------------------


def _is_type_checking_test(node: ast.expr) -> bool:
    """`if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:`, and nothing else.

    Deliberately narrow. `if sys.version_info >= (3, 11): import x` really does
    import x at module load, and treating it as type-only would hide a live
    dependency.
    """
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    if isinstance(node, ast.Attribute):
        return node.attr == "TYPE_CHECKING"
    return False


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, package: str, internal_tops: Set[str]) -> None:
        self.module = module
        # The package a relative import counts back from. For `pkg/sub/a.py`
        # that is `pkg.sub`; for `pkg/sub/__init__.py` it is `pkg.sub` too.
        self.package_parts = package.split(".") if package else []
        self.internal_tops = internal_tops
        self.edges: List[ImportEdge] = []
        #: `(module_path, line)` from `"module.path:Attribute"` literals,
        #: kept until `scan_tree` can confirm the module is real.
        self.registry_targets: List[Tuple[str, int]] = []
        # Outermost context wins: a function nested inside a TYPE_CHECKING block
        # never runs, so its imports stay type-only.
        self._context = "eager"

    # -- context tracking

    def _in(self, context: str, body: Iterable[ast.stmt]) -> None:
        previous = self._context
        if previous == "eager":
            self._context = context
        for statement in body:
            self.visit(statement)
        self._context = previous

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_test(node.test):
            self._in("type_only", node.body)
            # The `else` of a TYPE_CHECKING guard is the runtime branch.
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._in("lazy", node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._in("lazy", node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._in("lazy", [])

    # -- imports

    #: The two ways to import by name at runtime.
    _DYNAMIC_NAMES = {"import_module", "__import__"}

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.attr if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name)
            else ""
        )
        if name in self._DYNAMIC_NAMES and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self._record(first.value, node.lineno, wildcard=False, dynamic=True)
            else:
                # Unresolvable, and therefore reported rather than dropped: a
                # name the checker cannot read is where a banned import lives.
                self.edges.append(
                    ImportEdge(
                        importer=self.module,
                        imported="<dynamic>",
                        line=node.lineno,
                        kind=self._context,
                        origin="unknown",
                        wildcard=False,
                        dynamic=True,
                        unresolved=True,
                    )
                )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """A `"module.path:Attribute"` string is a dependency written sideways.

        A registry that maps keys to import targets and resolves them with
        `import_module` creates exactly the edges an import statement would.
        Recording them as `lazy` is honest: they resolve on first use, so they
        cannot cause a startup cycle, but they are real and a rule about
        direction must see them.

        The shape has to be specific or every dictionary of prose becomes a
        graph, so this requires a dotted module path, a colon, an identifier --
        and, at `scan_tree`, that the module actually exists in the tree.
        """
        if not isinstance(node.value, str) or ":" not in node.value:
            return
        module_path, _, attribute = node.value.partition(":")
        if not _MODULE_TARGET.fullmatch(module_path) or not attribute.isidentifier():
            return
        self.registry_targets.append((module_path, node.lineno))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, node.lineno, wildcard=False)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self._resolve_relative(node.module, node.level)
        if base is None:
            return
        for alias in node.names:
            if alias.name == "*":
                self._record(base, node.lineno, wildcard=True)
                continue
            # `from pkg import thing` may import a module or a name. Both make
            # pkg a dependency, and `pkg.thing` is only an edge if it is a
            # module -- which the caller resolves against the scanned files.
            self._record(base, node.lineno, wildcard=False, candidate=alias.name)

    def _resolve_relative(self, module: Optional[str], level: int) -> Optional[str]:
        if not level:
            return module
        if level > len(self.package_parts):
            return module
        prefix = self.package_parts[: len(self.package_parts) - level + 1]
        parts = list(prefix) + ([module] if module else [])
        return ".".join(part for part in parts if part) or None

    def _record(
        self,
        name: str,
        line: int,
        *,
        wildcard: bool,
        candidate: Optional[str] = None,
        dynamic: bool = False,
    ) -> None:
        self.edges.append(
            ImportEdge(
                importer=self.module,
                imported=name,
                line=line,
                kind=self._context,
                origin=_origin(name, self.internal_tops),
                wildcard=wildcard,
                dynamic=dynamic,
            )
        )
        if candidate:
            # Recorded provisionally; `scan_tree` keeps it only if the dotted
            # form is a real module in the tree.
            self.edges.append(
                ImportEdge(
                    importer=self.module,
                    imported=f"{name}.{candidate}",
                    line=line,
                    kind=self._context,
                    origin=_origin(name, self.internal_tops),
                    wildcard=False,
                )
            )


def _origin(name: str, internal_tops: Set[str]) -> str:
    """Internal means "a package this scan actually found", not a name list.

    Deriving it from the tree keeps a stale prefix constant from quietly
    reclassifying a package as third-party the day somebody renames it.
    """
    top = name.split(".")[0]
    if top in internal_tops:
        return "internal"
    if top == AGENT_CORE_PREFIX:
        return "agent_core"
    if top in sys.stdlib_module_names:
        return "stdlib"
    return "third_party"


# -- scanning ----------------------------------------------------------------


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_files(root: Path) -> List[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and ".venv" not in path.parts
    )


def scan_tree(root: Path, *, relative_to: Optional[Path] = None) -> List[ImportEdge]:
    """Every import edge under `root`, sorted deterministically.

    `relative_to` names the directory module paths are computed from, which
    differs from `root` when scanning `uta/` inside the repository: the module
    is `uta.app.cli`, not `app.cli`.
    """
    root = Path(root)
    base = Path(relative_to) if relative_to else root

    modules: Set[str] = set()
    parsed: List[Tuple[str, str, ast.Module]] = []
    for path in _python_files(root):
        module = _module_name(path, base)
        modules.add(module)
        package = module if path.name == "__init__.py" else ".".join(module.split(".")[:-1])
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            raise ScanError(f"{path.relative_to(base).as_posix()}: {exc.msg}") from exc
        parsed.append((module, package, tree))

    internal_tops = {name.split(".")[0] for name in modules if name}

    edges: List[ImportEdge] = []
    registry: List[Tuple[str, str, int]] = []
    for module, package, tree in parsed:
        collector = _Collector(module, package, internal_tops)
        for statement in tree.body:
            collector.visit(statement)
        edges.extend(collector.edges)
        registry.extend((module, target, line) for target, line in collector.registry_targets)

    # Only now, with every module known, can a registry string be told from a
    # sentence that happens to contain a colon.
    for importer, target, line in registry:
        if target in modules:
            edges.append(
                ImportEdge(
                    importer=importer,
                    imported=target,
                    line=line,
                    kind="lazy",
                    origin=_origin(target, internal_tops),
                    wildcard=False,
                    dynamic=True,
                )
            )

    return _dedupe(edges, modules)


def unresolved_dynamic_imports(edges: Iterable[ImportEdge]) -> List[str]:
    """Dynamic imports whose module name is computed, as `module:line`.

    These cannot be resolved to an edge, so they are the one place the scan
    admits it does not know. Surfaced for a human rather than dropped.
    """
    return sorted(
        f"{edge.importer}:{edge.line}" for edge in edges if edge.unresolved
    )


def _dedupe(edges: Iterable[ImportEdge], modules: Set[str]) -> List[ImportEdge]:
    """Drop provisional `pkg.name` edges that are names, not modules.

    `from pkg import Thing` produces both `pkg` and `pkg.Thing`; only one of
    them is real, and which one depends on whether `pkg/Thing.py` exists.
    """
    kept: Dict[Tuple[str, str, int], ImportEdge] = {}
    by_line: Dict[Tuple[str, int], List[ImportEdge]] = {}
    for edge in edges:
        # Unresolved placeholders are not edges to anywhere; they ride along so
        # `unresolved_dynamic_imports` can report them.
        if edge.unresolved:
            kept[edge.sort_key()] = edge
            continue
        by_line.setdefault((edge.importer, edge.line), []).append(edge)

    for group in by_line.values():
        specific = [edge for edge in group if edge.imported in modules and "." in edge.imported]
        chosen = specific or [edge for edge in group if edge.imported.count(".") == min(e.imported.count(".") for e in group)]
        for edge in chosen:
            kept[edge.sort_key()] = edge

    return sorted(kept.values(), key=ImportEdge.sort_key)


def scan_default_roots(repo_root: Path) -> List[ImportEdge]:
    """The two trees the design names: `uta/` and the distributed client."""
    repo_root = Path(repo_root)
    edges: List[ImportEdge] = []
    for relative in DEFAULT_ROOTS:
        root = repo_root / relative
        if not root.exists():
            continue
        # The distributed tree is its own import root: `uta_py_enforce`, not
        # `tools.python_enforcement.uta_py_enforce`.
        base = root if relative != "uta" else repo_root
        edges.extend(scan_tree(root, relative_to=base))

    # Origin is computed per tree, so a `uta` edge seen while scanning the
    # tools tree would come back third-party. Re-derive it across both.
    internal_tops = {edge.importer.split(".")[0] for edge in edges}
    edges = [
        edge if edge.origin != "third_party" or edge.imported.split(".")[0] not in internal_tops
        else ImportEdge(edge.importer, edge.imported, edge.line, edge.kind, "internal", edge.wildcard)
        for edge in edges
    ]
    return sorted(edges, key=ImportEdge.sort_key)


# -- cycles ------------------------------------------------------------------


def _prefix(module: str, depth: int) -> str:
    return ".".join(module.split(".")[:depth])


def find_cycles(edges: Iterable[ImportEdge], *, depth: int = 1) -> List[List[str]]:
    """Cycles among package prefixes at `depth`, ignoring type-only edges.

    Lazy edges count. A cycle held open by a function-local import is still a
    cycle -- it is the kind this tree actually has.
    """
    graph: Dict[str, Set[str]] = {}
    for edge in edges:
        if edge.kind == "type_only" or edge.origin != "internal" or edge.unresolved:
            continue
        source = _prefix(edge.importer, depth)
        target = _prefix(edge.imported, depth)
        if source == target:
            continue
        graph.setdefault(source, set()).add(target)
        graph.setdefault(target, set())

    cycles: List[List[str]] = []
    seen: Set[frozenset] = set()
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    counter = [0]

    def strongconnect(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for neighbour in sorted(graph.get(node, ())):
            if neighbour not in index:
                strongconnect(neighbour)
                low[node] = min(low[node], low[neighbour])
            elif neighbour in on_stack:
                low[node] = min(low[node], index[neighbour])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                key = frozenset(component)
                if key not in seen:
                    seen.add(key)
                    cycles.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            strongconnect(node)

    return sorted(cycles)


# -- rendering ---------------------------------------------------------------


def render_graph(edges: Iterable[ImportEdge]) -> str:
    lines = sorted(
        f"{edge.importer} -> {edge.imported} [{edge.kind}, {edge.origin}"
        f"{', wildcard' if edge.wildcard else ''}] :{edge.line}"
        for edge in edges
    )
    return "\n".join(lines) + ("\n" if lines else "")


def render_json(edges: Iterable[ImportEdge]) -> str:
    ordered = sorted(edges, key=ImportEdge.sort_key)
    return json.dumps({"edges": [edge.as_dict() for edge in ordered]}, indent=2, sort_keys=False)


# -- command -----------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", default=".", help="repository root to scan")
    parser.add_argument("--graph", action="store_true", help="print the text graph")
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument("--cycles", action="store_true", help="print internal package cycles")
    parser.add_argument("--check", action="store_true", help="fail on any prohibited edge")
    parser.add_argument("--depth", type=int, default=2, help="package depth for cycle detection")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    try:
        edges = scan_default_roots(repo_root)
    except ScanError as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 2

    if args.check:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import package_dependency_policy as policy

        # All three things the architecture criterion names, not just the
        # first. Running one of three and printing a green line is how a gate
        # gets quoted for work it never did.
        violations = policy.check(edges)
        cycles = policy.unapproved_cycles(edges)
        harness = policy.find_concrete_harness_names(repo_root / "uta", relative_to=repo_root)
        stale = (
            [f"edge exception matches nothing: {item}" for item in policy.stale_exceptions(edges)]
            + [f"cycle exception matches nothing: {item}" for item in policy.stale_cycle_exceptions(edges)]
            + [f"harness exception names a missing file: {item}" for item in policy.stale_harness_exceptions(repo_root)]
        )

        if violations:
            print(policy.render_violations(violations), end="", file=sys.stderr)
        for cycle in cycles:
            print(f"unapproved package cycle: {' <-> '.join(cycle)}", file=sys.stderr)
        for hit in harness:
            print(f"concrete harness name: {hit.path}:{hit.line} {hit.name}", file=sys.stderr)
        for entry in stale:
            print(f"stale: {entry}", file=sys.stderr)

        if violations or cycles or harness or stale:
            return 1
        print(
            f"clean: no prohibited edges, no unapproved cycles "
            f"({len(policy.CYCLE_EXCEPTIONS)} recorded), no concrete harness names"
        )
        return 0

    if args.json:
        print(render_json(edges))
    if args.cycles:
        for cycle in find_cycles(edges, depth=args.depth):
            print(" <-> ".join(cycle))
    if args.graph or not (args.json or args.cycles or args.check):
        print(render_graph(edges), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
