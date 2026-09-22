"""Tests ensuring competing parallel enforcement protocols are retired."""

from __future__ import annotations

from pathlib import Path


def test_enforcement_core_protocol_is_retired():
    """EnforcementCore was an unused parallel protocol with no production consumer."""
    import uta.enforcement.enforcement as mod

    assert not hasattr(mod, "EnforcementCore"), "EnforcementCore should be deleted"


def test_verification_runner_protocol_is_retired():
    """VerificationRunner was a redundant parallel protocol superseded by language verification bindings."""
    import uta.enforcement.verification as mod

    assert not hasattr(mod, "VerificationRunner"), "VerificationRunner should be deleted"
    assert not hasattr(mod, "VerificationRunnerRegistry"), "VerificationRunnerRegistry should be deleted"


def test_uta_does_not_redefine_the_contract_enforcement_result():
    """`EnforcementResult` belongs to `uta_enforce_core` alone.

    UTA used to define a structurally incompatible pydantic class of the same
    name. That class is a *quality gate command outcome* (command, returncode,
    stdout/stderr, usage guide), not the neutral enforcement result, so it was
    renamed to `QualityGateResult` instead of being merged into the contract.
    """
    import uta.enforcement.enforcement as mod
    from uta_enforce_core.contracts import EnforcementResult as ContractResult

    assert not hasattr(mod, "EnforcementResult"), "UTA must not redefine EnforcementResult"
    assert not hasattr(mod, "EnforcementResultStatus"), "UTA must not redefine EnforcementResultStatus"
    assert hasattr(mod, "QualityGateResult")
    assert hasattr(mod, "QualityGateStatus")
    assert mod.QualityGateResult is not ContractResult


def test_enforcement_runner_port_has_one_definition():
    """`EnforcementRunner` is a subordinate port, defined once.

    `uta.app.ci` re-exports the definition in `uta.enforcement.ci`; it does not
    declare a competing protocol object with the same name.
    """
    import uta.app.ci as app_ci
    import uta.enforcement.ci as enforcement_ci

    assert app_ci.EnforcementRunner is enforcement_ci.EnforcementRunner
    assert enforcement_ci.EnforcementRunner.__module__ == "uta.enforcement.ci"

    for name in ("BaseCiLanguageHandler", "CiLanguageHandler", "CiLanguageHandlerRegistry"):
        assert getattr(app_ci, name) is getattr(enforcement_ci, name)

    source = (Path(__file__).resolve().parents[1] / "uta" / "app" / "ci.py").read_text(encoding="utf-8")
    assert "class EnforcementRunner" not in source


def test_generic_enforcement_evidence_does_not_import_java():
    """Requirement: "Generic enforcement must not depend on Java."

    `uta/enforcement/evidence.py` imported three Maven/PIT output parsers from
    `uta.language.java.ci_evidence` -- the module the spec's candidate table
    names for exactly this. It was also one of only three edges pointing the
    wrong way between enforcement and language; the other ten point correctly,
    from a binding to the contract.
    """
    import ast

    source = Path(__file__).resolve().parents[1] / "uta" / "enforcement" / "evidence.py"
    imported = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    offenders = sorted(m for m in imported if m.startswith("uta.language"))
    assert offenders == [], offenders


def test_raw_output_evidence_is_resolved_as_a_backend_role():
    """The parser arrives through composition, like every other language piece."""
    from uta.shared.backends import backend_class

    parser = backend_class("java", "output_evidence")
    for method in ("baseline_failure_detail", "should_merge", "output_detail"):
        assert hasattr(parser, method), method


def test_a_language_with_no_parser_merges_nothing():
    """Fail quiet, not loud: an unregistered language should contribute no
    output-derived evidence rather than raising inside a report."""
    from uta.enforcement.evidence import raw_output_parser_for

    parser = raw_output_parser_for("cobol")
    assert parser.should_merge({}, "anything") is False
    assert parser.baseline_failure_detail("anything") is None


def test_python_repair_uses_the_canonical_enforcer_not_the_direct_verifier():
    """Repair and CI must enter Python enforcement through the same tool.

    The low-level verifier remains an implementation detail of the canonical
    binding.  Importing it from the generation cycle recreates the competing
    repair lane that previously drifted from CI selection and evidence.
    """
    import inspect

    from uta.language.python import phases
    from uta.language.python.generation_backend import PythonGenerationCycleBackend
    from uta.language.python.verification import generation

    phase_source = inspect.getsource(phases)
    adapter_source = inspect.getsource(generation)
    assert "run_python_enforcement" in adapter_source
    assert "verify_generated_test" not in phase_source
    assert "from uta.language.python.verification.runner import (\n    verify_python_target" not in adapter_source
    assert "verification_runner" not in inspect.signature(
        PythonGenerationCycleBackend
    ).parameters
