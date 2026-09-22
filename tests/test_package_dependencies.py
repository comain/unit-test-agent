"""The scanner that makes the package architecture executable.

It answers one question about every import in the tree: does this edge exist,
and is it eager, deferred to call time, or type-only? The distinction is the
whole point. A function-local import is still a dependency -- it just fails at
the first call instead of at startup -- so the scanner records it rather than
forgiving it, and the policy layer built on top (Task 2) decides what to do.

Tests here run against fixture trees rather than the real one. A test that
asserted things about `uta/` would change meaning every time somebody moved a
module, which is the opposite of what a gate is for.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_package_dependencies.py"


def _scanner():
    """Loaded once. Reloading it per call would hand `pytest.raises` a
    different `ScanError` class than the one the scanner actually raises."""
    if "check_package_dependencies" in sys.modules:
        return sys.modules["check_package_dependencies"]
    spec = importlib.util.spec_from_file_location("check_package_dependencies", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(root: Path, relative: str, body: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.lstrip("\n"), encoding="utf-8")
    return path


def _edges(root: Path):
    return _scanner().scan_tree(root)


def _by_target(edges, module: str):
    return [edge for edge in edges if edge.imported == module]


# -- classification -----------------------------------------------------------


def test_a_module_level_import_is_eager(tmp_path):
    _write(tmp_path, "pkg/a.py", """
import pkg.b
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "eager"
    assert edge.importer == "pkg.a"
    assert edge.line == 1


def test_an_import_inside_a_function_is_lazy_not_absent(tmp_path):
    """The edge exists. It just fails later than an eager one."""
    _write(tmp_path, "pkg/a.py", """
def build():
    import pkg.b
    return pkg.b
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "lazy"
    assert edge.line == 2


def test_an_import_inside_a_method_is_lazy(tmp_path):
    _write(tmp_path, "pkg/a.py", """
class Builder:
    def build(self):
        from pkg import b
        return b
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "lazy"


def test_a_type_checking_import_is_type_only(tmp_path):
    _write(tmp_path, "pkg/a.py", """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pkg.b import Thing
""")
    _write(tmp_path, "pkg/b.py", "class Thing: pass")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "type_only"


def test_typing_dot_type_checking_is_recognised_too(tmp_path):
    """`if typing.TYPE_CHECKING:` is the same guard spelled differently."""
    _write(tmp_path, "pkg/a.py", """
import typing

if typing.TYPE_CHECKING:
    from pkg.b import Thing
""")
    _write(tmp_path, "pkg/b.py", "class Thing: pass")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "type_only"


def test_a_conditional_import_that_is_not_type_checking_stays_eager(tmp_path):
    """`if sys.version_info` runs at import time. Only TYPE_CHECKING does not."""
    _write(tmp_path, "pkg/a.py", """
import sys

if sys.version_info >= (3, 11):
    import pkg.b
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "eager"


def test_a_try_import_is_eager(tmp_path):
    _write(tmp_path, "pkg/a.py", """
try:
    import pkg.b
except ImportError:
    pkg = None
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "eager"


def test_a_function_inside_a_type_checking_block_is_still_type_only(tmp_path):
    """The outer guard wins; nothing under it executes at runtime."""
    _write(tmp_path, "pkg/a.py", """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    def helper():
        import pkg.b
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "type_only"


# -- what an edge records -----------------------------------------------------


def test_an_edge_records_importer_target_line_and_origin(tmp_path):
    _write(tmp_path, "pkg/a.py", """
import os
import pkg.b
import agent_core.git
import fastapi
""")
    _write(tmp_path, "pkg/b.py", "")

    origins = {edge.imported: edge.origin for edge in _edges(tmp_path)}
    assert origins["pkg.b"] == "internal"
    assert origins["agent_core.git"] == "agent_core"
    assert origins["fastapi"] == "third_party"
    assert origins["os"] == "stdlib"


def test_a_relative_import_resolves_to_its_absolute_module(tmp_path):
    """Otherwise `from . import b` looks like an edge to nowhere."""
    _write(tmp_path, "pkg/sub/a.py", """
from . import b
from .. import top
from .c import thing
""")
    _write(tmp_path, "pkg/sub/b.py", "")
    _write(tmp_path, "pkg/sub/c.py", "thing = 1")
    _write(tmp_path, "pkg/top.py", "")

    imported = {edge.imported for edge in _edges(tmp_path)}
    assert imported == {"pkg.sub.b", "pkg.top", "pkg.sub.c"}


def test_a_from_import_of_a_module_and_of_a_name_both_land_on_the_module(tmp_path):
    """`from pkg.b import Thing` depends on pkg.b whether Thing is a module or not."""
    _write(tmp_path, "pkg/a.py", """
from pkg.b import Thing
from pkg import c
""")
    _write(tmp_path, "pkg/b.py", "class Thing: pass")
    _write(tmp_path, "pkg/c.py", "")

    imported = {edge.imported for edge in _edges(tmp_path)}
    assert imported == {"pkg.b", "pkg.c"}


def test_a_wildcard_import_is_recorded_as_such(tmp_path):
    """The spec bans these; the scanner has to be able to see them."""
    _write(tmp_path, "pkg/a.py", """
from pkg.b import *
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.wildcard is True


def test_an_ordinary_import_is_not_a_wildcard(tmp_path):
    _write(tmp_path, "pkg/a.py", "from pkg.b import Thing")
    _write(tmp_path, "pkg/b.py", "class Thing: pass")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.wildcard is False


def test_a_file_that_does_not_parse_is_reported_not_skipped(tmp_path):
    """Silently skipping it would let someone hide an edge behind a syntax error."""
    _write(tmp_path, "pkg/a.py", "def broken(:\n")

    with pytest.raises(_scanner().ScanError) as excinfo:
        _edges(tmp_path)
    assert "pkg/a.py" in str(excinfo.value)


# -- cycles -------------------------------------------------------------------


def test_two_packages_importing_each_other_eagerly_is_a_cycle(tmp_path):
    _write(tmp_path, "alpha/__init__.py", "")
    _write(tmp_path, "alpha/a.py", "import beta.b")
    _write(tmp_path, "beta/__init__.py", "")
    _write(tmp_path, "beta/b.py", "import alpha.a")

    module = _scanner()
    cycles = module.find_cycles(_edges(tmp_path), depth=1)
    assert [sorted(cycle) for cycle in cycles] == [["alpha", "beta"]]


def test_a_cycle_hidden_behind_a_function_local_import_is_still_found(tmp_path):
    """This is the case the scanner exists for -- nothing crashes at startup."""
    _write(tmp_path, "alpha/__init__.py", "")
    _write(tmp_path, "alpha/a.py", "import beta.b")
    _write(tmp_path, "beta/__init__.py", "")
    _write(tmp_path, "beta/b.py", """
def late():
    import alpha.a
    return alpha.a
""")

    module = _scanner()
    assert module.find_cycles(_edges(tmp_path), depth=1)


def test_a_type_only_edge_does_not_make_a_cycle(tmp_path):
    """Nothing imports at runtime, so there is no cycle to break."""
    _write(tmp_path, "alpha/__init__.py", "")
    _write(tmp_path, "alpha/a.py", "import beta.b")
    _write(tmp_path, "beta/__init__.py", "")
    _write(tmp_path, "beta/b.py", """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alpha.a import Thing
""")

    module = _scanner()
    assert module.find_cycles(_edges(tmp_path), depth=1) == []


def test_a_module_importing_its_own_package_sibling_is_not_a_package_cycle(tmp_path):
    _write(tmp_path, "alpha/__init__.py", "")
    _write(tmp_path, "alpha/a.py", "import alpha.b")
    _write(tmp_path, "alpha/b.py", "")

    module = _scanner()
    assert module.find_cycles(_edges(tmp_path), depth=1) == []


def test_a_three_package_cycle_is_reported_once(tmp_path):
    _write(tmp_path, "alpha/__init__.py", "")
    _write(tmp_path, "alpha/a.py", "import beta.b")
    _write(tmp_path, "beta/__init__.py", "")
    _write(tmp_path, "beta/b.py", "import gamma.c")
    _write(tmp_path, "gamma/__init__.py", "")
    _write(tmp_path, "gamma/c.py", "import alpha.a")

    module = _scanner()
    cycles = module.find_cycles(_edges(tmp_path), depth=1)
    assert len(cycles) == 1
    assert sorted(cycles[0]) == ["alpha", "beta", "gamma"]


def test_module_level_cycles_are_found_at_greater_depth(tmp_path):
    """`uta.testgen.a <-> uta.testgen.b` is invisible at package depth 1."""
    _write(tmp_path, "uta/__init__.py", "")
    _write(tmp_path, "uta/testgen/__init__.py", "")
    _write(tmp_path, "uta/testgen/a.py", "import uta.testgen.b")
    _write(tmp_path, "uta/testgen/b.py", """
def late():
    import uta.testgen.a
""")

    module = _scanner()
    assert module.find_cycles(_edges(tmp_path), depth=1) == []
    assert module.find_cycles(_edges(tmp_path), depth=3)


# -- determinism and output ---------------------------------------------------


def test_the_same_tree_scans_to_the_same_bytes_twice(tmp_path):
    """A gate whose output reorders would produce diffs nobody can review."""
    _write(tmp_path, "pkg/a.py", "import pkg.b\nimport pkg.c")
    _write(tmp_path, "pkg/b.py", "import pkg.c")
    _write(tmp_path, "pkg/c.py", "")

    module = _scanner()
    first = module.render_graph(_edges(tmp_path))
    second = module.render_graph(_edges(tmp_path))
    assert first == second
    assert first.splitlines() == sorted(first.splitlines())


def test_the_json_report_round_trips(tmp_path):
    import json

    _write(tmp_path, "pkg/a.py", "import pkg.b")
    _write(tmp_path, "pkg/b.py", "")

    module = _scanner()
    payload = json.loads(module.render_json(_edges(tmp_path)))
    assert payload["edges"] == [
        {
            "importer": "pkg.a",
            "imported": "pkg.b",
            "line": 1,
            "kind": "eager",
            "origin": "internal",
            "wildcard": False,
        }
    ]


def test_the_report_carries_no_absolute_paths(tmp_path):
    """Baseline artifacts get committed; a developer's home directory must not."""
    _write(tmp_path, "pkg/a.py", "import pkg.b")
    _write(tmp_path, "pkg/b.py", "")

    module = _scanner()
    rendered = module.render_graph(_edges(tmp_path)) + module.render_json(_edges(tmp_path))
    assert str(tmp_path) not in rendered


# -- the real tree ------------------------------------------------------------


def test_the_real_tree_scans_within_the_budget():
    """The design budgets 5 seconds for ~207 modules plus the tools tree."""
    module = _scanner()
    started = time.monotonic()
    edges = module.scan_default_roots(REPO_ROOT)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"scan took {elapsed:.2f}s"
    assert len(edges) > 500, len(edges)


def test_the_real_tree_scan_imports_nothing_it_scans():
    """Parsing, not importing -- a scanner with side effects is not a gate."""
    module = _scanner()
    before = set(sys.modules)
    module.scan_default_roots(REPO_ROOT)
    leaked = {name for name in set(sys.modules) - before if name.startswith(("uta", "uta_py_enforce", "uta_enforce_core"))}
    assert leaked == set()


def test_the_command_runs_and_prints_a_graph():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--graph"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "uta.app" in completed.stdout


# =============================================================================
# Task 2 -- the policy layer
#
# The scanner above says what the edges are. The policy says which of them are
# allowed. Keeping them in separate modules matters: the rules are data one can
# read in one sitting, rather than assertions scattered across a test file.
# =============================================================================


POLICY = REPO_ROOT / "scripts" / "package_dependency_policy.py"


def _policy():
    if "package_dependency_policy" in sys.modules:
        return sys.modules["package_dependency_policy"]
    spec = importlib.util.spec_from_file_location("package_dependency_policy", POLICY)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _edge(importer, imported, *, kind="eager", origin="internal", line=1, wildcard=False):
    return _scanner().ImportEdge(importer, imported, line, kind, origin, wildcard)


def _violations(edges):
    return _policy().check(edges)


# -- the one-way rules --------------------------------------------------------


def test_task_storage_may_not_import_the_workflow_that_uses_it():
    violations = _violations([_edge("uta.tasks.retention", "uta.testgen.operations")])
    assert [v.rule for v in violations] == ["tasks-imports-testgen"]


def test_task_storage_may_not_import_agent_core():
    violations = _violations([_edge("uta.tasks.delivery", "agent_core.git", origin="agent_core")])
    assert [v.rule for v in violations] == ["tasks-imports-agent-core"]


def test_task_storage_may_import_shared_values():
    assert _violations([_edge("uta.tasks.db", "uta.shared.values")]) == []


def test_testgen_may_not_import_an_enforcement_binding():
    violations = _violations([_edge("uta.testgen.graph", "uta.enforcement.bindings.java")])
    assert [v.rule for v in violations] == ["testgen-imports-enforcement-binding"]


def test_testgen_may_import_the_neutral_enforcement_contract():
    assert _violations([_edge("uta.testgen.graph", "uta_enforce_core.request")]) == []


def test_testgen_may_not_import_a_concrete_generation_binding():
    """Decided in ADR-012's amendment: app injects the binding, testgen takes it."""
    violations = _violations([_edge("uta.testgen.cycle", "uta.language.java.generation")])
    assert [v.rule for v in violations] == ["testgen-imports-language-binding"]


def test_the_contract_may_not_import_uta():
    violations = _violations([_edge("uta_enforce_core.service", "uta.shared.config")])
    assert [v.rule for v in violations] == ["contract-imports-product"]


def test_the_contract_may_not_import_agent_core():
    violations = _violations([_edge("uta_enforce_core.service", "agent_core.harness", origin="agent_core")])
    assert [v.rule for v in violations] == ["contract-imports-product"]


def test_the_distributed_binding_may_not_import_uta():
    violations = _violations([_edge("uta_py_enforce.mutation", "uta.shared.config")])
    assert [v.rule for v in violations] == ["distributed-imports-product"]


def test_the_distributed_binding_may_import_the_contract():
    assert _violations([_edge("uta_py_enforce.mutation", "uta_enforce_core.models")]) == []


def test_a_binding_may_not_import_the_app_that_composed_it():
    violations = _violations([_edge("uta.enforcement.bindings.java.binding", "uta.app.service")])
    assert [v.rule for v in violations] == ["binding-imports-app"]


def test_app_may_import_anything_it_composes():
    """Composition is the one place allowed to know every concrete name."""
    for target in ("uta.testgen.graph", "uta.tasks.db", "uta.enforcement.bindings.java", "uta.language.python.adapter"):
        assert _violations([_edge("uta.app.service", target)]) == [], target


# -- lazy is not a loophole ---------------------------------------------------


def test_a_forbidden_edge_is_still_forbidden_when_it_is_function_local():
    violations = _violations([_edge("uta.tasks.retention", "uta.testgen.operations", kind="lazy")])
    assert [v.rule for v in violations] == ["tasks-imports-testgen"]


def test_a_forbidden_edge_is_allowed_when_it_is_type_only():
    """Nothing executes, so it cannot invert the runtime dependency."""
    assert _violations([_edge("uta.tasks.retention", "uta.testgen.operations", kind="type_only")]) == []


# -- wildcards ----------------------------------------------------------------


def test_a_wildcard_re_export_of_internal_code_is_refused():
    violations = _violations([_edge("uta.testgen.newthing", "uta.shared.values", wildcard=True)])
    assert "wildcard-re-export" in [v.rule for v in violations]


def test_a_wildcard_from_a_third_party_package_is_not_this_rule_s_business():
    assert _violations([_edge("uta.app.cli", "click", origin="third_party", wildcard=True)]) == []


# -- concrete harness names ---------------------------------------------------


def test_a_concrete_opencode_name_in_product_code_is_refused(tmp_path):
    module = _policy()
    source = tmp_path / "uta" / "testgen" / "harness.py"
    source.parent.mkdir(parents=True)
    source.write_text("from agent_core.harness.process import OpenCodeProcess\n", encoding="utf-8")

    found = module.find_concrete_harness_names(tmp_path / "uta", relative_to=tmp_path)
    assert [(hit.path, hit.name) for hit in found] == [("uta/testgen/harness.py", "OpenCodeProcess")]


def test_the_word_opencode_in_a_comment_is_not_a_violation(tmp_path):
    """Banning the substring would ban explaining why the ban exists."""
    module = _policy()
    source = tmp_path / "uta" / "testgen" / "harness.py"
    source.parent.mkdir(parents=True)
    source.write_text("# We used to call OpenCodeProcess here; agent-core owns it now.\n", encoding="utf-8")

    assert module.find_concrete_harness_names(tmp_path / "uta", relative_to=tmp_path) == []


# -- exceptions are exact, owned, and dated ----------------------------------


def test_every_exception_names_an_owner_a_reason_and_a_deletion_milestone():
    for exception in _policy().EXCEPTIONS:
        assert exception.owner, exception
        assert exception.reason, exception
        assert exception.delete_by, exception


def test_no_exception_is_a_whole_package():
    """A blanket exemption is how a temporary seam becomes the architecture."""
    for exception in _policy().EXCEPTIONS:
        assert "*" not in exception.importer, exception
        assert "*" not in exception.imported, exception


def test_an_exception_forgives_exactly_its_own_edge(monkeypatch):
    module = _policy()
    dummy = module.Exception_(
        "tasks-imports-testgen",
        "uta.tasks.foo",
        "uta.testgen.bar",
        owner="test",
        reason="test",
        delete_by="test",
    )
    monkeypatch.setattr(module, "EXCEPTIONS", (dummy,))

    assert _violations([_edge(dummy.importer, dummy.imported)]) == []
    # A different module in the same package is not covered by it.
    assert _violations([_edge(dummy.importer + "_other", dummy.imported)])


def test_an_exception_that_matches_nothing_is_itself_reported():
    """Otherwise deleted seams accumulate as permanent noise."""
    module = _policy()
    stale = module.stale_exceptions(_scanner().scan_default_roots(REPO_ROOT))
    assert stale == [], f"exceptions matching no edge: {stale}"


# -- reporting ----------------------------------------------------------------


def test_a_violation_reports_a_location_somebody_can_open():
    violations = _violations([_edge("uta.tasks.retention", "uta.testgen.operations", line=42)])
    rendered = _policy().render_violations(violations)
    assert "uta/tasks/retention.py:42" in rendered
    assert "uta.testgen.operations" in rendered


def test_the_real_tree_has_no_violation_that_is_not_a_recorded_exception():
    """This is the gate. It passes today only because every current violation
    is enumerated with an owner and a deletion milestone."""
    module = _policy()
    violations = module.check(_scanner().scan_default_roots(REPO_ROOT))
    assert violations == [], module.render_violations(violations)


def test_the_check_command_exits_zero_on_the_current_tree():
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


# -- the gate must run the checks it is credited with -------------------------
#
# Criterion 1 asks for three things: no prohibited edge, no concrete OpenCode
# dependency, no unapproved cycle. `--check` was running only the first, while
# the other two existed and were exercised only against synthetic fixtures. A
# gate that reports green on checks it never ran is worse than no gate, because
# its green is quoted.


def test_the_check_command_runs_the_cycle_check_against_the_real_tree():
    module = _policy()
    edges = _scanner().scan_default_roots(REPO_ROOT)

    reported = module.unapproved_cycles(edges)
    assert reported == [], f"unapproved cycles: {reported}"


def test_the_check_command_runs_the_harness_name_check_against_the_real_tree():
    module = _policy()

    hits = module.find_concrete_harness_names(REPO_ROOT / "uta", relative_to=REPO_ROOT)
    assert hits == [], f"unexempted concrete harness names: {[(h.path, h.name) for h in hits]}"


def test_every_cycle_exception_names_an_owner_and_a_deletion_milestone():
    for exception in _policy().CYCLE_EXCEPTIONS:
        assert exception.owner, exception
        assert exception.delete_by, exception
        assert exception.reason, exception


def test_a_cycle_exception_that_matches_nothing_is_reported():
    """Same ratchet as the edge exceptions: a cycle that gets broken must force
    its entry out of the table rather than lingering as false debt."""
    module = _policy()
    edges = _scanner().scan_default_roots(REPO_ROOT)

    assert module.stale_cycle_exceptions(edges) == []


def test_a_harness_exception_that_names_a_missing_file_is_reported():
    module = _policy()

    stale = module.stale_harness_exceptions(REPO_ROOT)
    assert stale == [], f"harness exceptions naming files that do not exist: {stale}"


def test_a_new_cycle_is_not_forgiven_by_an_existing_exception(tmp_path):
    module = _policy()
    _write(tmp_path, "alpha/__init__.py", "")
    _write(tmp_path, "alpha/a.py", "import beta.b")
    _write(tmp_path, "beta/__init__.py", "")
    _write(tmp_path, "beta/b.py", "import alpha.a")

    reported = module.unapproved_cycles(_edges(tmp_path), depth=1)
    assert reported == [["alpha", "beta"]]


# -- indirection is still a dependency ---------------------------------------
#
# `importlib.import_module("agent_core.git")` is the same edge as
# `import agent_core.git`, spelled so an AST walker looking only at Import
# nodes cannot see it. The pattern is already live in this tree, so this is a
# hole someone will fall through, not a hypothetical.


def test_import_module_with_a_literal_is_an_edge(tmp_path):
    _write(tmp_path, "pkg/a.py", """
import importlib


def late():
    return importlib.import_module("pkg.b")
""")
    _write(tmp_path, "pkg/b.py", "")

    edge, = _by_target(_edges(tmp_path), "pkg.b")
    assert edge.kind == "lazy"
    assert edge.dynamic is True


def test_a_bare_import_module_call_is_an_edge_too(tmp_path):
    _write(tmp_path, "pkg/a.py", """
from importlib import import_module

thing = import_module("pkg.b")
""")
    _write(tmp_path, "pkg/b.py", "")

    assert _by_target(_edges(tmp_path), "pkg.b")


def test_dunder_import_with_a_literal_is_an_edge(tmp_path):
    _write(tmp_path, "pkg/a.py", """
def late():
    return __import__("pkg.b")
""")
    _write(tmp_path, "pkg/b.py", "")

    assert _by_target(_edges(tmp_path), "pkg.b")


def test_a_dynamic_import_of_a_non_literal_is_reported_not_ignored(tmp_path):
    """A name computed at runtime cannot be resolved, and silently skipping it
    is how a banned import survives. It is surfaced for a human instead."""
    _write(tmp_path, "pkg/a.py", """
import importlib


def late(name):
    return importlib.import_module(name)
""")

    module = _scanner()
    unresolved = module.unresolved_dynamic_imports(_edges(tmp_path))
    assert unresolved == ["pkg.a:5"], unresolved


def test_a_smuggled_forbidden_edge_is_caught_by_the_policy(tmp_path):
    """The exact bypass an independent review used against this scanner."""
    _write(tmp_path, "uta/__init__.py", "")
    _write(tmp_path, "uta/tasks/__init__.py", "")
    _write(tmp_path, "uta/tasks/models.py", """
import importlib


def _smuggled():
    return importlib.import_module("agent_core.git").GitScopedPublisher
""")

    violations = _policy().check(_edges(tmp_path))
    assert [v.rule for v in violations] == ["tasks-imports-agent-core"]


# -- a registry of module paths is a dependency table -------------------------
#
# `uta/shared/backends.py` maps (language, role) to "module.path:Attribute"
# strings and resolves them with `import_module`. Its own docstring says the
# strings exist so "the static graph is acyclic" -- which is true of the graph
# and false of the program. Fourteen real edges were invisible.


def test_a_module_path_registry_produces_edges(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/registry.py", """
from importlib import import_module

_PROVIDERS = {
    ("java", "adapter"): "pkg.java.adapter:JavaAdapter",
    ("python", "adapter"): "pkg.python.adapter:PythonAdapter",
}


def backend(language, role):
    module_path, _, attribute = _PROVIDERS[(language, role)].partition(":")
    return getattr(import_module(module_path), attribute)
""")
    _write(tmp_path, "pkg/java/__init__.py", "")
    _write(tmp_path, "pkg/java/adapter.py", "class JavaAdapter: pass")
    _write(tmp_path, "pkg/python/__init__.py", "")
    _write(tmp_path, "pkg/python/adapter.py", "class PythonAdapter: pass")

    imported = {edge.imported for edge in _edges(tmp_path) if edge.dynamic}
    assert {"pkg.java.adapter", "pkg.python.adapter"} <= imported


def test_registry_edges_are_lazy_not_eager(tmp_path):
    """They resolve on first use, which is what `lazy` means. Recording them
    eager would report a startup cycle that does not exist."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/registry.py", """
_PROVIDERS = {("java", "adapter"): "pkg.java.adapter:JavaAdapter"}
""")
    _write(tmp_path, "pkg/java/__init__.py", "")
    _write(tmp_path, "pkg/java/adapter.py", "class JavaAdapter: pass")

    edge, = [e for e in _edges(tmp_path) if e.imported == "pkg.java.adapter"]
    assert edge.kind == "lazy"


def test_a_string_that_is_not_a_module_path_is_not_an_edge(tmp_path):
    """Only `module.path:Attribute` shaped strings naming a real module count.
    Otherwise every dictionary of prose becomes a dependency graph."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/registry.py", """
MESSAGES = {
    "greeting": "hello:world",
    "path": "some.dotted.name:Attr",
}
""")

    assert [e for e in _edges(tmp_path) if e.dynamic] == []


def test_the_real_backend_registry_is_visible_to_the_scanner():
    """The concrete case this exists for.

    The table has since moved out of `uta.shared` -- it was the single edge
    holding a five-package cycle together -- so this now watches it where it
    lives. That it *had* to move is the point: the scanner made a dependency
    visible, and the visible dependency turned out to be load-bearing.
    """
    edges = _scanner().scan_default_roots(REPO_ROOT)
    from_backends = {
        edge.imported
        for edge in edges
        if edge.importer == "uta.composition.language_backends" and edge.dynamic
    }
    assert "uta.language.java.adapter" in from_backends, sorted(from_backends)
    assert "uta.language.python.adapter" in from_backends, sorted(from_backends)
    assert len(from_backends) >= 14, sorted(from_backends)
