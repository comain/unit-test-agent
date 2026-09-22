"""Import roots for the repository's own pytest runs.

The binding shipped `pytest_import_roots_plugin`, which reads
`UTA_PYTEST_IMPORT_ROOTS`, but nothing ever set it -- a consumer ported without
its producer. Seven targets in one production report never reached coverage
because their tests could not import their own conftest.
"""

from __future__ import annotations

import os
from pathlib import Path

from uta_py_enforce.pytest_env import mutation_pytest_env, pytest_env, pytest_import_roots


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "test").mkdir(parents=True)
    (repo / "test" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "test" / "conftest.py").write_text("", encoding="utf-8")
    (repo / "test" / "test_thing.py").write_text("import conftest\n", encoding="utf-8")
    return repo


def test_the_test_directory_becomes_an_import_root(tmp_path):
    repo = _repo(tmp_path)

    roots = pytest_import_roots(repo, ["test/test_thing.py"])

    assert roots == [(repo / "test").resolve().as_posix()]


def test_pythonpath_carries_the_root_and_the_repo(tmp_path):
    repo = _repo(tmp_path)

    env = pytest_env(repo, ["test/test_thing.py"], base_env={})
    entries = env["PYTHONPATH"].split(os.pathsep)

    assert (repo / "test").resolve().as_posix() in entries
    assert repo.resolve().as_posix() in entries
    # The test's own directory must precede the repo, or `import conftest`
    # still resolves against the repo root and fails.
    assert entries.index((repo / "test").resolve().as_posix()) < entries.index(repo.resolve().as_posix())
    assert env["UTA_REPO_ROOT"] == repo.resolve().as_posix()


def test_the_plugin_is_registered_and_importable(tmp_path):
    """Registering a plugin the child cannot import would break pytest
    outright, so the distribution root ships on PYTHONPATH alongside it."""
    repo = _repo(tmp_path)

    env = pytest_env(repo, ["test/test_thing.py"], base_env={})

    assert env["PYTEST_PLUGINS"].split(",") == [
        "uta_py_enforce.pytest_import_roots_plugin",
        "uta_py_enforce.optional_plugins",
    ]
    import uta_py_enforce

    distribution_root = Path(uta_py_enforce.__file__).resolve().parent.parent.as_posix()
    assert distribution_root in env["PYTHONPATH"].split(os.pathsep)
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_async_marker_loads_only_the_declared_async_plugin(tmp_path):
    repo = _repo(tmp_path)
    (repo / "test" / "test_thing.py").write_text(
        "import pytest\n@pytest.mark.asyncio\nasync def test_thing(): pass\n",
        encoding="utf-8",
    )

    env = pytest_env(repo, ["test/test_thing.py"], base_env={})

    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["PYTEST_PLUGINS"].split(",") == [
        "uta_py_enforce.pytest_import_roots_plugin",
        "uta_py_enforce.optional_plugins",
    ]


def test_an_existing_environment_is_preserved(tmp_path):
    """`run_command` hands `env` straight to subprocess, which replaces rather
    than extends -- so dropping a key here would strip it from the child."""
    repo = _repo(tmp_path)

    env = pytest_env(repo, ["test/test_thing.py"], base_env={"PATH": "/usr/bin", "PYTHONPATH": "/pre/existing"})

    assert env["PATH"] == "/usr/bin"
    assert "/pre/existing" in env["PYTHONPATH"].split(os.pathsep)


def test_a_missing_test_file_contributes_no_root(tmp_path):
    repo = _repo(tmp_path)

    assert pytest_import_roots(repo, ["test/does_not_exist.py"]) == []
    env = pytest_env(repo, ["test/does_not_exist.py"], base_env={})
    assert "UTA_PYTEST_IMPORT_ROOTS" not in env
    # The import-roots plugin needs roots; the optional shim always loads.
    assert env["PYTEST_PLUGINS"] == "uta_py_enforce.optional_plugins"


def test_the_optional_plugin_shim_degrades_when_the_plugin_is_absent(monkeypatch):
    """A plugin named in PYTEST_PLUGINS but missing aborts pytest before it
    collects anything, which turns a partial failure into a zero coverage
    number. The shim resolves the name instead of asserting it."""
    import importlib

    import uta_py_enforce.optional_plugins as optional_plugins

    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: None if "asyncio" in name else object()
    )
    reloaded = importlib.reload(optional_plugins)
    assert reloaded.pytest_plugins == ()

    monkeypatch.undo()
    restored = importlib.reload(optional_plugins)
    assert restored.pytest_plugins == ("pytest_asyncio.plugin",)


def test_a_deadlocked_test_is_bounded_by_the_projects_own_pytest_timeout(tmp_path):
    """2026-09-01: `pipecat/api/app.py`'s generated test deadlocked in
    `futex_wait_queue` for 1h52m and was reaped with no traceback, no coverage
    and no indication of where it blocked. The repo declares pytest-timeout in
    its requirements -- exactly the safety net for this -- but
    PYTEST_DISABLE_PLUGIN_AUTOLOAD stops it loading and UTA never asked for it.
    """
    repo = _repo(tmp_path)
    (repo / "test" / "test_thing.py").write_text("def test_thing(): pass\n", encoding="utf-8")

    env = pytest_env(repo, ["test/test_thing.py"], base_env={})

    assert "uta_py_enforce.optional_plugins" in env["PYTEST_PLUGINS"].split(",")
    assert "pytest_timeout" in env["UTA_PYTEST_OPTIONAL_PLUGINS"].split(",")
    assert env["PYTEST_TIMEOUT"] == "120"


def test_the_async_plugin_is_only_requested_when_a_marker_implies_it(tmp_path):
    repo = _repo(tmp_path)
    (repo / "test" / "test_plain.py").write_text("def test_plain(): pass\n", encoding="utf-8")
    plain = pytest_env(repo, ["test/test_plain.py"], base_env={})
    assert plain["UTA_PYTEST_OPTIONAL_PLUGINS"].split(",") == ["pytest_timeout"]

    (repo / "test" / "test_async.py").write_text(
        "import pytest\n@pytest.mark.asyncio\nasync def test_async(): pass\n",
        encoding="utf-8",
    )
    asyncio_env = pytest_env(repo, ["test/test_async.py"], base_env={})
    assert asyncio_env["UTA_PYTEST_OPTIONAL_PLUGINS"].split(",") == [
        "pytest_asyncio.plugin",
        "pytest_timeout",
    ]


def test_mutation_env_preserves_async_plugin_before_mutants_are_generated(tmp_path):
    """The mutant tree is empty while UTA prepares the mutmut process env."""
    repo = _repo(tmp_path)
    (repo / "test" / "test_async.py").write_text(
        "import pytest\n@pytest.mark.asyncio\nasync def test_async(): pass\n",
        encoding="utf-8",
    )
    coverage_env = pytest_env(repo, ["test/test_async.py"], base_env={})
    mutants = repo / "mutants"
    mutants.mkdir()

    env = mutation_pytest_env(mutants, ["test/test_async.py"], coverage_env)

    assert env["UTA_PYTEST_OPTIONAL_PLUGINS"].split(",") == [
        "pytest_asyncio.plugin",
        "pytest_timeout",
    ]


def test_the_per_test_timeout_is_configurable_and_can_be_disabled(tmp_path):
    repo = _repo(tmp_path)
    (repo / "test" / "test_thing.py").write_text("def test_thing(): pass\n", encoding="utf-8")

    tuned = pytest_env(
        repo, ["test/test_thing.py"], base_env={"UTA_PYTHON_TEST_TIMEOUT_SECONDS": "45"}
    )
    assert tuned["PYTEST_TIMEOUT"] == "45"

    off = pytest_env(
        repo, ["test/test_thing.py"], base_env={"UTA_PYTHON_TEST_TIMEOUT_SECONDS": "0"}
    )
    assert "PYTEST_TIMEOUT" not in off


def test_the_shim_resolves_only_the_plugins_it_was_asked_for(monkeypatch):
    """A project without pytest-timeout must not have its session aborted by
    UTA asking for it -- the shim resolves names, it does not assert them."""
    import importlib

    import uta_py_enforce.optional_plugins as optional_plugins

    monkeypatch.setenv("UTA_PYTEST_OPTIONAL_PLUGINS", "pytest_timeout,not_a_real_plugin")
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "not_a_real_plugin" else object(),
    )
    reloaded = importlib.reload(optional_plugins)
    assert reloaded.pytest_plugins == ("pytest_timeout",)

    monkeypatch.undo()
    importlib.reload(optional_plugins)
