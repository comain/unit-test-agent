from pathlib import Path

from uta.graph.nodes import (
    _allowed_llm_path,
    _clean_rerun_artifacts,
    _module_from_source_path,
    _verify_task_branch_and_preexisting_diff,
    setup_branch,
)


def test_clean_rerun_artifacts_preserves_cache_reports_and_summary_but_removes_other_untracked_artifacts(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cache_file = repo / ".uta_cache" / "context" / "keep.md"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("keep", encoding="utf-8")
    report = repo / ".uta_reports" / "summary.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}", encoding="utf-8")
    test_file = repo / "biz" / "src" / "test" / "java" / "FooTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class FooTest {}", encoding="utf-8")
    stray_file = repo / "provider" / "src" / "test" / "java" / "com" / "example" / "Unexpected.java"
    stray_file.parent.mkdir(parents=True)
    stray_file.write_text("class Unexpected {}", encoding="utf-8")
    summary = repo / ".uta_summary.md"
    summary.write_text("generated", encoding="utf-8")
    opencode_config = repo / "opencode.json"
    opencode_config.write_text("{}", encoding="utf-8")

    def fake_run(cmd, capture_output=True, check=False, cwd=None, **kwargs):
        class R:
            returncode = 0
            stdout = b"biz/src/test/java/FooTest.java\nprovider/src/test/java/com/example/Unexpected.java\n.uta_summary.md\nopencode.json\n.uta_cache/context/keep.md\n.uta_reports/summary.json\n"
            stderr = b""
        return R()

    monkeypatch.setattr("uta.graph.nodes.subprocess.run", fake_run)
    _clean_rerun_artifacts(str(repo))

    assert cache_file.exists()
    assert report.exists()
    assert not test_file.exists()
    assert not stray_file.exists()
    assert summary.exists()
    assert opencode_config.exists()


def test_setup_branch_recreates_branch_from_default(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def fake_run(cmd, capture_output=True, check=False, cwd=None, **kwargs):
        calls.append((cmd, cwd))

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        if cmd[:4] == ["git", "rev-parse", "--verify", "origin/master"] and cwd == str(repo):
            return R()
        return R()

    monkeypatch.setattr("uta.graph.nodes.subprocess.run", fake_run)
    monkeypatch.setattr("uta.graph.nodes._clean_rerun_artifacts", lambda repo_path: calls.append((["clean", repo_path], None)))

    out = setup_branch({"repo_path": str(repo), "branch_name": "unit-code-gen"})

    assert any(cmd[:3] == ["git", "fetch", "origin"] and cwd == str(repo) for cmd, cwd in calls)
    assert any(cmd[:6] == ["git", "checkout", "-B", "unit-code-gen", "origin/master", "-f"] and cwd == str(repo) for cmd, cwd in calls)
    assert any(cmd[:4] == ["git", "reset", "--hard", "origin/master"] and cwd == str(repo) for cmd, cwd in calls)
    assert (["clean", str(repo)], None) in calls
    assert "phase_timings" in out


def test_setup_branch_reuses_existing_branch_without_reset_or_clean(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def fake_run(cmd, capture_output=True, check=False, cwd=None, **kwargs):
        calls.append((cmd, cwd))

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        if cmd[:3] == ["git", "branch", "--show-current"] and cwd == str(repo):
            R.stdout = b"feature/calibration\n"
        return R()

    monkeypatch.setattr("uta.graph.nodes.subprocess.run", fake_run)
    monkeypatch.setattr("uta.graph.nodes._clean_rerun_artifacts", lambda repo_path: calls.append((["clean", repo_path], None)))

    out = setup_branch(
        {
            "repo_path": str(repo),
            "branch_name": "feature/calibration",
            "preserve_branch": True,
        }
    )

    assert all("reset" not in cmd for cmd, _cwd in calls if isinstance(cmd, list))
    assert all("checkout" not in cmd for cmd, _cwd in calls if isinstance(cmd, list))
    assert (["clean", str(repo)], None) not in calls
    assert "phase_timings" in out


def test_setup_branch_checkouts_existing_branch_without_force(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = []

    def fake_run(cmd, capture_output=True, check=False, cwd=None, **kwargs):
        calls.append((cmd, cwd))

        class R:
            returncode = 0
            stdout = b""
            stderr = b""

        if cmd[:3] == ["git", "branch", "--show-current"] and cwd == str(repo):
            R.stdout = b"other\n"
        return R()

    monkeypatch.setattr("uta.graph.nodes.subprocess.run", fake_run)
    monkeypatch.setattr("uta.graph.nodes._clean_rerun_artifacts", lambda repo_path: calls.append((["clean", repo_path], None)))

    setup_branch(
        {
            "repo_path": str(repo),
            "branch_name": "feature/calibration",
            "preserve_branch": True,
        }
    )

    assert (["git", "checkout", "feature/calibration"], str(repo)) in calls
    assert all(cmd[:3] != ["git", "reset", "--hard"] for cmd, _cwd in calls if isinstance(cmd, list))
    assert (["clean", str(repo)], None) not in calls


def test_preexisting_diff_guard_allows_deterministic_setup_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project />", encoding="utf-8")

    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "uta@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "UTA"], cwd=repo, check=True)
    subprocess.run(["git", "add", "pom.xml"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "pom.xml").write_text("<project><dependencies /></project>", encoding="utf-8")
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _verify_task_branch_and_preexisting_diff(
        {
            "repo_path": str(repo),
            "branch_name": current_branch,
            "task_id": 1,
            "task_db_path": str(tmp_path / "tasks.db"),
            "deterministic_change_paths": ["pom.xml"],
        },
        [],
    )


def test_llm_diff_guard_allows_module_scoped_test_path_when_module_is_all():
    class_fqn = "com.example.biz.ActorDataLoader"
    state = {"module": None, "selected_classes": [class_fqn]}

    assert _allowed_llm_path(
        "biz/src/test/java/com/example/biz/ActorDataLoaderTest.java",
        state,
        [class_fqn],
    )
    assert _allowed_llm_path(
        "biz/src/test/java/com/example/biz/ReceiptFinishActorTest.java",
        state,
        [class_fqn],
    )
    assert not _allowed_llm_path(
        "biz/src/main/java/com/example/biz/ActorDataLoader.java",
        state,
        [class_fqn],
    )


def test_llm_diff_guard_allows_test_resources():
    class_fqn = "com.example.service.StateMachine"
    state = {"module": None, "selected_classes": [class_fqn]}

    assert _allowed_llm_path(
        "service/src/test/resources/statemachine.json",
        state,
        [class_fqn],
    )


def test_llm_diff_guard_allows_sisyphus_runtime_state():
    state = {"module": None, "selected_classes": ["com.example.service.StateMachine"]}

    assert _allowed_llm_path(
        ".sisyphus/run-continuation/ses_123.json",
        state,
        ["com.example.service.StateMachine"],
    )


def test_module_from_source_path_detects_maven_module(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "biz/src/main/java/com/example/Foo.java"

    assert _module_from_source_path(str(repo), str(source)) == "biz"
    assert _module_from_source_path(str(repo), str(repo / "src/main/java/com/example/Foo.java")) is None


def test_preexisting_diff_guard_allows_baseline_residue_without_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project />", encoding="utf-8")
    test_file = repo / "provider/src/test/java/com/example/ExistingTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("class ExistingTest {}", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Project\n", encoding="utf-8")

    import subprocess

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "uta@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "UTA"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "pom.xml").write_text("<project><dependencies /></project>", encoding="utf-8")
    test_file.write_text("import org.mockito.ArgumentMatchers;\nclass ExistingTest {}", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Project\n\nGenerated guidance\n", encoding="utf-8")
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    _verify_task_branch_and_preexisting_diff(
        {
            "repo_path": str(repo),
            "branch_name": current_branch,
            "task_id": 1,
            "task_db_path": str(tmp_path / "tasks.db"),
        },
        [],
    )
