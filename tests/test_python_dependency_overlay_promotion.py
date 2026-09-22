"""Tests for promoted nested dependency overlay resolution, caching, and concurrency locking."""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.dependency_overlay import (
    flatten_manifest,
    install_manifest_requirements,
    manifest_requirement_lines,
    nearest_requirements_manifest,
    prepare_dependency_overlay,
)


def test_nearest_requirements_manifest(tmp_path: Path):
    nested_dir = tmp_path / "services" / "payment"
    nested_dir.mkdir(parents=True)
    req_file = nested_dir / "requirements.txt"
    req_file.write_text("httpx>=0.27\npydantic>=2.0\n")

    source_file = nested_dir / "handlers" / "charge.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("import httpx\ndef charge(): pass")

    found = nearest_requirements_manifest(tmp_path, "services/payment/handlers/charge.py")
    assert found == req_file


def test_manifest_requirement_lines_ignores_comments_blanks_and_options():
    lines = [
        "# runtime",
        "",
        "--index-url https://example.invalid/simple",
        "pyjwt>=2.8  # tokens",
        "requests>=2.31",
    ]
    assert manifest_requirement_lines(lines) == ["pyjwt>=2.8", "requests>=2.31"]


def test_flatten_manifest_inlines_includes_and_absolutizes_paths(tmp_path: Path):
    base = tmp_path / "base.txt"
    base.write_text("requests>=2.31\n", encoding="utf-8")
    nested_dir = tmp_path / "svc"
    nested_dir.mkdir()
    (nested_dir / "requirements.txt").write_text(
        "-r ../base.txt\n-c ./pins.txt\npyjwt>=2.8\n",
        encoding="utf-8",
    )

    lines = flatten_manifest(nested_dir / "requirements.txt")

    assert "requests>=2.31" in lines
    assert "pyjwt>=2.8" in lines
    assert f"-c {(nested_dir / 'pins.txt').resolve()}" in lines
    assert not any(line.startswith("-r") for line in lines)


def test_flatten_manifest_tolerates_a_recursive_include(tmp_path: Path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("-r b.txt\nrequests>=2.31\n", encoding="utf-8")
    second.write_text("-r a.txt\npyjwt>=2.8\n", encoding="utf-8")

    assert manifest_requirement_lines(flatten_manifest(first)) == ["pyjwt>=2.8", "requests>=2.31"]


def test_prepare_dependency_overlay_installs_the_whole_manifest(tmp_path: Path):
    """Requirements the target never imports are still the target's environment."""
    nested_dir = tmp_path / "svc"
    nested_dir.mkdir()
    (nested_dir / "requirements.txt").write_text(
        "pyjwt>=2.8\nrequests>=2.31\nnever-imported>=1.0\n",
        encoding="utf-8",
    )
    (nested_dir / "token.py").write_text("import jwt\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_runner(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        return {"exitCode": 0, "stdout": "installed", "stderr": ""}

    evidence, dep_dir, _ = prepare_dependency_overlay(
        tmp_path,
        "svc/token.py",
        python_bin="python3",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=fake_runner,
    )

    assert evidence is not None
    assert evidence["name"] == "dependency_overlay_install"
    assert dep_dir is not None
    assert len(calls) == 1
    install = calls[0]
    assert install[:4] == ["python3", "-m", "pip", "install"]
    assert "--target" in install
    requirements_arg = Path(install[install.index("-r") + 1])
    assert manifest_requirement_lines(requirements_arg.read_text().splitlines()) == [
        "pyjwt>=2.8",
        "requests>=2.31",
        "never-imported>=1.0",
    ]


def test_prepare_dependency_overlay_cached(tmp_path: Path):
    nested_dir = tmp_path / "pkg"
    nested_dir.mkdir()
    req_file = nested_dir / "requirements.txt"
    req_file.write_text("my-dummy-lib>=1.0\n")
    source_file = nested_dir / "worker.py"
    source_file.write_text("import my_dummy_lib\ndef work(): pass")

    def fake_runner(cmd, cwd=None, timeout=None, env=None):
        class Result:
            returncode = 0
            exit_code = 0
            stdout = "Successfully installed"
            stderr = ""

        return Result()

    evidence1, dep_dir1, digest1 = prepare_dependency_overlay(
        tmp_path,
        "pkg/worker.py",
        python_bin="python3",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=fake_runner,
    )
    assert evidence1 is not None
    assert evidence1["name"] == "dependency_overlay_install"
    assert dep_dir1 is not None
    assert dep_dir1.exists()

    evidence2, dep_dir2, digest2 = prepare_dependency_overlay(
        tmp_path,
        "pkg/worker.py",
        python_bin="python3",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=fake_runner,
    )
    assert evidence2 is not None
    assert evidence2["name"] == "dependency_overlay_cached"
    assert dep_dir2 == dep_dir1
    assert digest2 == digest1


def test_prepare_dependency_overlay_cache_follows_a_nested_include_edit(tmp_path: Path):
    base = tmp_path / "base.txt"
    base.write_text("requests>=2.31\n", encoding="utf-8")
    nested_dir = tmp_path / "svc"
    nested_dir.mkdir()
    (nested_dir / "requirements.txt").write_text("-r ../base.txt\n", encoding="utf-8")
    (nested_dir / "app.py").write_text("import requests\n", encoding="utf-8")

    def fake_runner(cmd, cwd=None, timeout=None, env=None):
        return {"exitCode": 0, "stdout": "installed", "stderr": ""}

    def prepare():
        return prepare_dependency_overlay(
            tmp_path,
            "svc/app.py",
            python_bin="python3",
            timeout_seconds=30,
            artifact_dir=".uta_cache/python",
            runner=fake_runner,
        )

    _, _, first_digest = prepare()
    base.write_text("requests>=2.32\n", encoding="utf-8")
    evidence, _, second_digest = prepare()

    assert second_digest != first_digest
    assert evidence is not None
    assert evidence["name"] == "dependency_overlay_install"


def test_prepare_dependency_overlay_accepts_canonical_dict_runner_results(tmp_path: Path):
    nested_dir = tmp_path / "pipecat"
    nested_dir.mkdir()
    (nested_dir / "requirements.txt").write_text("loguru>=0.7\n", encoding="utf-8")
    (nested_dir / "app.py").write_text("from loguru import logger\n", encoding="utf-8")
    calls: list[list[str]] = []

    def canonical_runner(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        return {
            "name": "dependency_overlay",
            "command": list(cmd),
            "exitCode": 0,
            "stdout": "installed",
            "stderr": "",
        }

    evidence, dep_dir, _ = prepare_dependency_overlay(
        tmp_path,
        "pipecat/app.py",
        python_bin="python3",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=canonical_runner,
    )

    assert evidence is not None
    assert evidence["name"] == "dependency_overlay_install"
    assert dep_dir is not None
    assert any(command[:3] == ["python3", "-m", "pip"] for command in calls)


def test_prepare_dependency_overlay_skips_only_unavailable_distribution_and_installs_rest(
    tmp_path: Path,
):
    nested_dir = tmp_path / "pipecat"
    nested_dir.mkdir()
    (nested_dir / "requirements.txt").write_text(
        "pipecat-ai>=0.0.47\nfastapi>=0.110\n",
        encoding="utf-8",
    )
    (nested_dir / "app.py").write_text(
        "from pipecat.frames.frames import Frame\nfrom fastapi import FastAPI\n",
        encoding="utf-8",
    )
    installed: list[list[str]] = []

    def fake_runner(cmd, cwd=None, timeout=None, env=None):
        requirements = Path(cmd[cmd.index("-r") + 1]).read_text(encoding="utf-8")
        installed.append(manifest_requirement_lines(requirements.splitlines()))
        if any(line.startswith("pipecat-ai") for line in installed[-1]):
            return {
                "exitCode": 1,
                "stdout": "",
                "stderr": "ERROR: No matching distribution found for pipecat-ai>=0.0.47",
            }
        return {"exitCode": 0, "stdout": "installed fastapi", "stderr": ""}

    evidence, dep_dir, _ = prepare_dependency_overlay(
        tmp_path,
        "pipecat/app.py",
        python_bin="python3",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=fake_runner,
    )

    assert evidence is not None
    assert evidence["exitCode"] == 0
    assert "Skipped unavailable dependency: pipecat-ai>=0.0.47" in evidence["stderr"]
    assert installed == [["pipecat-ai>=0.0.47", "fastapi>=0.110"], ["fastapi>=0.110"]]
    assert dep_dir is not None
    assert (dep_dir / ".uta-installed").is_file()


def test_install_manifest_requirements_reports_a_failure_it_cannot_attribute(tmp_path: Path):
    """A build failure is not a missing distribution; it has to reach the gate."""
    dependency_dir = tmp_path / "deps"
    dependency_dir.mkdir()

    def fake_runner(cmd, cwd=None, timeout=None, env=None):
        return {"exitCode": 1, "stdout": "", "stderr": "ERROR: Failed building wheel for lxml"}

    evidence = install_manifest_requirements(
        tmp_path,
        dependency_dir,
        ["lxml>=5.0"],
        python_bin="python3",
        timeout_seconds=30,
        runner=fake_runner,
        requirements_path=tmp_path / "req.txt",
    )

    assert evidence["exitCode"] == 1
    assert "Failed building wheel for lxml" in evidence["stderr"]


def test_prepare_dependency_overlay_falls_back_to_unpinned_target_imports(
    tmp_path: Path,
):
    """A stale repository-wide lockfile must not block a modern nested target.

    The production regression used a Python 3.6-era root requirements.txt for
    a target whose own README specifies Python 3.10.  Building the whole
    manifest failed on absl-py before the generated test could run, although
    that test needed only numpy and pandas.
    """
    source = tmp_path / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text("import numpy\nimport pandas\nimport timesfm\n", encoding="utf-8")
    test_file = tmp_path / "tests" / "test_forecast.py"
    test_file.parent.mkdir()
    test_file.write_text("import numpy\nimport pandas\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "absl-py==0.9.0\nnumpy==1.18.1\npandas==0.25.1\nunrelated==1.0\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_runner(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        if "-r" in cmd:
            return {
                "exitCode": 1,
                "stdout": "",
                "stderr": "ERROR: Failed to build 'absl-py' when getting requirements to build wheel",
            }
        return {"exitCode": 0, "stdout": "installed compatible wheels", "stderr": ""}

    evidence, dependency_dir, _ = prepare_dependency_overlay(
        tmp_path,
        "jobs/forecast.py",
        test_paths=("tests/test_forecast.py",),
        python_bin="python3.11",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=fake_runner,
    )

    assert evidence is not None
    assert evidence["name"] == "dependency_overlay_compat_install"
    assert evidence["exitCode"] == 0
    assert "whole manifest failed" in evidence["stderr"]
    assert calls[1][-2:] == ["numpy", "pandas"]
    assert "absl-py" not in calls[1]
    assert "unrelated" not in calls[1]
    assert dependency_dir is not None
    assert (dependency_dir / ".uta-installed").is_file()


def test_prepare_dependency_overlay_does_not_relax_target_local_manifest(tmp_path: Path):
    service = tmp_path / "service"
    service.mkdir()
    (service / "worker.py").write_text("import numpy\n", encoding="utf-8")
    (service / "requirements.txt").write_text("numpy==1.18.1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def failed_install(cmd, cwd=None, timeout=None, env=None):
        calls.append(list(cmd))
        return {"exitCode": 1, "stdout": "", "stderr": "legacy pin cannot build"}

    evidence, dependency_dir, _ = prepare_dependency_overlay(
        tmp_path,
        "service/worker.py",
        python_bin="python3.11",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=failed_install,
    )

    assert evidence is not None
    assert evidence["name"] == "dependency_overlay_install"
    assert evidence["exitCode"] == 1
    assert len(calls) == 1
    assert dependency_dir is not None
    assert not (dependency_dir / ".uta-installed").exists()


def test_an_index_that_serves_nothing_fails_instead_of_caching_an_empty_overlay(tmp_path: Path):
    """The 2026-09-01 production failure: the node's configured index 404s for
    every distribution, each one is dropped as unavailable, pip is finally
    asked to install nothing and exits 0 -- and an empty overlay gets cached as
    a success, so every gate afterwards measures a dependency-less environment.
    """
    nested_dir = tmp_path / "pipecat"
    nested_dir.mkdir()
    (nested_dir / "requirements.txt").write_text(
        "loguru>=0.7\npytest-asyncio>=0.23\n",
        encoding="utf-8",
    )
    (nested_dir / "app.py").write_text("from loguru import logger\n", encoding="utf-8")

    def dead_index(cmd, cwd=None, timeout=None, env=None):
        requirements = Path(cmd[cmd.index("-r") + 1]).read_text(encoding="utf-8")
        wanted = manifest_requirement_lines(requirements.splitlines())
        if not wanted:
            return {"exitCode": 0, "stdout": "", "stderr": ""}
        return {
            "exitCode": 1,
            "stdout": "",
            "stderr": f"ERROR: No matching distribution found for {wanted[0]}",
        }

    evidence, dep_dir, _ = prepare_dependency_overlay(
        tmp_path,
        "pipecat/app.py",
        python_bin="python3",
        timeout_seconds=30,
        artifact_dir=".uta_cache/python",
        runner=dead_index,
    )

    assert evidence is not None
    assert evidence["exitCode"] != 0
    assert "refusing to cache an empty dependency overlay" in evidence["stderr"]
    assert dep_dir is not None
    assert not (dep_dir / ".uta-installed").is_file()
