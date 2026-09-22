"""The composition table does not live in the lowest layer.

`uta/shared/backends.py` mapped `(language, role)` to
`"uta.language.java.adapter:JavaLanguageAdapter"` strings. Its docstring said
the strings existed so "the static graph is acyclic" -- true of the graph and
false of the program, and once the scanner learned to read the table it became
true of neither.

That single edge was load-bearing. `uta.shared` has exactly one outbound edge to
any other `uta` package, and dropping it collapses the recorded five-package
cycle to two:

    with    shared -> language:  enforcement, language, shared, tasks, testgen
    without shared -> language:  enforcement, language

So the table moves to composition and the registry starts empty. `uta.shared`
keeps the *mechanism* -- registry, lookup, error -- and loses the *knowledge*.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_lowest_layer_declares_no_backend():
    """The registry starts empty. Checked as a value, not as a substring -- the
    module's own docstring explains the cycle it used to cause, and a substring
    check would flag the explanation."""
    import uta.shared.backends as backends

    module = ast.parse(Path(backends.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(module):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_PROVIDERS":
            assert isinstance(node.value, ast.Dict)
            assert node.value.keys == [], "the composition table is back in uta.shared"
            return
    pytest.fail("_PROVIDERS is gone entirely; the registry mechanism should stay")


def test_shared_has_no_outbound_edge_to_any_language_package():
    """What the scanner sees, which is what the gate acts on."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "cpd_backend_check", REPO_ROOT / "scripts" / "check_package_dependencies.py"
    )
    scanner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scanner
    spec.loader.exec_module(scanner)

    offenders = [
        f"{edge.importer}:{edge.line} -> {edge.imported}"
        for edge in scanner.scan_default_roots(REPO_ROOT)
        if edge.kind != "type_only"
        and edge.importer.startswith("uta.shared")
        and edge.imported.startswith("uta.language")
    ]
    assert offenders == [], offenders


def test_registration_makes_a_backend_resolvable():
    from uta.shared.backends import backend_class

    assert backend_class("python", "adapter") is not None


def test_an_unregistered_role_says_what_is_registered():
    from uta.shared.backends import UnknownBackendError, backend_class

    with pytest.raises(UnknownBackendError) as excinfo:
        backend_class("cobol", "adapter")
    assert "cobol" in str(excinfo.value)


def test_every_entrypoint_registers_the_backends():
    """`make_backend` raises when nothing has registered, so a missed
    registration is a crash at first use rather than a startup failure. Both
    entrypoints have to do it, and this is what says so."""
    for relative in ("uta/app/cli.py", "uta/app/app.py"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "register_language_backends()" in source, relative


def test_the_registration_module_is_the_only_place_naming_the_bindings():
    table = REPO_ROOT / "uta" / "composition" / "language_backends.py"
    assert table.is_file()

    # A `"module.path:Attribute"` literal naming a language binding, anywhere
    # but the table. Parsed, so a class *definition* named JavaLanguageAdapter
    # and a docstring mentioning one are both correctly ignored.
    offenders = []
    for path in sorted((REPO_ROOT / "uta").rglob("*.py")):
        if "__pycache__" in path.parts or path == table:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.startswith("uta.language.")
                and ":" in node.value
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}")
    assert offenders == [], offenders
