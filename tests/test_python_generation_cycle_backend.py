
from uta.language.python import phases as python_phases
from uta.language.python.generation_backend import PythonGenerationCycleBackend
from uta.language.python.cycle_inputs import prepare_python_cycle_state
from uta.language.python.verification.runner import (
    CoverageSummary,
    MutationSummary,
    PythonVerificationResult,
)


def _state(tmp_path, **updates):
    state = {
        "repo_path": str(tmp_path),
        "language": "python",
        "batch": ["pyfile:jobs/forecast.py"],
        "target": {
            "language": "python",
            "target_id": "pyfile:jobs/forecast.py",
            "display_name": "jobs/forecast.py",
            "source_path": "jobs/forecast.py",
            "granularity": "file",
        },
        "generated_test_path": "tests/uta_generated/test_jobs_forecast.py",
        "target_context_paths": {"context_abs": "context.md", "json_abs": "context.json"},
        "context_payload": {"syntax": {"version": "python3"}},
        "coverage_gate": 80.0,
        "mutation_gate": 70.0,
        "results": {},
        "phase_results": {},
        "attempts_by_phase": {},
        "max_attempts_by_phase": {"python_repair_total": 2},
    }
    state.update(updates)
    return state


def _write_test(tmp_path, text="def test_forecast():\n    assert True\n"):
    path = tmp_path / "tests/uta_generated/test_jobs_forecast.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _enforcer_from_verifier(verifier):
    def enforce(**kwargs):
        result = verifier(run_mutation=kwargs["run_mutation"])
        return {
            "status": result.status,
            "passed": result.status == "passed",
            "reasonCode": result.reason_code,
            "summary": result.message,
            "targetResults": [{
                "status": result.status,
                "reasonCode": result.reason_code,
                "testsPass": result.tests_pass,
                "message": result.message,
                "coverage": result.coverage.as_dict() if result.coverage else None,
                "mutation": result.mutation.as_dict() if result.mutation else None,
            }],
        }

    return enforce


def test_python_precheck_records_unsupported_plan_without_a_model_turn(tmp_path):
    result = PythonGenerationCycleBackend().run_phase(
        "precheck_existing_tests", _state(tmp_path)
    )

    assert result["phase_outcome"] == "plan_skipped"
    assert result["phase_results"]["plan_tests"] == {
        "phase_outcome": "skipped",
        "evidence": {"reason": "unsupported_by_current_python_policy"},
    }


def test_python_preverified_existing_test_keeps_its_passing_result(tmp_path):
    _write_test(tmp_path)

    def verify(*_args, **_kwargs):
        return PythonVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=CoverageSummary(
                covered=1,
                total=1,
                rate=100.0,
                gate=80.0,
                passed=True,
                xml_path="coverage.xml",
            ),
        )

    backend = PythonGenerationCycleBackend(enforcer=_enforcer_from_verifier(verify))
    prechecked = backend.run_phase("precheck_existing_tests", _state(tmp_path))
    completed = backend.run_phase(
        "complete_generation",
        _state(
            tmp_path,
            phase_results={"precheck_existing_tests": prechecked},
        ),
    )

    assert prechecked["existing_verification"]["result_fields"]["status"] == "PASS"
    assert completed["results"]["pyfile:jobs/forecast.py"]["status"] == "PASS"


def test_python_compile_equivalent_checks_syntax_and_import_contract(tmp_path):
    _write_test(tmp_path, "def test_broken(:\n    pass\n")
    backend = PythonGenerationCycleBackend()

    syntax = backend.run_phase("verify_compile", _state(tmp_path))
    _write_test(
        tmp_path,
        "import importlib.util\n\ndef test_bad():\n    importlib.util.spec_from_file_location('x', 'x.py')\n",
    )
    imports = backend.run_phase("verify_compile", _state(tmp_path))

    assert syntax["phase_outcome"] == "repair"
    assert syntax["evidence"]["failure_reason"] == "python_syntax_error"
    assert imports["phase_outcome"] == "repair"
    assert imports["evidence"]["failure_reason"] == "invalid_generated_test_import"


def test_python_compile_and_test_repairs_share_one_unit_wide_budget(tmp_path):
    backend = PythonGenerationCycleBackend()
    first = backend.interpret(
        "fix_compile", _state(tmp_path), {"status": "completed", "session_id": "s1"}
    )
    second = backend.interpret(
        "fix_tests",
        _state(tmp_path, attempts_by_phase=first["attempts_by_phase"]),
        {"status": "completed", "session_id": "s2"},
    )
    scored = backend.interpret(
        "fix_coverage",
        _state(tmp_path, attempts_by_phase=second["attempts_by_phase"]),
        {"status": "completed", "session_id": "s3"},
    )

    assert first["attempts_by_phase"]["python_repair_total"] == 1
    assert second["attempts_by_phase"]["python_repair_total"] == 2
    # Score-driven repair is bounded by its own progress, so it must not spend
    # the budget compile and test repair share.
    assert scored["attempts_by_phase"]["python_repair_total"] == 2
    assert scored["attempts_by_phase"]["fix_coverage"] == 1
    exhausted = PythonGenerationCycleBackend().run_phase(
        "verify_compile",
        _state(
            tmp_path,
            attempts_by_phase=second["attempts_by_phase"],
        ),
    )
    assert exhausted["phase_outcome"] == "failed"


def test_python_verification_splits_tests_coverage_and_mutation(tmp_path):
    _write_test(tmp_path)
    calls = []

    def enforce(**kwargs):
        calls.append((kwargs["run_mutation"], kwargs["base_ref"]))
        mutation = {
            "runtimeLane": "mutmut-modern", "generated": 10, "killed": 8,
            "survived": 2, "noCoverage": 0, "rate": 80.0, "gate": 70.0,
            "passed": True,
        } if kwargs["run_mutation"] else None
        return {
            "status": "passed", "passed": True, "reasonCode": "passed",
            "summary": "passed",
            "targetResults": [{
                "status": "passed", "reasonCode": "passed", "testsPass": True,
                "coverage": {
                    "covered": 9, "total": 10, "rate": 90.0, "gate": 80.0,
                    "passed": True, "xml_path": "coverage.xml",
                },
                "mutation": mutation,
            }],
        }

    backend = PythonGenerationCycleBackend(enforcer=enforce)
    state = _state(tmp_path, base_ref="origin/release")
    tested = backend.run_phase("verify_tests", state)
    covered = backend.run_phase(
        "measure_coverage",
        _state(tmp_path, base_ref="origin/release", phase_results={"verify_tests": tested}),
    )
    mutated = backend.run_phase("measure_mutation", state)

    assert tested["phase_outcome"] == "passed"
    assert covered["phase_outcome"] == "passed"
    assert mutated["phase_outcome"] == "passed"
    assert calls == [(False, "origin/release"), (True, "origin/release")]


def test_python_mutation_skip_is_explicit(tmp_path):
    result = PythonGenerationCycleBackend().run_phase(
        "measure_mutation", _state(tmp_path, mutation_gate=0)
    )

    assert result == {
        "phase_outcome": "skipped",
        "evidence": {"reason": "mutation_gate_disabled"},
    }


def test_python_candidate_plan_failure_is_not_sent_to_mutation_repair(tmp_path):
    _write_test(tmp_path)

    def verify(*_args, **_kwargs):
        return PythonVerificationResult(
            status="failed",
            reason_code="mutation_candidate_plan_failed",
            tests_pass=True,
            coverage=CoverageSummary(
                covered=1, total=1, rate=100.0, gate=80.0, passed=True, xml_path="coverage.xml"
            ),
            mutation=MutationSummary(
                runtime_lane="mutmut-modern", generated=0, killed=0, survived=0,
                no_coverage=0, rate=100.0, gate=95.0, passed=True,
            ),
            message="Python mutation candidate plan failed: metadata generation failed",
        )

    measured = PythonGenerationCycleBackend(enforcer=_enforcer_from_verifier(verify)).run_phase(
        "measure_mutation", _state(tmp_path, mutation_gate=95.0)
    )

    assert measured["phase_outcome"] == "failed"
    assert measured["evidence"]["failure_reason"] == "mutation_candidate_plan_failed"


def test_python_existing_test_precheck_does_not_repair_candidate_plan_failure(tmp_path):
    _write_test(tmp_path)

    def verify(*_args, **_kwargs):
        return PythonVerificationResult(
            status="failed",
            reason_code="mutation_candidate_plan_failed",
            tests_pass=True,
            message="metadata generation failed",
        )

    result = PythonGenerationCycleBackend(enforcer=_enforcer_from_verifier(verify)).run_phase(
        "precheck_existing_tests", _state(tmp_path)
    )

    assert result["phase_outcome"] == "failed"
    assert result["evidence"]["failure_reason"] == "mutation_candidate_plan_failed"


def test_python_cycle_input_contains_no_live_backend_objects(tmp_path):
    source = tmp_path / "jobs/forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def forecast():\n    return 1\n", encoding="utf-8")
    state = {
        "repo_path": str(tmp_path),
        "current_target": _state(tmp_path)["target"],
        "results": {},
        "coverage_gate": 80,
        "mutation_gate": 0,
    }

    prepared = prepare_python_cycle_state(state)

    assert prepared["target"]["target_id"] == "pyfile:jobs/forecast.py"
    assert prepared["generated_test_path"] == "tests/uta_generated/test_jobs_forecast.py"
    assert prepared["max_attempts_by_phase"]["python_repair_total"] == 2
    assert "backend_context" not in prepared


def test_python_mutation_repair_prompt_names_the_survivors(tmp_path, monkeypatch):
    """The repair round has to see which mutants lived.

    Production task 150 spent four rounds on a target whose prompt carried only
    "Python mutation score 90.91% is below gate 95.00%" where its SURVIVING
    MUTANTS block should have been, and then told the model to read a mutation
    repair context artifact nothing had written. The rounds were re-rolls of one
    blind guess, so the score stalled and the progress heuristic -- correctly --
    stopped paying for them.
    """
    _write_test(tmp_path)
    (tmp_path / "jobs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jobs/forecast.py").write_text(
        "def forecast(x):\n    return x + 1\n", encoding="utf-8"
    )
    survivors = [
        {"id": "jobs.forecast.x_forecast__mutmut_1", "line": 2, "symbol": "forecast"}
    ]

    def verify(*args, **kwargs):
        return PythonVerificationResult(
            status="failed",
            reason_code="mutation_gate_failed",
            tests_pass=True,
            coverage=CoverageSummary(
                covered=10, total=10, rate=100.0, gate=80.0, passed=True, xml_path="c.xml"
            ),
            mutation=MutationSummary(
                runtime_lane="mutmut-modern", generated=11, killed=10, survived=1,
                no_coverage=0, rate=90.9091, gate=95.0, passed=False,
                survivors=survivors,
            ) if kwargs["run_mutation"] else None,
        )

    backend = PythonGenerationCycleBackend(enforcer=_enforcer_from_verifier(verify))
    state = _state(tmp_path, mutation_gate=95.0)
    measured = backend.run_phase("measure_mutation", state)
    evidence = measured["evidence"]
    prompt = backend.render_prompt(
        "fix_mutation", _state(tmp_path, mutation_gate=95.0,
                               phase_results={"measure_mutation": measured})
    )

    assert measured["phase_outcome"] == "repair"
    assert evidence["mutation_repair_context_abs"].endswith(
        "mutation-repair-context.md"
    )
    assert "forecast" in evidence["mutation_repair_groups"]
    # The prompt must carry the survivor map, not just the score it fell short of.
    assert "forecast" in prompt
    assert "Python mutation score" not in prompt.split("### SURVIVING MUTANTS")[1]
    assert "make no test edit and stop promptly" in prompt


def test_python_survivor_summary_in_evidence_is_bounded():
    """The summary rides the checkpoint and a ledger artifact on every
    measurement, so a target with hundreds of kilobytes of mutmut diffs must
    not carry them all; the full map stays in the artifact the prompt names."""
    huge = "x" * (python_phases.SURVIVOR_SUMMARY_MAX_CHARS + 5000)

    bounded = python_phases._bounded_summary(huge, "/repo/.uta_cache/ctx.md")

    assert len(bounded) < len(huge)
    assert bounded.startswith("x")
    assert "/repo/.uta_cache/ctx.md" in bounded
    assert python_phases._bounded_summary("short", "/repo/ctx.md") == "short"


def test_python_skips_the_survivor_map_when_no_round_can_follow(tmp_path):
    """The last measurement must not pay for `mutmut show` subprocesses whose
    output only a repair round would read."""
    _write_test(tmp_path)
    (tmp_path / "jobs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jobs/forecast.py").write_text("def forecast(x):\n    return x\n", encoding="utf-8")

    def verify(*args, **kwargs):
        return PythonVerificationResult(
            status="failed", reason_code="mutation_gate_failed", tests_pass=True,
            mutation=MutationSummary(
                runtime_lane="mutmut-modern", generated=11, killed=10, survived=1,
                no_coverage=0, rate=90.9091, gate=95.0, passed=False,
                survivors=[{"id": "m1", "line": 2, "symbol": "forecast"}],
            ) if kwargs["run_mutation"] else None,
        )

    exhausted = PythonGenerationCycleBackend(enforcer=_enforcer_from_verifier(verify)).run_phase(
        "measure_mutation",
        _state(tmp_path, mutation_gate=95.0,
               attempts_by_phase={"fix_mutation": 6},
               max_attempts_by_phase={"fix_mutation": 6}),
    )

    assert exhausted["phase_outcome"] == "repair"  # the policy fails it afterwards
    assert "mutation_repair_context_abs" not in exhausted["evidence"]
