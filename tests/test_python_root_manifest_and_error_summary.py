"""Fixes drawn from repo_tasks 175 and 176 (2026-09-09).

176: a repository whose only requirements.txt sat at its root got no dependency
overlay, and all 39 targets died on `ModuleNotFoundError: No module named
'django'` with nothing in the record naming the cause.

175: four classes failed for three different reasons, and the task error showed
only the first, so a run with three real gate misses read as pure tooling
breakage.
"""

from pathlib import Path

from uta_py_enforce.dependency_overlay import nearest_requirements_manifest
from uta.language.python.verification.runtime_setup import _skipped_overlay_evidence
from uta.tasks.accounting.results import TaskResultSyncMixin


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return repo


def test_root_manifest_is_used_when_nothing_nearer_exists(tmp_path):
    """The 176 case: one manifest, at the root, previously ignored."""
    repo = _repo(tmp_path, {
        "requirements.txt": "Django==3.2.25\n",
        "common/auth.py": "import django\n",
    })

    assert nearest_requirements_manifest(repo, "common/auth.py") == repo / "requirements.txt"


def test_nested_manifest_still_wins_over_the_root(tmp_path):
    """Per-service repositories must keep installing the service's own manifest."""
    repo = _repo(tmp_path, {
        "requirements.txt": "django==3.2.25\n",
        "pipecat/requirements.txt": "fastapi>=0.110\n",
        "pipecat/router/session_store.py": "import fastapi\n",
    })

    found = nearest_requirements_manifest(repo, "pipecat/router/session_store.py")
    assert found == repo / "pipecat" / "requirements.txt"


def test_empty_root_manifest_is_not_selected(tmp_path):
    """An empty manifest is not a manifest; it must not mask a missing one."""
    repo = _repo(tmp_path, {"requirements.txt": "\n  \n", "app/main.py": "x = 1\n"})

    assert nearest_requirements_manifest(repo, "app/main.py") is None


def test_absent_overlay_is_recorded_with_the_directories_searched(tmp_path):
    repo = _repo(tmp_path, {"app/main.py": "x = 1\n"})

    evidence = _skipped_overlay_evidence(repo, "app/main.py")

    assert evidence.name == "dependency_overlay_skipped"
    # Skipping is not a failure: repositories with no third-party requirements
    # are ordinary and must not be turned red by this record.
    assert evidence.exit_code == 0
    assert "no dependency overlay" in evidence.stderr
    assert (repo / "app").resolve().as_posix() in evidence.stderr
    assert repo.resolve().as_posix() in evidence.stderr


def test_a_manifest_declaring_nothing_installable_is_not_selected(tmp_path):
    """Comments and options are not requirements, at any depth."""
    repo = _repo(tmp_path, {
        "requirements.txt": "# nothing installable\n--index-url https://example.invalid\n",
        "app/main.py": "x = 1\n",
    })

    assert nearest_requirements_manifest(repo, "app/main.py") is None
    assert "installable" in _skipped_overlay_evidence(repo, "app/main.py").stderr


def test_a_comments_only_nested_manifest_does_not_shadow_the_root(tmp_path):
    """The regression this fix's own first cut introduced.

    Making the root reachable is worthless if a nearer placeholder manifest
    hides it: the lookup would return the nested file, callers would find
    nothing to install and skip the overlay, and the repository would be back
    to no overlay at all -- the 176 failure by a subtler route.
    """
    repo = _repo(tmp_path, {
        "requirements.txt": "Django==3.2.25\n",
        "svc/requirements.txt": "# TODO: fill this in\n",
        "svc/app.py": "import django\n",
    })

    assert nearest_requirements_manifest(repo, "svc/app.py") == repo / "requirements.txt"


def test_a_nested_manifest_of_only_an_include_still_wins(tmp_path):
    """Includes are followed, so an indirection is still a real manifest."""
    repo = _repo(tmp_path, {
        "requirements.txt": "Django==3.2.25\n",
        "svc/base.txt": "fastapi>=0.110\n",
        "svc/requirements.txt": "-r base.txt\n",
        "svc/app.py": "import fastapi\n",
    })

    assert nearest_requirements_manifest(repo, "svc/app.py") == repo / "svc" / "requirements.txt"


def _rows(*pairs):
    return [{"display_name": name, "error": error, "last_error": None} for name, error in pairs]


def test_task_error_summarises_every_failing_class():
    """The 175 shape: one tooling fault must not hide three real gate misses."""
    summary = TaskResultSyncMixin._summary_error_message(_rows(
        ("com.example.WeChatLetAiService", "Missing or invalid PIT compatibility completion evidence"),
        ("com.example.CustomerSessionManager", "Mutation gate failed: 97% < 100%"),
        ("com.example.PromotionBizImpl", "Coverage gate failed: 0.00% < 95.00% (0/2)"),
    ))

    assert summary.startswith("3 classes failed:")
    assert "Mutation gate failed: 97% < 100%" in summary
    assert "Coverage gate failed" in summary
    # Class names are the reader's index into the report; keep the leaf name.
    assert "CustomerSessionManager" in summary
    assert "com.example" not in summary


def test_single_failure_is_reported_verbatim():
    """One failing class should read exactly as it did before this change."""
    summary = TaskResultSyncMixin._summary_error_message(
        _rows(("com.example.Only", "Coverage gate failed: 10.00% < 95.00%"))
    )

    assert summary == "Coverage gate failed: 10.00% < 95.00%"


def test_task_error_is_bounded_when_many_classes_fail():
    summary = TaskResultSyncMixin._summary_error_message(
        _rows(*[(f"pkg.Class{i}", "Mutation gate failed: 1% < 100%" + "x" * 400) for i in range(9)])
    )

    assert summary.startswith("9 classes failed:")
    assert "+5 more" in summary
    assert len(summary) < 1200


def test_task_error_is_none_when_no_class_carries_one():
    assert TaskResultSyncMixin._summary_error_message(
        [{"display_name": "pkg.Ok", "error": None, "last_error": None}]
    ) is None


def test_last_error_is_used_when_error_is_absent():
    summary = TaskResultSyncMixin._summary_error_message(
        [{"display_name": "pkg.A", "error": None, "last_error": "Coverage gate failed"}]
    )

    assert summary == "Coverage gate failed"


def test_adapter_self_abort_stays_inside_the_command_budget():
    """The adapter must win the race against the reap, at every budget."""
    from uta.language.python.verification.mutation_execution import _adapter_self_abort_seconds

    for budget in (120, 1800, 7200):
        seconds = _adapter_self_abort_seconds(budget)
        assert 0 < seconds < budget, budget
        # A long budget must not give away more than five minutes of headroom.
        assert budget - seconds <= 300


def test_adapter_self_abort_disabled_for_budgets_too_short_to_carve():
    """Better no watchdog than one that fires before real work can finish."""
    from uta.language.python.verification.mutation_execution import _adapter_self_abort_seconds

    assert _adapter_self_abort_seconds(60) == 0
    assert _adapter_self_abort_seconds(0) == 0
