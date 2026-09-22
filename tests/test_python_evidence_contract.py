import subprocess
from pathlib import Path

from uta.language.python.enforcement import (
    PYTHON_ENFORCEMENT_SCHEMA_VERSION,
    PythonEnforcementStatus,
    _aggregate_mutation,
    _changed_lines_by_file,
    _ci_report_mutation_selection,
    _diagnosis,
    _has_only_non_executable_changed_lines,
    _target_refs,
    format_evidence_markers,
    run_python_enforcement,
    validate_python_enforcement_evidence,
)
from uta.language.python.verification.runner import CoverageSummary, MutationSummary, PythonVerificationResult
from uta.language.python.ci_sampling import CiCapSamplingPolicy
from binding_stubs import PassingBinding, TargetOutcome, stub_for
from binding_stubs import (  # noqa: F401  -- pytest fixtures
    binding_refuses_to_verify,
    binding_stub,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_forecast.py").write_text("from jobs.forecast import run\n\n\ndef test_run():\n    assert run() == 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")


def _init_multi_file_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "jobs").mkdir()
    (repo / "jobs" / "forecast.py").write_text("def forecast():\n    return 1\n", encoding="utf-8")
    (repo / "jobs" / "pricing.py").write_text("def price():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_forecast.py").write_text("def test_forecast():\n    assert True\n", encoding="utf-8")
    (repo / "tests" / "test_pricing.py").write_text("def test_pricing():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "jobs" / "forecast.py").write_text("def forecast():\n    return 2\n", encoding="utf-8")
    (repo / "jobs" / "pricing.py").write_text("def price():\n    return 2\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change two files")


def _passing_verification(*args, **kwargs):
    return PythonVerificationResult(
        status="passed",
        reason_code="passed",
        tests_pass=True,
        coverage=CoverageSummary(
            covered=1,
            total=1,
            rate=100.0,
            gate=95.0,
            passed=True,
            xml_path=".uta_cache/python/coverage/coverage.xml",
            scope="changed_lines",
            changed_lines={"jobs/forecast.py": [2]},
        ),
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=4,
            killed=4,
            survived=0,
            no_coverage=0,
            rate=100.0,
            gate=100.0,
            passed=True,
            scope="changed_lines",
            changed_lines={"jobs/forecast.py": [2]},
            diff_survivors=[],
            changed_line_mutants_generated=4,
            changed_line_mutants_killed=4,
        ),
        message="Python pytest, coverage, and mutation gates passed",
        environment_profile="test",
        cache_key="python-env:test",
    )


def _enforce_with(repo, binding, **kwargs):
    return run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        coverage_gate=95.0,
        mutation_gate=100.0,
        enforcement_binding=binding,
        **kwargs,
    )


def test_local_development_gets_no_sampling_policy(tmp_path):
    """Full mutation is the local default. Sampling is a CI cost control, and
    silently applying it locally would hide surviving mutants from the person
    who could still fix them."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    binding = PassingBinding(changed_lines={"jobs/forecast.py": [2]})

    evidence = _enforce_with(repo, binding)

    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert binding.sampling_policies == [None]


def test_the_ci_sampling_env_var_alone_does_not_enable_sampling(monkeypatch, tmp_path):
    """The env var is read by the CI adapter, which then asks for sampling
    explicitly. Honouring it here too would mean a developer with it left in
    their shell silently enforces less than everyone else."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setenv("UTA_PYTHON_ENABLE_CI_MUTATION_SAMPLING", "1")
    binding = PassingBinding(changed_lines={"jobs/forecast.py": [2]})

    evidence = _enforce_with(repo, binding)

    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert binding.sampling_policies == [None]


def test_the_ci_adapter_gets_the_cap_policy(tmp_path):
    """Asking for CI sampling now hands the binding a policy carrying the
    caps. It used to hand it nothing at all -- the flag stopped at the lane
    boundary, and CI ran uncapped."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    binding = PassingBinding(changed_lines={"jobs/forecast.py": [2]})

    _enforce_with(repo, binding, enable_ci_mutation_sampling=True)

    assert len(binding.sampling_policies) == 1
    policy = binding.sampling_policies[0]
    assert isinstance(policy, CiCapSamplingPolicy)
    assert policy.max_selected > 0


def test_python_enforcement_skips_comment_only_changed_lines_before_test_collection(
    tmp_path, binding_refuses_to_verify
):
    """Refusing to verify a comment-only diff is a pre-flight property, and the
    lane that replaces legacy has to refuse what legacy refuses. Checked
    against both: an envelope-only assertion would pass even if the lane ran
    the whole suite first."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "jobs" / "forecast.py").write_text(
        "def run():\n    return 2\n    # disabled validation\n    # return 3\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "comments only")

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        coverage_gate=95.0,
        mutation_gate=95.0,
        **binding_refuses_to_verify,
    )

    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert evidence["coverage"]["no_executable_changed_lines"] is True
    assert evidence["coverage"]["total"] == 0
    assert evidence["mutation"]["generated"] == 0
    assert evidence["targetResults"][0]["reasonCode"] == "no_executable_changed_lines"


def test_python_enforcement_evaluates_all_targets_before_aggregating(tmp_path):
    """No short-circuit on the first failing target.

    Stopping early would report a green second target as unverified and an
    aggregate built from one module -- and the envelope would look plausible
    either way, so the assertion that matters is which targets were asked for.
    Both lanes, because "evaluate everything" is a property of enforcement,
    not of an implementation.
    """
    repo = tmp_path / "repo"
    _init_multi_file_repo(repo)
    stub, calls = stub_for(
        {
            "jobs/forecast.py": TargetOutcome(passed=True, covered=1, total=1),
            "jobs/pricing.py": TargetOutcome(passed=False, covered=0, total=1),
        }
    )

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=100.0,
        **stub,
    )

    assert calls == ["jobs/forecast.py", "jobs/pricing.py"]
    assert [target["source_path"] for target in evidence["targets"]] == calls
    assert len(evidence["targetResults"]) == 2
    assert evidence["status"] == PythonEnforcementStatus.failed.value
    assert evidence["reasonCode"] == "coverage_gate_failed"
    assert evidence["coverage"]["modules"] == 2
    assert evidence["coverage"]["covered"] == 1
    assert evidence["coverage"]["total"] == 2
    assert evidence["coverage"]["rate"] == 50.0
    assert evidence["coverage"]["passed"] is False
    assert evidence["coverage"]["changed_lines"] == {
        "jobs/forecast.py": [2],
        "jobs/pricing.py": [2],
    }


def test_python_enforcement_excludes_package_local_test_directories(tmp_path):
    """A file under a package-local `test/` directory is not a production
    target. Which files count as changed production code is resolved by UTA
    before either lane runs, so both must agree -- and enforcing a test helper
    would fail a build over code nobody ships."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    package_test = repo / "jobs" / "test" / "check_test.py"
    package_test.parent.mkdir(parents=True)
    package_test.write_text("def helper():\n    return 'not a production target'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add package local test helper")
    stub, calls = stub_for({"jobs/forecast.py": TargetOutcome(passed=True)})

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=100.0,
        **stub,
    )

    assert calls == ["jobs/forecast.py"]
    assert evidence["changedProductionFiles"] == ["jobs/forecast.py"]
    assert "jobs/test/check_test.py" not in evidence["changedLines"]


def test_python_enforcement_evidence_is_schema_versioned_and_commit_bound(
    tmp_path, binding_stub
):
    """The envelope is the contract every consumer reads, so it is checked
    against both lanes. What the *runner* was handed is lane-specific and is
    asserted separately below."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=["jobs/forecast.py"],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=100.0,
        **binding_stub,
    )

    assert evidence["schemaVersion"] == PYTHON_ENFORCEMENT_SCHEMA_VERSION
    assert evidence["language"] == "python"
    assert evidence["backend"] == "python_enforcer"
    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert evidence["reasonCode"] == "passed"
    assert evidence["baseRef"] == "origin/master"
    assert evidence["baseCommit"]
    assert evidence["headCommit"]
    assert evidence["changedProductionFiles"] == ["jobs/forecast.py"]
    assert evidence["changedLines"] == {"jobs/forecast.py": [2]}
    assert evidence["coverage"]["passed"] is True
    assert evidence["coverage"]["scope"] == "changed_lines"
    assert evidence["coverage"]["modules"] == 1
    assert evidence["coverage"]["changed_lines"] == {"jobs/forecast.py": [2]}
    assert evidence["mutation"]["passed"] is True
    assert evidence["mutation"]["scope"] == "changed_lines"
    assert evidence["mutation"]["modules"] == 1
    assert evidence["mutation"]["changed_lines"] == {"jobs/forecast.py": [2]}
    assert evidence["targets"][0]["target_id"] == "pyfile:jobs/forecast.py"

    verdict = validate_python_enforcement_evidence(evidence, expected_head=evidence["headCommit"])

    assert verdict.passed is True
    assert verdict.reason_code == "passed"


def test_python_changed_line_mutation_aggregate_preserves_failed_targets():
    aggregate = _aggregate_mutation(
        [
            {
                "mutation": {
                    "generated": 57,
                    "killed": 57,
                    "survived": 0,
                    "rate": 100.0,
                    "gate": 100.0,
                    "passed": True,
                    "scope": "changed_lines",
                    "changedLineMutantsGenerated": 57,
                    "changedLineMutantsKilled": 57,
                    "changedLineMutantsScored": 57,
                    "changed_lines": {"jobs/a.py": [1]},
                    "candidatePlan": {
                        "changedLines": [1],
                        "coveredChangedLines": [1],
                        "eligibleMutationOpportunities": [{"opportunityId": "opp-a"}],
                        "suppressed": [],
                        "reportFullSelected": [{"candidateId": "c-a", "toolCandidateKey": "jobs.a.x_f__mutmut_1"}],
                        "activeSelected": [{"candidateId": "c-a", "toolCandidateKey": "jobs.a.x_f__mutmut_1"}],
                        "exactToolCandidateKeys": ["jobs.a.x_f__mutmut_1"],
                        "runMutants": 57,
                        "scoredMutants": 57,
                        "killed": 57,
                        "survived": 0,
                    },
                }
            },
            {
                "mutation": {
                    "generated": 806,
                    "killed": 0,
                    "survived": 0,
                    "rate": 0.0,
                    "gate": 100.0,
                    "passed": False,
                    "scope": "changed_lines",
                    "changedLineMutantsGenerated": 0,
                    "changedLineMutantsKilled": 0,
                    "changedLineMutantsScored": 0,
                    "changed_lines": {"jobs/b.py": [1]},
                    "candidatePlan": {
                        "changedLines": [1],
                        "coveredChangedLines": [1],
                        "eligibleMutationOpportunities": [{"opportunityId": "opp-b"}],
                        "suppressed": [],
                        "reportFullSelected": [],
                        "activeSelected": [],
                        "exactToolCandidateKeys": [],
                        "planningError": "no exact keys",
                    },
                }
            },
        ],
        100.0,
    )

    assert aggregate["passed"] is False
    assert aggregate["rate"] == 100.0
    assert aggregate["worstTargetRate"] == 0.0
    assert aggregate["changedLineMutantsGenerated"] == 57
    assert aggregate["changedLineMutantsKilled"] == 57
    assert aggregate["generated"] == 863
    assert aggregate["modules"] == 2
    assert aggregate["candidatePlan"]["changedLines"] == 2
    assert aggregate["candidatePlan"]["coveredChangedLines"] == 2
    assert aggregate["candidatePlan"]["eligibleOpportunities"] == 2
    assert aggregate["candidatePlan"]["reportFullCandidates"] == 1
    assert aggregate["candidatePlan"]["activeCandidates"] == 1
    assert aggregate["candidatePlan"]["runMutants"] == 57
    assert aggregate["candidatePlan"]["scoredMutants"] == 57


def test_python_changed_line_mutation_aggregate_accepts_explicit_zero_mutant_pass():
    aggregate = _aggregate_mutation(
        [
            {
                "mutation": {
                    "generated": 0,
                    "killed": 0,
                    "survived": 0,
                    "rate": 100.0,
                    "gate": 100.0,
                    "passed": True,
                    "scope": "changed_lines",
                    "changedLineMutantsGenerated": 0,
                    "changedLineMutantsKilled": 0,
                    "changedLineMutantsScored": 0,
                    "changed_lines": {"dashboard/store/views_floor_mark.py": [293, 294]},
                    "candidatePlan": {
                        "changedLines": [293, 294],
                        "coveredChangedLines": [293, 294],
                        "eligibleMutationOpportunities": [],
                        "suppressed": [],
                        "reportFullSelected": [],
                        "activeSelected": [],
                        "exactToolCandidateKeys": [],
                    },
                }
            }
        ],
        100.0,
    )

    assert aggregate["rate"] == 100.0
    assert aggregate["passed"] is True
    assert aggregate["changedLineMutantsGenerated"] == 0


def test_python_changed_line_mutation_aggregate_preserves_explicit_zero_mutant_failure():
    aggregate = _aggregate_mutation(
        [
            {
                "mutation": {
                    "generated": 0,
                    "killed": 0,
                    "survived": 0,
                    "rate": 0.0,
                    "gate": 100.0,
                    "passed": False,
                    "scope": "changed_lines",
                    "changedLineMutantsGenerated": 0,
                    "changedLineMutantsKilled": 0,
                    "changedLineMutantsScored": 0,
                    "changed_lines": {"jobs/broken.py": [7]},
                }
            }
        ],
        100.0,
    )

    assert aggregate["rate"] == 0.0
    assert aggregate["passed"] is False


def test_python_enforcement_no_target_is_explicit_pass(tmp_path, binding_refuses_to_verify):
    """A docs-only diff has nothing to enforce, and saying so explicitly is
    what keeps it from reading as a silent skip. Both lanes, because a lane
    that ran verification here and still reported `passed` would look
    identical in the envelope."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "docs")

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests/test_forecast.py"],
        base_ref="origin/master",
        coverage_gate=95.0,
        mutation_gate=100.0,
        **binding_refuses_to_verify,
    )

    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert evidence["reasonCode"] == "no_changed_python_targets"
    assert evidence["targets"] == []
    assert evidence["changedProductionFiles"] == []


def test_format_evidence_markers_includes_test_quality_line_when_warnings_exist():
    base = {
        "language": "python",
        "coverage": {"rate": 100.0, "passed": True, "covered": 4, "total": 4},
        "mutation": {"rate": 100.0, "passed": True, "killed": 2, "generated": 2},
    }
    with_warnings = {
        **base,
        "targetResults": [
            {
                "testQuality": {
                    "warningCount": 2,
                    "topRuleIds": [
                        {"ruleId": "python-weak-assert-not-none", "count": 1},
                        {"ruleId": "python-happy-path-only-hint", "count": 1},
                    ],
                }
            }
        ],
    }

    marked = format_evidence_markers(with_warnings)
    unmarked = format_evidence_markers(base)

    assert "[test-enforcer] python test quality 2 advisory warning(s)" in marked
    assert "top=python-weak-assert-not-nonex1,python-happy-path-only-hintx1" in marked
    assert "coverage/mutation gates unaffected" in marked
    assert "test quality" not in unmarked


def test_python_ci_mutation_cap_is_shared_across_report_targets(monkeypatch, tmp_path):
    """The cap is a report budget, not a per-target one.

    `select_mutants` fires once per target. Before the budget was allocated up
    front, an N-target report ran N x `max_selected` mutants -- the cost blowup
    the CI profile exists to prevent.
    """
    repo = tmp_path / "repo"
    _init_multi_file_repo(repo)
    monkeypatch.setattr(
        "uta.language.python.enforcement.settings.python_mutation_generation_ci_max_selected",
        2,
    )
    binding = PassingBinding()

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        coverage_gate=95.0,
        mutation_gate=100.0,
        enforcement_binding=binding,
        enable_ci_mutation_sampling=True,
    )

    policy = binding.sampling_policies[0]
    assert policy.allocations == {"jobs/forecast.py": 1, "jobs/pricing.py": 1}
    assert sum(policy.allocations.values()) == 2

    budget = evidence["mutationBudget"]
    assert budget["scope"] == "report"
    assert budget["configuredCandidates"] == 2
    assert budget["allocatedCandidates"] == 2
    assert budget["strategy"] == "deterministic_target_symbol_operator_hash_v1"
    assert len(budget["seed"]) == 64
    assert budget["targetAllocations"] == {
        "jobs/forecast.py": 1,
        "jobs/pricing.py": 1,
    }


def test_python_ci_report_selection_is_stable_and_not_input_ordered(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _init_multi_file_repo(repo)
    targets = _target_refs(repo, [], base_ref="origin/master")
    changed_lines = _changed_lines_by_file(
        repo,
        "origin/master",
        [str(target.source_path) for target in targets],
    )
    monkeypatch.setattr(
        "uta.language.python.enforcement.settings.python_mutation_generation_ci_max_selected",
        1,
    )

    first, first_seed = _ci_report_mutation_selection(
        repo=repo,
        targets=targets,
        changed_lines=changed_lines,
        enabled=True,
        base_commit="base",
    )
    reordered, reordered_seed = _ci_report_mutation_selection(
        repo=repo,
        targets=list(reversed(targets)),
        changed_lines=changed_lines,
        enabled=True,
        base_commit="base",
    )

    assert first == reordered
    assert first_seed == reordered_seed
    assert sum(len(lines) for lines in first.values()) == 1


def test_python_ci_sample_seed_ignores_test_only_head_changes(monkeypatch, tmp_path):
    """A test-only commit must not reshuffle the sample.

    Seeding on the head commit meant every repair push resampled, so the
    mutation that failed CI vanished and a different one appeared -- the
    failure stopped reproducing across exactly the commits a developer makes
    while trying to fix it.
    """
    repo = tmp_path / "repo"
    _init_multi_file_repo(repo)
    monkeypatch.setattr(
        "uta.language.python.enforcement.settings.python_mutation_generation_ci_max_selected",
        1,
    )

    first = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        enforcement_binding=PassingBinding(),
        enable_ci_mutation_sampling=True,
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "def test_forecast():\n    assert 2 == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "repair tests only")
    second = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        enforcement_binding=PassingBinding(),
        enable_ci_mutation_sampling=True,
    )

    assert first["headCommit"] != second["headCommit"]
    assert first["mutationBudget"]["seed"] == second["mutationBudget"]["seed"]
    assert first["mutationBudget"]["targetAllocations"] == second["mutationBudget"]["targetAllocations"]


def test_python_ci_sampling_enforces_mutation_gate_only_on_report_aggregate(tmp_path):
    """One target misses its sampled mutant, the other kills its own.

    Per target that is a fail and a pass; over the report it is 50%, which
    clears the gate. Only the aggregate verdict is meaningful when each target
    saw a single mutant.
    """
    repo = tmp_path / "repo"
    _init_multi_file_repo(repo)
    kwargs, _calls = stub_for(
        {
            "jobs/forecast.py": TargetOutcome(
                passed=False,
                reason_code="mutation_gate_failed",
                message="sample target result",
                mutants_generated=1,
                mutants_killed=0,
            ),
            "jobs/pricing.py": TargetOutcome(
                passed=True,
                mutants_generated=1,
                mutants_killed=1,
            ),
        }
    )

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        coverage_gate=95.0,
        mutation_gate=40.0,
        enable_ci_mutation_sampling=True,
        **kwargs,
    )

    assert evidence["mutation"]["rate"] == 50.0
    assert evidence["mutation"]["passed"] is True
    assert evidence["mutation"]["gateScope"] == "report"
    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert evidence["passed"] is True
    assert evidence["reasonCode"] == "passed"
    failing = [
        item
        for item in evidence["targetResults"]
        if item["target"]["source_path"] == "jobs/forecast.py"
    ][0]
    assert failing["status"] == "passed"
    assert failing["reasonCode"] == "sampled_mutation_diagnostic"


def test_python_ci_sampling_fails_when_report_aggregate_is_below_gate(tmp_path):
    """Removing the per-target gate must not remove the gate."""
    repo = tmp_path / "repo"
    _init_multi_file_repo(repo)
    kwargs, _calls = stub_for(
        {
            path: TargetOutcome(
                passed=False,
                reason_code="mutation_gate_failed",
                message="survivor",
                mutants_generated=1,
                mutants_killed=0,
            )
            for path in ("jobs/forecast.py", "jobs/pricing.py")
        }
    )

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        coverage_gate=95.0,
        mutation_gate=40.0,
        enable_ci_mutation_sampling=True,
        **kwargs,
    )

    assert evidence["mutation"]["rate"] == 0.0
    assert evidence["mutation"]["passed"] is False
    assert evidence["mutation"]["gateScope"] == "report"
    assert evidence["passed"] is False
    assert evidence["reasonCode"] == "mutation_gate_failed"
    assert "aggregate mutation score" in evidence["summary"]


def test_ci_reclassifies_binding_mutation_failure_but_not_coverage_failure():
    from uta.language.python.enforcement import (
        _has_unsampled_failure,
        _reclassify_sampled_mutation_misses,
    )

    results = [
        {"status": "failed", "reasonCode": "mutation_failed", "testsPass": True,
         "coverage": {"passed": True}, "mutation": {"rate": 92.3}},
        {"status": "passed", "reasonCode": "python_large_change_skipped"},
    ]
    _reclassify_sampled_mutation_misses(results)

    assert results[0]["status"] == "passed"
    assert results[0]["reasonCode"] == "sampled_mutation_diagnostic"
    assert not _has_unsampled_failure(results)

    results.append({"status": "failed", "reasonCode": "coverage_failed"})
    assert _has_unsampled_failure(results)


def test_python_non_mutation_failures_still_gate_under_sampling(tmp_path):
    """Only the sampled mutation score loses its gating power.

    A test that will not import is not a sampling artefact, and reclassifying
    it would let the CI profile pass a broken build.
    """
    repo = tmp_path / "repo"
    _init_multi_file_repo(repo)
    kwargs, _calls = stub_for(
        {
            "jobs/forecast.py": TargetOutcome(
                passed=False,
                reason_code="tests_failed",
                message="collection error",
            ),
            "jobs/pricing.py": TargetOutcome(
                passed=True, mutants_generated=1, mutants_killed=1
            ),
        }
    )

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        coverage_gate=95.0,
        mutation_gate=40.0,
        enable_ci_mutation_sampling=True,
        **kwargs,
    )

    assert evidence["passed"] is False
    assert evidence["reasonCode"] == "tests_failed"


def test_python_deletion_only_change_is_not_verified(tmp_path, binding_refuses_to_verify):
    """A file that only lost lines has nothing to verify.

    `git diff` reports no added lines for a deletion-only change, so the target
    carries no changed-line obligation. Letting it through to verification made
    coverage fall back to the whole file and invent an obligation the diff never
    created -- one such file cost a real report ~8 points of aggregate coverage.

    `binding_refuses_to_verify` is the point: the target must be refused before
    any verification runs, not merely reported as passing afterwards.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "jobs" / "forecast.py").write_text(
        "def run():\n    unused = 2\n    return 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base with dead assignment")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    # Removing a line and adding none: `git diff` reports no added lines.
    (repo / "jobs" / "forecast.py").write_text(
        "def run():\n    return 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "drop the dead assignment")

    evidence = run_python_enforcement(
        repo_path=repo,
        target_values=[],
        test_paths=["tests"],
        coverage_gate=95.0,
        mutation_gate=95.0,
        **binding_refuses_to_verify,
    )

    assert evidence["status"] == PythonEnforcementStatus.passed.value
    assert evidence["coverage"]["no_executable_changed_lines"] is True
    assert evidence["coverage"]["total"] == 0
    assert evidence["mutation"]["generated"] == 0
    assert evidence["targetResults"][0]["reasonCode"] == "no_executable_changed_lines"


def test_python_deletion_only_target_is_skipped_by_the_guard(tmp_path):
    """The guard itself, at the boundary the aggregate depends on."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = _target_refs(repo, [], base_ref="origin/master")[0]

    # No changed lines at all: a deletion-only or pure-rename change.
    assert _has_only_non_executable_changed_lines(repo, target, {"jobs/forecast.py": []}) is True
    # A real added line still has to be verified.
    assert _has_only_non_executable_changed_lines(repo, target, {"jobs/forecast.py": [2]}) is False


def test_python_diagnosis_ignores_skipped_targets(tmp_path):
    """A skipped target must not supply the report's failure reason.

    Skips carry a non-`passed` reason code (`no_executable_changed_lines`)
    while their status is `passed`. Walking targets on the reason code alone
    let a skip win the diagnosis, so a report with real test failures was
    labelled with a skip reason and the summary "Python enforcement passed" --
    while its status said failed. Observed on a 56-target production report:
    two deletion-only skips masked seven failing targets.
    """
    target_results = [
        {
            "target": {"source_path": "jobs/skipped.py"},
            "status": "passed",
            "reasonCode": "no_executable_changed_lines",
            "message": "Python enforcement passed; changed target lines are comments or whitespace",
        },
        {
            "target": {"source_path": "jobs/broken.py"},
            "status": "failed",
            "reasonCode": "test_failed",
            "message": "Python tests failed",
        },
    ]

    reason, summary = _diagnosis({"reasonCode": "quality_gate_failed"}, target_results, passed=False)

    assert reason == "test_failed"
    assert summary == "Python tests failed"
