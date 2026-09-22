from __future__ import annotations

import subprocess
from types import SimpleNamespace

from click.testing import CliRunner

from uta.app.cli import main
from uta.language.python.mutation_context import build_python_mutation_repair_context
from uta.language.python.verification.runner import (
    CommandEvidence,
    MutationSummary,
    PythonVerificationResult,
    parse_mutmut_survivors,
)


def test_parse_mutmut_survivors_keeps_mutmut_id_for_show():
    survivors = parse_mutmut_survivors(
        "SURVIVED jobs.forecast.x_forecast__mutmut_7 jobs/forecast.py:12 changed return value\n"
    )

    assert survivors == [
        {
            "file": "jobs/forecast.py",
            "line": 12,
            "description": "changed return value",
            "id": "jobs.forecast.x_forecast__mutmut_7",
        }
    ]


def test_parse_mutmut_survivors_accepts_id_only_mutmut3_results():
    survivors = parse_mutmut_survivors(
        "chat_robot.service.fine_tuning_service.x_update_run_from_callback__mutmut_63: survived\n"
    )

    assert survivors == [
        {
            "file": "",
            "line": 0,
            "description": "survived",
            "id": "chat_robot.service.fine_tuning_service.x_update_run_from_callback__mutmut_63",
        }
    ]


def test_python_mutation_repair_context_groups_by_symbol_and_writes_split_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def normalize_sku(value):\n"
        "    return value.strip().upper()\n"
        "\n"
        "class StoreForecast:\n"
        "    def predict(self, value):\n"
        "        return value + 1\n",
        encoding="utf-8",
    )
    survivors = [
        {"file": "jobs/forecast.py", "line": 2, "description": "changed return", "id": "jobs.forecast.x_normalize_sku__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 2, "description": "changed call", "id": "jobs.forecast.x_normalize_sku__mutmut_2"},
        {"file": "", "line": 0, "description": "changed math", "id": "jobs.forecast.xǁStoreForecastǁpredict__mutmut_1"},
    ]
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=3,
            killed=0,
            survived=51,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=survivors,
        ),
        commands=[
            CommandEvidence(
                name="mutmut_run",
                command=["mutmut", "run", "--paths-to-mutate", "jobs/forecast.py"],
                exit_code=1,
                stdout="51/51 🎉 0 🙁 51",
            )
        ],
    )
    show_calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        show_calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"--- before\n+++ after\n-mutant {cmd[-1]}\n+original {cmd[-1]}\n51/51 🎉 0 🙁 51\n",
            stderr="",
        )

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 2)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.mutation_repair_groups_per_round", 1)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        limit_per_symbol=1,
        runner=fake_run,
    )

    assert [group.symbol for group in context.groups] == ["normalize_sku", "StoreForecast.predict"]
    assert show_calls == [
        ["mutmut", "show", "jobs.forecast.x_normalize_sku__mutmut_1"],
        ["mutmut", "show", "jobs.forecast.x_normalize_sku__mutmut_2"],
        ["mutmut", "show", "jobs.forecast.xǁStoreForecastǁpredict__mutmut_1"],
    ]
    assert [len(group.representative_diffs) for group in context.groups] == [2, 1]
    full_context = (repo / ".uta_cache" / "python" / "mutation_repair" / "mutation-repair-context.md").read_text(encoding="utf-8")
    assert "mutmut run --paths-to-mutate jobs/forecast.py" not in full_context
    assert "mutmut show" not in full_context
    assert "Do not run mutation commands from this context artifact." in full_context
    assert "51/51" not in full_context
    assert "normalize_sku" in context.group_artifact_paths


def test_python_mutation_repair_context_uses_timeout_candidates_when_survivors_are_empty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "pipecat" / "testing" / "text_replay_injector.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import asyncio\n"
        "\n"
        "class TextSessionManager:\n"
        "    async def _gc_loop(self):\n"
        "        while True:\n"
        "            await asyncio.sleep(60)\n"
        "            if stale_session():\n"
        "                await self.remove('sid')\n",
        encoding="utf-8",
    )
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=3,
            killed=1,
            survived=0,
            no_coverage=0,
            rate=33.3333,
            gate=95.0,
            passed=False,
            timeout=2,
            survivors=[],
            diff_survivors=[],
            candidate_plan={
                "activeSelected": [
                    {
                        "toolCandidateKey": "pipecat.testing.text_replay_injector.xǁTextSessionManagerǁ_gc_loop__mutmut_4",
                        "opportunity": {
                            "sourcePath": "pipecat/testing/text_replay_injector.py",
                            "line": 7,
                            "operatorName": "comparison_boundary",
                        },
                    },
                    {
                        "toolCandidateKey": "pipecat.testing.text_replay_injector.xǁTextSessionManagerǁ_gc_loop__mutmut_5",
                        "opportunity": {
                            "sourcePath": "pipecat/testing/text_replay_injector.py",
                            "line": 8,
                            "operatorName": "call_argument",
                        },
                    },
                ]
            },
        ),
        commands=[
            CommandEvidence(
                name="mutmut_run",
                command=["mutmut", "run", "--paths-to-mutate", "pipecat/testing/text_replay_injector.py"],
                exit_code=1,
                stdout="3/3 🎉 1 ⏰ 2 🙁 0",
            )
        ],
    )
    show_calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        show_calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"--- before\n+++ after\n-mutant {cmd[-1]}\n+original {cmd[-1]}\n",
            stderr="",
        )

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 50)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:pipecat/testing/text_replay_injector.py",
        source_path="pipecat/testing/text_replay_injector.py",
        test_paths=["tests/uta_generated/test_pipecat_testing_text_replay_injector.py"],
        verification=verification,
        runner=fake_run,
    )

    assert context.survivor_count == 2
    assert context.failure_breakdown == {"timeout": 2}
    assert [group.symbol for group in context.groups] == ["TextSessionManager._gc_loop"]
    assert len(context.groups[0].survivors) == 2
    rendered = (repo / ".uta_cache" / "python" / "mutation_repair" / "mutation-repair-context.md").read_text(encoding="utf-8")
    assert "- Failed mutation candidates: 2 (timeout=2)" in rendered
    assert "Surviving mutants: 0" not in rendered
    assert "timeout mutation candidate" in rendered
    assert len(show_calls) == 2


def test_python_mutation_repair_context_uses_exact_persisted_timeout_candidate(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "processor" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def initialize():\n"
        "    logger.init_log(properties.get_env())\n",
        encoding="utf-8",
    )
    exact_diff = (
        "--- processor/main.py\n"
        "+++ processor/main.py\n"
        "@@ -2 +2 @@\n"
        "-    logger.init_log(properties.get_env())\n"
        "+    logger.init_log()\n"
    )
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=2,
            killed=1,
            survived=0,
            no_coverage=0,
            rate=50.0,
            gate=95.0,
            passed=False,
            timeout=1,
            candidate_plan={
                "activeSelected": [
                    {
                        "toolCandidateKey": "processor.main.x_initialize__mutmut_1",
                        "executionStatus": "killed",
                        "opportunity": {
                            "sourcePath": "processor/main.py",
                            "line": 2,
                            "operatorName": "call_argument",
                        },
                    },
                    {
                        "toolCandidateKey": "processor.main.x_initialize__mutmut_2",
                        "executionStatus": "timeout",
                        "mutmutShowCommand": "mutmut internal show processor.main.x_initialize__mutmut_2",
                        "mutmutShowOutput": exact_diff,
                        "opportunity": {
                            "sourcePath": "processor/main.py",
                            "line": 2,
                            "operatorName": "call_argument",
                        },
                    },
                ]
            },
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )

    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:processor/main.py",
        source_path="processor/main.py",
        test_paths=["tests/test_main.py"],
        verification=verification,
        runner=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mutmut show should not run")),
    )

    group = context.groups[0]
    assert [item["id"] for item in group.survivors] == ["processor.main.x_initialize__mutmut_2"]
    assert group.representative_diffs[0]["output"] == exact_diff.strip()
    rendered = (repo / ".uta_cache" / "python" / "mutation_repair" / "mutation-repair-context.md").read_text(
        encoding="utf-8"
    )
    assert "candidate inferred from deterministic candidate-plan order" not in rendered
    assert "+    logger.init_log()" in rendered


def test_python_mutation_repair_context_collects_diffs_only_for_selected_split_group(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def normalize_sku(value):\n"
        "    return value.strip().upper()\n"
        "\n"
        "class StoreForecast:\n"
        "    def predict(self, value):\n"
        "        return value + 1\n",
        encoding="utf-8",
    )
    survivors = [
        {"file": "jobs/forecast.py", "line": 2, "description": "changed return", "id": "jobs.forecast.x_normalize_sku__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 2, "description": "changed call", "id": "jobs.forecast.x_normalize_sku__mutmut_2"},
        {"file": "", "line": 0, "description": "changed math", "id": "jobs.forecast.xǁStoreForecastǁpredict__mutmut_1"},
    ]
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=3,
            killed=0,
            survived=51,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=survivors,
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )
    show_calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        show_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"diff for {cmd[-1]}", stderr="")

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 2)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.mutation_repair_groups_per_round", 1)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        limit_per_symbol=1,
        repair_attempt=2,
        runner=fake_run,
    )

    assert show_calls == [
        ["mutmut", "show", "jobs.forecast.x_normalize_sku__mutmut_1"],
        ["mutmut", "show", "jobs.forecast.x_normalize_sku__mutmut_2"],
    ]
    assert [len(group.representative_diffs) for group in context.groups] == [2, 0]


def test_python_mutation_repair_context_collects_multiple_groups_per_round(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def first(value):\n"
        "    return value + 1\n"
        "\n"
        "def second(value):\n"
        "    return value + 2\n"
        "\n"
        "def third(value):\n"
        "    return value + 3\n"
        "\n"
        "def fourth(value):\n"
        "    return value + 4\n",
        encoding="utf-8",
    )
    survivors = [
        {"file": "jobs/forecast.py", "line": 2, "description": "changed math", "id": "jobs.forecast.x_first__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 5, "description": "changed math", "id": "jobs.forecast.x_second__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 8, "description": "changed math", "id": "jobs.forecast.x_third__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 11, "description": "changed math", "id": "jobs.forecast.x_fourth__mutmut_1"},
    ]
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=4,
            killed=0,
            survived=80,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=survivors,
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )
    show_calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        show_calls.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout=f"diff for {cmd[-1]}", stderr="")

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 2)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.mutation_repair_groups_per_round", 3)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        limit_per_symbol=1,
        repair_attempt=1,
        runner=fake_run,
    )

    assert show_calls == [
        "jobs.forecast.x_first__mutmut_1",
        "jobs.forecast.x_fourth__mutmut_1",
        "jobs.forecast.x_second__mutmut_1",
        "jobs.forecast.x_third__mutmut_1",
    ]
    assert [len(group.representative_diffs) for group in context.groups] == [1, 1, 1, 1]
    assert set(context.group_artifact_paths) == {"first", "fourth", "second", "third"}


def test_python_mutation_repair_context_preserves_roi_for_all_groups_on_first_round(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def cheap(value):\n"
        "    return value + 1\n"
        "\n"
        "def expensive(value):\n"
        "    return value + 2\n",
        encoding="utf-8",
    )
    survivors = [
        {"file": "jobs/forecast.py", "line": 2, "description": "changed math", "id": "jobs.forecast.x_cheap__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 5, "description": "changed math", "id": "jobs.forecast.x_expensive__mutmut_1"},
    ]
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=2,
            killed=0,
            survived=2,
            no_coverage=0,
            rate=0.0,
            gate=95.0,
            passed=False,
            survivors=survivors,
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        return subprocess.CompletedProcess(cmd, 0, stdout=f"diff for {cmd[-1]}", stderr="")

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 1)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.mutation_repair_groups_per_round", 1)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        limit_per_symbol=1,
        repair_attempt=1,
        runner=fake_run,
        method_efforts=[
            {"name": "cheap", "effort_score": 1},
            {"name": "expensive", "effort_score": 6},
        ],
    )

    assert [group.symbol for group in context.groups] == ["cheap", "expensive"]
    assert all(group.roi is not None for group in context.groups)
    assert "roi " in (repo / ".uta_cache" / "python" / "mutation_repair" / "mutation-repair-context.md").read_text(encoding="utf-8")


def test_python_mutation_repair_context_splits_large_symbol_into_exact_batches(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def normalize_sku(value):\n    return value.strip().upper()\n", encoding="utf-8")
    survivors = [
        {
            "file": "jobs/forecast.py",
            "line": 2,
            "description": f"changed return {index}",
            "id": f"jobs.forecast.x_normalize_sku__mutmut_{index}",
        }
        for index in range(1, 76)
    ]
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=75,
            killed=0,
            survived=75,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=survivors,
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )
    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 50)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.mutation_repair_groups_per_round", 1)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_batch_max_survivors", 40)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_exact_diff_limit", 60)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutant_show_total_timeout_seconds", 999)

    def collect(attempt):
        show_calls = []

        def fake_run(cmd, cwd=None, timeout=None, env=None):
            show_calls.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0, stdout=f"diff for {cmd[-1]}", stderr="")

        context = build_python_mutation_repair_context(
            repo=repo,
            target_id="pyfile:jobs/forecast.py",
            source_path="jobs/forecast.py",
            test_paths=["tests/test_forecast.py"],
            verification=verification,
            limit_per_symbol=3,
            repair_attempt=attempt,
            runner=fake_run,
            artifact_dir=repo / ".uta_cache" / "python" / "mutation_repair" / f"batch-{attempt}",
        )
        return context, show_calls

    first, first_calls = collect(1)
    assert [group.symbol for group in first.groups] == [
        "normalize_sku#batch-1-of-2",
        "normalize_sku#batch-2-of-2",
    ]
    assert len(first.groups[0].survivors) == 40
    assert len(first.groups[0].representative_diffs) == 40
    assert len(first.groups[1].survivors) == 35
    assert len(first.groups[1].representative_diffs) == 35
    assert first_calls == [f"jobs.forecast.x_normalize_sku__mutmut_{index}" for index in range(1, 76)]

    second, second_calls = collect(2)
    assert len(second.groups[0].survivors) == 40
    assert len(second.groups[0].representative_diffs) == 40
    assert len(second.groups[1].survivors) == 3
    assert second_calls == [f"jobs.forecast.x_normalize_sku__mutmut_{index}" for index in range(1, 41)]


def test_python_mutation_repair_context_uses_exact_diffs_when_not_split(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def normalize_sku(value):\n    return value.strip().upper()\n", encoding="utf-8")
    survivors = [
        {
            "file": "jobs/forecast.py",
            "line": 2,
            "description": f"changed return {index}",
            "id": f"jobs.forecast.x_normalize_sku__mutmut_{index}",
        }
        for index in range(1, 7)
    ]
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=6,
            killed=0,
            survived=6,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=survivors,
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 50)

    show_calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        show_calls.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout=f"diff for {cmd[-1]}", stderr="")

    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        limit_per_symbol=2,
        repair_attempt=1,
        runner=fake_run,
    )

    assert [item["id"] for item in context.groups[0].survivors] == show_calls
    assert show_calls == [
        f"jobs.forecast.x_normalize_sku__mutmut_{index}"
        for index in range(1, 7)
    ]


def test_python_mutation_repair_context_focuses_latest_top_split_symbol_after_first_round(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def normalize_sku(value):\n"
        "    return value.strip().upper()\n"
        "\n"
        "def score(value):\n"
        "    return value + 1\n",
        encoding="utf-8",
    )
    survivors = [
        {"file": "jobs/forecast.py", "line": 2, "description": "changed return", "id": "jobs.forecast.x_normalize_sku__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 2, "description": "changed call", "id": "jobs.forecast.x_normalize_sku__mutmut_2"},
        {"file": "jobs/forecast.py", "line": 2, "description": "changed call", "id": "jobs.forecast.x_normalize_sku__mutmut_3"},
        {"file": "jobs/forecast.py", "line": 5, "description": "changed math", "id": "jobs.forecast.x_score__mutmut_1"},
        {"file": "jobs/forecast.py", "line": 5, "description": "changed math", "id": "jobs.forecast.x_score__mutmut_2"},
    ]
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=5,
            killed=0,
            survived=51,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=survivors,
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )
    show_calls = []

    def fake_run(cmd, cwd=None, timeout=None, env=None):
        show_calls.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout=f"diff for {cmd[-1]}", stderr="")

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutation_repair_split_threshold", 4)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.mutation_repair_groups_per_round", 1)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        limit_per_symbol=1,
        repair_attempt=3,
        runner=fake_run,
    )

    assert [group.symbol for group in context.groups] == ["normalize_sku", "score"]
    assert show_calls == [
        "jobs.forecast.x_normalize_sku__mutmut_1",
        "jobs.forecast.x_normalize_sku__mutmut_2",
        "jobs.forecast.x_normalize_sku__mutmut_3",
    ]
    assert [group["id"] for group in context.groups[0].survivors] == show_calls


def test_python_mutation_repair_context_uses_stored_survivor_diff_without_show(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def normalize_sku(value):\n    return value.strip().upper()\n", encoding="utf-8")
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=1,
            killed=0,
            survived=1,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=[
                {
                    "file": "",
                    "line": 0,
                    "description": "survived",
                    "id": "jobs.forecast.x_normalize_sku__mutmut_1",
                    "mutmut_show_command": "mutmut show jobs.forecast.x_normalize_sku__mutmut_1",
                    "mutmut_show_output": "--- before\n+++ after\n-    return value.strip().upper()\n+    return value.strip().lower()",
                }
            ],
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=0)],
    )

    def fail_if_called(cmd, cwd=None, timeout=None, env=None):
        raise AssertionError(f"unexpected mutmut show call: {cmd}")

    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        runner=fail_if_called,
    )

    diff = context.groups[0].representative_diffs[0]
    assert diff["command"] == "mutmut show jobs.forecast.x_normalize_sku__mutmut_1"
    assert "value.strip().lower()" in diff["output"]


def test_python_mutation_repair_context_uses_internal_mutmut_diff_before_subprocess(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    mutants_source = repo / "mutants" / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    mutants_source.parent.mkdir(parents=True)
    source.write_text("def normalize_sku(value):\n    return value.strip().upper()\n", encoding="utf-8")
    mutants_source.write_text("mutmut generated module", encoding="utf-8")
    mutant_id = "jobs.forecast.x_normalize_sku__mutmut_1"
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=1,
            killed=0,
            survived=1,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=[
                {"file": "", "line": 0, "description": "survived", "id": mutant_id}
            ],
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )

    class FakeCstModule:
        def __init__(self, body):
            self.body = body

        @property
        def code(self):
            return "\n".join(self.body)

    def fake_parse_module(source):
        assert source == "mutmut generated module"
        return "parsed mutants module"

    def fake_read_original(module, mutant_name):
        assert module == "parsed mutants module"
        assert mutant_name == mutant_id
        return "return value.upper()"

    def fake_read_mutant(module, mutant_name):
        assert module == "parsed mutants module"
        assert mutant_name == mutant_id
        return "return value.lower()"

    def fake_import_module(name):
        assert name == "mutmut.__main__"
        return SimpleNamespace(
            cst=SimpleNamespace(parse_module=fake_parse_module, Module=FakeCstModule),
            read_original_function=fake_read_original,
            read_mutant_function=fake_read_mutant,
        )

    def fail_if_called(cmd, cwd=None, timeout=None, env=None):
        raise AssertionError(f"unexpected subprocess mutmut show call: {cmd}")

    monkeypatch.setattr("uta.language.python.mutation_context.importlib.import_module", fake_import_module)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        runner=fail_if_called,
    )

    diff = context.groups[0].representative_diffs[0]
    assert diff["command"] == f"mutmut internal show {mutant_id} --path jobs/forecast.py"
    assert "return value.lower()" in diff["output"]


def test_python_mutation_repair_context_records_mutmut_show_timeout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def normalize_sku(value):\n    return value.strip().upper()\n", encoding="utf-8")
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=1,
            killed=0,
            survived=1,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=[
                {"file": "jobs/forecast.py", "line": 2, "description": "changed return", "id": "jobs.forecast.x_normalize_sku__mutmut_1"}
            ],
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )
    seen_timeouts = []

    def slow_show(cmd, cwd=None, timeout=None, env=None):
        seen_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd, timeout=timeout)

    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutant_show_timeout_seconds", 7)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutant_show_total_timeout_seconds", 20)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        runner=slow_show,
    )

    assert seen_timeouts == [7]
    diff = context.groups[0].representative_diffs[0]
    assert diff["command"] == "mutmut show jobs.forecast.x_normalize_sku__mutmut_1"
    assert "timed out after 7s" in diff["output"]


def test_python_mutation_repair_context_stops_show_calls_when_total_budget_exhausts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def normalize_sku(value):\n    return value.strip().upper()\n", encoding="utf-8")
    verification = PythonVerificationResult(
        status="failed",
        reason_code="mutation_gate_failed",
        tests_pass=True,
        mutation=MutationSummary(
            runtime_lane="mutmut-modern",
            generated=2,
            killed=0,
            survived=2,
            no_coverage=0,
            rate=0.0,
            gate=100.0,
            passed=False,
            survivors=[
                {"file": "jobs/forecast.py", "line": 2, "description": "changed return", "id": "jobs.forecast.x_normalize_sku__mutmut_1"},
                {"file": "jobs/forecast.py", "line": 2, "description": "changed call", "id": "jobs.forecast.x_normalize_sku__mutmut_2"},
            ],
        ),
        commands=[CommandEvidence(name="mutmut_run", command=["mutmut", "run"], exit_code=1)],
    )
    show_calls = []
    clock = iter([0.0, 0.0, 2.0])

    def fake_monotonic():
        return next(clock)

    def fake_show(cmd, cwd=None, timeout=None, env=None):
        show_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"diff for {cmd[-1]}", stderr="")

    monkeypatch.setattr("uta.language.python.mutation_context.time.monotonic", fake_monotonic)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutant_show_timeout_seconds", 10)
    monkeypatch.setattr("uta.language.python.mutation_context.settings.python_mutant_show_total_timeout_seconds", 1)
    context = build_python_mutation_repair_context(
        repo=repo,
        target_id="pyfile:jobs/forecast.py",
        source_path="jobs/forecast.py",
        test_paths=["tests/test_forecast.py"],
        verification=verification,
        limit_per_symbol=2,
        runner=fake_show,
    )

    assert show_calls == [["mutmut", "show", "jobs.forecast.x_normalize_sku__mutmut_1"]]
    outputs = [diff["output"] for diff in context.groups[0].representative_diffs]
    assert outputs == [
        "diff for jobs.forecast.x_normalize_sku__mutmut_1",
        "[mutmut show unavailable: skipped because the mutmut show total budget was exhausted]",
    ]


def test_python_mutant_diffs_cli_writes_artifact(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("def forecast(value):\n    return value + 1\n", encoding="utf-8")
    survivors = repo / ".uta_cache" / "python" / "mutation" / "survivors.json"
    survivors.parent.mkdir(parents=True)
    survivors.write_text(
        '[{"file":"jobs/forecast.py","line":2,"description":"changed math"}]',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "python-mutant-diffs",
            "--repo",
            str(repo),
            "--target",
            "jobs/forecast.py",
            "--test-path",
            "tests/test_forecast.py",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (repo / ".uta_cache" / "python" / "mutation_repair" / "mutation-repair-context.md").is_file()
    assert '"artifact"' in result.output
