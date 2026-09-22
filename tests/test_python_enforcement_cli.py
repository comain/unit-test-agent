import json
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from uta.app.cli import main

from uta.language.python.verification.runner import CoverageSummary, MutationSummary, PythonVerificationResult


import pytest

requires_mutmut = pytest.mark.skipif(
    subprocess.run(["which", "mutmut"], capture_output=True).returncode != 0,
    reason="mutmut is not installed",
)

from binding_stubs import SelectingBinding, patch_binding


@pytest.fixture
def binding(monkeypatch):
    """Real selection, faked verification.

    These tests used to stub `verify_python_target`, a seam that no longer
    exists -- the CLI now goes through the enforcement binding. Stubbing the
    whole binding would take selection with it and make every `selectedTest`
    assertion below an assertion about the stub, so the stub keeps the real
    `strict_test_candidates` and fakes only the pytest and mutmut run.
    """
    stub = SelectingBinding()
    patch_binding(monkeypatch, stub)
    return stub


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo_with_python_change(repo: Path) -> None:
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


def test_python_enforce_cli_executes_core_and_prints_json(binding, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests/test_forecast.py",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    assert payload["reasonCode"] == "passed"
    assert payload["targets"][0]["target_id"] == "pyfile:jobs/forecast.py"
    assert binding.selected[0] == ["tests/test_forecast.py"]
    assert payload["targetResults"][0]["selectedTestPaths"] == ["tests/test_forecast.py"]
    assert payload["targetResults"][0]["configuredTestPaths"] == ["tests/test_forecast.py"]


def test_python_enforce_cli_attaches_test_quality_for_selected_test(binding, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n\ndef test_run():\n    assert run() is not None\n",
        encoding="utf-8",
    )

    def fake_verify(*args, **kwargs):
        return PythonVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=CoverageSummary(covered=2, total=2, rate=100.0, gate=95.0, passed=True, xml_path=".uta_cache/python/coverage/coverage.xml"),
            mutation=MutationSummary(
                runtime_lane="mutmut-modern",
                generated=4,
                killed=4,
                survived=0,
                no_coverage=0,
                rate=100.0,
                gate=100.0,
                passed=True,
            ),
        )


    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests/test_forecast.py",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    quality = payload["targetResults"][0]["testQuality"]
    assert quality["warningCount"] == 2
    assert quality["warnings"][0]["ruleId"] == "python-weak-assert-not-none"
    assert quality["warnings"][1]["ruleId"] == "python-happy-path-only-hint"


def test_python_enforce_cli_uses_selected_test_for_target_evidence(binding, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)
    (repo / "tests" / "test_unrelated.py").write_text("def test_unrelated():\n    assert True\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert binding.selected[0] == ["tests/test_forecast.py"]
    assert payload["targetResults"][0]["selectedTestPaths"] == ["tests/test_forecast.py"]
    assert payload["targetResults"][0]["configuredTestPaths"] == ["tests"]


def test_python_enforce_cli_ignores_explicit_broad_test_path(binding, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    source = repo / "chat_robot" / "service" / "fine_tuning_service.py"
    source.parent.mkdir(parents=True)
    source.write_text("def dispatch_fine_tuning_run():\n    return True\n", encoding="utf-8")
    broad = repo / "tests" / "test_fine_tuning_pure.py"
    broad.parent.mkdir(parents=True)
    broad.write_text(
        "from chat_robot.service import fine_tuning_service\n\n"
        "def test_dispatch():\n"
        "    assert fine_tuning_service.dispatch_fine_tuning_run()\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    source.write_text("def dispatch_fine_tuning_run():\n    return False\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "chat_robot/service/fine_tuning_service.py",
            "--test-path",
            "tests/test_fine_tuning_pure.py",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    # Selection ran and found nothing strict: the configured path is a broad
    # test, and a broad test does not verify a target.
    assert binding.selected == [[]]
    # The binding names this condition `missing_test_file`; the deleted lane
    # called it `missing_selected_test_evidence`. Nothing in the product
    # reads either string -- only this assertion did.
    assert payload["targetResults"][0]["reasonCode"] == "missing_test_file"
    assert payload["targetResults"][0]["candidateTestPaths"] == []
    assert payload["targetResults"][0]["configuredTestPaths"] == ["tests/test_fine_tuning_pure.py"]


def test_python_enforce_cli_skips_runtime_incompatible_target_without_selected_tests(binding, monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)
    (repo / "jobs" / "legacy_only.py").write_text("value = None\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "legacy target")

    def fake_precheck(_repo, target, **kwargs):
        """Stand in for a py_compile failure without needing a py2 file."""
        return PythonVerificationResult(
            status="passed",
            reason_code="python_runtime_incompatible_skipped",
            tests_pass=True,
            coverage=CoverageSummary(
                covered=0, total=0, rate=100.0,
                gate=float(kwargs.get("coverage_gate") or 95.0),
                passed=True, xml_path="", scope="changed_lines",
            ),
            mutation=MutationSummary(
                runtime_lane="not_run",
                generated=0, killed=0, survived=0, no_coverage=0,
                rate=100.0, gate=float(kwargs.get("mutation_gate") or 100.0),
                passed=True, scope="changed_lines",
            ),
            message="Python target is incompatible with the configured verification runtime",
        )

    monkeypatch.setattr(
        "uta.language.python.enforcement.precheck_python_target_runtime_incompatibility",
        fake_precheck,
    )

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/legacy_only.py",
            "--test-path",
            "tests/test_forecast.py",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    assert payload["reasonCode"] == "passed"
    assert payload["targetResults"][0]["reasonCode"] == "python_runtime_incompatible_skipped"
    assert payload["targetResults"][0]["selectedTestPaths"] == []
    # Skipped before the binding is asked to do anything: enforcing a file
    # this interpreter cannot compile would report "no test file" for code no
    # test on this lane could cover, and that failure opens an LLM repair
    # which cannot succeed.
    assert binding.requests == []


def test_python_enforce_cli_prefers_existing_uta_generated_target_test(binding, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)
    generated = repo / "tests" / "uta_generated" / "test_jobs_forecast.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("from jobs.forecast import run\n\n\ndef test_generated_run():\n    assert run() == 2\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Preference is order, not exclusion: the binding reports every strict
    # candidate and verifies them in order, so "prefers" means "first".
    assert binding.selected[0][0] == "tests/uta_generated/test_jobs_forecast.py"
    assert payload["targetResults"][0]["selectedTestPaths"] == ["tests/uta_generated/test_jobs_forecast.py"]


def test_python_enforce_cli_ignores_broad_tests_and_uses_strict_target_test(binding, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    source = repo / "tools" / "wconfig_util.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def get_dify_env():\n"
        "    return 'base'\n\n"
        "def get_from_json_file(path):\n"
        "    return path\n\n",
        encoding="utf-8",
    )
    pure = repo / "tests" / "test_fine_tuning_pure.py"
    pure.parent.mkdir(parents=True)
    pure.write_text(
        "from tools import wconfig_util\n\n"
        "def test_wconfig_symbols():\n"
        "    assert wconfig_util.get_dify_env()\n"
        "    assert wconfig_util.get_from_json_file('x')\n",
        encoding="utf-8",
    )
    check = repo / "chat_robot" / "test" / "check_test.py"
    check.parent.mkdir(parents=True)
    check.write_text(
        "from tools import wconfig_util\n\n"
        "def test_check_wconfig():\n"
        "    assert wconfig_util.get_dify_env()\n",
        encoding="utf-8",
    )
    strict = repo / "tests" / "test_wconfig_util.py"
    strict.write_text(
        "from tools import wconfig_util\n\n"
        "def test_wconfig_util_gets_env():\n"
        "    assert wconfig_util.get_dify_env()\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    source.write_text(source.read_text(encoding="utf-8") + "\ndef get_data_env():\n    return 'changed'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "tools/wconfig_util.py",
            "--test-path",
            "tests",
            "--test-path",
            "chat_robot/test",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert binding.selected[0] == ["tests/test_wconfig_util.py"]
    assert payload["targetResults"][0]["candidateTestPaths"] == ["tests/test_wconfig_util.py"]
    assert payload["targetResults"][0]["selectedTestPaths"] == ["tests/test_wconfig_util.py"]


@requires_mutmut
def test_python_enforce_cli_passes_when_any_strict_candidate_passes(tmp_path, monkeypatch):
    """Deliberately unstubbed.

    The subject is the binding's own candidate walk -- try each strict
    candidate until one verifies the target -- and any stub that reports a
    result replaces the very loop under test. So this one runs pytest and
    mutmut for real, on a repo whose first candidate fails and whose second
    passes.
    """
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)
    monkeypatch.setenv("UTA_PYTHON_BIN", sys.executable)
    second = repo / "tests" / "test_jobs_forecast.py"
    second.write_text("from jobs.forecast import run\n\n\ndef test_run_again():\n    assert run() == 2\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests",
            "--coverage-gate",
            "95",
            "--mutation-gate",
            "100",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    target_result = payload["targetResults"][0]
    assert target_result["candidateTestPaths"] == ["tests/test_forecast.py", "tests/test_jobs_forecast.py"]
    assert target_result["selectedTestPaths"] == ["tests/test_jobs_forecast.py"]
    assert [item["status"] for item in target_result["candidateResults"]] == ["failed", "passed"]


def test_python_enforce_cli_prints_test_enforcer_markers(binding, tmp_path):
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)

    def fake_verify(*args, **kwargs):
        return PythonVerificationResult(
            status="passed",
            reason_code="passed",
            tests_pass=True,
            coverage=CoverageSummary(covered=1, total=1, rate=100.0, gate=95.0, passed=True, xml_path=".uta_cache/python/coverage/coverage.xml"),
            mutation=MutationSummary(
                runtime_lane="mutmut-modern",
                generated=1,
                killed=1,
                survived=0,
                no_coverage=0,
                rate=100.0,
                gate=100.0,
                passed=True,
            ),
        )


    result = CliRunner().invoke(
        main,
        [
            "python-enforce",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests/test_forecast.py",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "[test-enforcer] python enforcement passed" in result.output
    assert "UTA_PYTHON_ENFORCEMENT_EVIDENCE=" in result.output


def test_python_enforce_cli_enables_sampling_only_for_internal_ci_profile(binding, monkeypatch, tmp_path):
    """Ported from main `68e3fc4`, against the binding rather than the deleted
    `verify_python_target` seam.

    The CI adapter runs enforcement in a subprocess for memory and
    process-group isolation, and the mutation profile has to survive that
    boundary. Without it CI silently enforces the local, uncapped profile --
    not a wrong verdict, an unbounded mutation run, which is the failure mode
    that hides.
    """
    from uta.language.python.ci_sampling import CiCapSamplingPolicy

    repo = tmp_path / "repo"
    _repo_with_python_change(repo)
    monkeypatch.setenv("UTA_INTERNAL_PYTHON_ENFORCEMENT_PROFILE", "ci_report_v1")

    result = CliRunner().invoke(
        main,
        ["python-enforce", "--repo", str(repo), "--target", "jobs/forecast.py",
         "--test-path", "tests/test_forecast.py", "--json-output"],
    )

    assert result.exit_code in (0, 1), result.output
    assert binding.sampling_policies, "the binding was never invoked"
    assert isinstance(binding.sampling_policies[0], CiCapSamplingPolicy)


def test_python_enforce_cli_leaves_sampling_off_without_the_profile(binding, monkeypatch, tmp_path):
    """A developer running the CLI by hand must get the full profile: sampling
    silently applied locally would hide surviving mutants from the one person
    positioned to fix them."""
    repo = tmp_path / "repo"
    _repo_with_python_change(repo)
    monkeypatch.delenv("UTA_INTERNAL_PYTHON_ENFORCEMENT_PROFILE", raising=False)

    CliRunner().invoke(
        main,
        ["python-enforce", "--repo", str(repo), "--target", "jobs/forecast.py",
         "--test-path", "tests/test_forecast.py", "--json-output"],
    )

    assert binding.sampling_policies == [None]


def _repo_with_large_python_change(repo: Path, *, changed_lines: int) -> None:
    """A repo whose one changed file is rewritten wholesale, not edited."""
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "jobs").mkdir()
    (repo / "jobs" / "bulk.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_bulk.py").write_text(
        "from jobs.bulk import run\n\n\ndef test_run():\n    assert run() == 1\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    body = "".join(f"    value_{index} = {index}\n" for index in range(changed_lines))
    (repo / "jobs" / "bulk.py").write_text(f"def run():\n{body}    return 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "bulk rewrite")


def test_python_enforce_cli_skips_target_whose_diff_exceeds_the_per_file_limit(binding, monkeypatch, tmp_path):
    """A wholesale-rewritten file is skipped with a warning, and the run passes.

    Coverage and mutation are diff-scoped, so a file the branch rewrote costs
    what a whole repo costs. Skipping it keeps the targets whose diffs really
    are incremental instead of losing the whole report to a timeout.
    """
    monkeypatch.setattr(
        "uta.language.python.verification.results.settings.python_enforcement_max_changed_lines_per_file",
        50,
    )
    repo = tmp_path / "repo"
    _repo_with_large_python_change(repo, changed_lines=200)

    result = CliRunner().invoke(
        main,
        [
            "python-enforce", "--repo", str(repo),
            "--target", "jobs/bulk.py",
            "--test-path", "tests/test_bulk.py",
            "--coverage-gate", "95", "--mutation-gate", "100", "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "passed"
    target_result = payload["targetResults"][0]
    assert target_result["reasonCode"] == "python_large_change_skipped"
    # The message has to name the cost that triggered the skip: the report says
    # this file passed, and a reader needs to know it passed unverified.
    assert "above the 50-line per-file enforcement limit" in target_result["message"]
    # Skipped before the binding runs -- the point is not paying for it.
    assert binding.requests == []


def test_python_enforce_cli_verifies_target_within_the_per_file_limit(binding, monkeypatch, tmp_path):
    """The skip is bounded: a diff under the limit is still verified normally."""
    monkeypatch.setattr(
        "uta.language.python.verification.results.settings.python_enforcement_max_changed_lines_per_file",
        500,
    )
    repo = tmp_path / "repo"
    _repo_with_large_python_change(repo, changed_lines=200)

    result = CliRunner().invoke(
        main,
        [
            "python-enforce", "--repo", str(repo),
            "--target", "jobs/bulk.py",
            "--test-path", "tests/test_bulk.py",
            "--coverage-gate", "95", "--mutation-gate", "100", "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["targetResults"][0]["reasonCode"] != "python_large_change_skipped"
    assert binding.requests != []


def test_python_enforce_cli_disables_the_per_file_limit_at_zero(binding, monkeypatch, tmp_path):
    """0 turns the skip off, for a caller who would rather wait than lose evidence."""
    monkeypatch.setattr(
        "uta.language.python.verification.results.settings.python_enforcement_max_changed_lines_per_file",
        0,
    )
    repo = tmp_path / "repo"
    _repo_with_large_python_change(repo, changed_lines=200)

    result = CliRunner().invoke(
        main,
        [
            "python-enforce", "--repo", str(repo),
            "--target", "jobs/bulk.py",
            "--test-path", "tests/test_bulk.py",
            "--coverage-gate", "95", "--mutation-gate", "100", "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["targetResults"][0]["reasonCode"] != "python_large_change_skipped"
    assert binding.requests != []
