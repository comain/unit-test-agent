"""App composition owns the registry; nothing below it imports a binding.

Success criterion 3. The registry existed and had no production caller, so the
contract was ornamental -- `uta enforce` branched on language and called each
lane's implementation directly, which is the coupling the contract exists to
remove.

The Python lane keeps its `UTA_PYTHON_ENFORCEMENT_IMPL` default of `legacy`.
Registering a binding is a wiring change, not a promotion, and promotion is what
the shadow soak is for.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from uta.app.enforcement_composition import build_enforcement_registry


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_registry_carries_both_languages():
    registry = build_enforcement_registry()

    assert "java" in registry
    assert "python" in registry


def test_the_registry_is_immutable_once_built():
    registry = build_enforcement_registry()

    assert not hasattr(registry, "register"), "a registry you can mutate is a registry you can half-build"


def test_the_java_binding_is_reachable_from_composition():
    registry = build_enforcement_registry()

    binding = registry.get("java")
    assert binding.language == "java"
    assert binding.capabilities().supports_mutation is True


def test_the_python_entry_is_the_thin_proxy():
    from uta.enforcement.bindings.python_proxy import UtaPythonEnforcementProxy

    registry = build_enforcement_registry()
    assert isinstance(registry.get("python"), UtaPythonEnforcementProxy)


def test_an_unknown_language_fails_before_any_work():
    from uta_enforce_core.registry import UnknownLanguageError

    registry = build_enforcement_registry()
    with pytest.raises(UnknownLanguageError):
        registry.get("cobol")


def test_testgen_imports_no_enforcement_binding():
    """The half of the criterion that was vacuously true: testgen consumed no
    enforcement contract at all, so it could not have imported a binding."""
    offenders = []
    for path in (REPO_ROOT / "uta" / "testgen").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = (
                node.module if isinstance(node, ast.ImportFrom) and node.module
                else next((a.name for a in node.names), "") if isinstance(node, ast.Import)
                else ""
            )
            if module.startswith("uta.enforcement.bindings"):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == []


def test_the_contract_imports_no_binding():
    """The direction that must never invert."""
    offenders = []
    contract = REPO_ROOT / "tools" / "python-enforcement" / "uta_enforce_core"
    for path in contract.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for needle in ("uta_py_enforce", "uta.enforcement.bindings", "uta.language"):
            assert needle not in source, f"{path.name} names {needle}"
    assert offenders == []


def test_composition_is_the_only_place_that_imports_both_bindings():
    """Anything else importing both is a second composition root."""
    importers = []
    for path in (REPO_ROOT / "uta").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "JavaEnforcementBinding" in source and "UtaPythonEnforcementProxy" in source:
            importers.append(path.relative_to(REPO_ROOT).as_posix())
    assert sorted(importers) == [
        "uta/app/enforcement_composition.py",
        "uta/enforcement/bindings/__init__.py",
    ], importers


# -- the runner must not break the toolchain it runs ---------------------------


def test_the_safe_runner_passes_toolchain_locations_through(monkeypatch):
    """Scrubbing the environment is for secrets. A Maven build with no
    JAVA_HOME fails in a way that looks nothing like a security control."""
    from uta_enforce_core.commands import SafeProcessRunner

    monkeypatch.setenv("JAVA_HOME", "/opt/java/17")
    monkeypatch.setenv("MAVEN_OPTS", "-Xmx2g")

    env = SafeProcessRunner()._sanitize_env(None)
    assert env["JAVA_HOME"] == "/opt/java/17"
    assert env["MAVEN_OPTS"] == "-Xmx2g"


def test_the_safe_runner_still_drops_credentials(monkeypatch):
    from uta_enforce_core.commands import SafeProcessRunner

    for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "GIT_ASKPASS"):
        monkeypatch.setenv(name, "secret")

    env = SafeProcessRunner()._sanitize_env(None)
    assert not any(name in env for name in
                   ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "GIT_ASKPASS"))


# -- the documentation describes the tree that exists -------------------------


def test_the_readme_documents_no_deleted_package():
    """`uta/engine/` was documented as live for three commits after deletion."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "uta/engine" not in readme


def test_the_readme_describes_the_three_boundaries():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Boundaries" in readme
    for phrase in ("agent-core", "enforcement contract", "distributed Python client"):
        assert phrase in readme, phrase


def test_every_package_the_readme_names_exists():
    """A structure section is a claim about the tree, so check it."""
    import re

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    missing = [
        path
        for path in sorted(set(re.findall(r"`(uta/[\w/]+/)`", readme)))
        if not (REPO_ROOT / path).is_dir()
    ]
    assert missing == [], missing
