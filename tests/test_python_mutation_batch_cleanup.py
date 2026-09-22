"""T3: between-batch cleanup preserves stats/coverage, removes only generated mutants."""

import importlib
import sys
from types import SimpleNamespace

from uta.language.python.verification.runner import (
    _clean_generated_mutants,
    _cleanup_mutation_state,
)


def _seed_mutation_state(repo):
    (repo / "mutants").mkdir()
    (repo / "mutants" / "x.py").write_text("# generated", encoding="utf-8")
    (repo / "mutants" / "mutmut-stats.json").write_text("{}", encoding="utf-8")
    (repo / ".coverage").write_text("coverage-data", encoding="utf-8")
    (repo / ".mutmut-cache").write_text("cache-data", encoding="utf-8")


def test_clean_generated_mutants_keeps_stats_and_coverage(tmp_path):
    _seed_mutation_state(tmp_path)

    _clean_generated_mutants(tmp_path)

    assert not (tmp_path / "mutants" / "x.py").exists()  # generated module removed -> forces regen
    assert (tmp_path / "mutants" / "mutmut-stats.json").exists()
    assert (tmp_path / ".coverage").exists()         # stats input preserved for reuse
    assert (tmp_path / ".mutmut-cache").exists()


def test_full_cleanup_still_removes_everything(tmp_path):
    _seed_mutation_state(tmp_path)

    _cleanup_mutation_state(tmp_path)

    assert not (tmp_path / "mutants").exists()
    assert not (tmp_path / ".coverage").exists()
    assert not (tmp_path / ".mutmut-cache").exists()


def test_filter_generation_policy_to_lines_restricts_line_keyed_fields():
    from uta.language.python.verification.runner import _filter_generation_policy_to_lines

    policy = {
        "sourcePath": "pkg/mod.py",
        "capProfile": "full",
        "selectedLines": [1, 5, 9],
        "allowedLines": [1, 5, 9],
        "selected": [{"line": 1}, {"line": 5}, {"line": 9}],
        "suppressed": [{"opportunity": {"line": 5}}, {"opportunity": {"line": 9}}],
        "operatorByLine": {"1": ["a"], "5": ["b"], "9": ["c"]},
        "opportunityIdByLine": {"1": "o1", "5": "o5", "9": "o9"},
    }
    filtered = _filter_generation_policy_to_lines(policy, {1, 5})

    assert filtered["selectedLines"] == [1, 5]
    assert filtered["allowedLines"] == [1, 5]
    assert [it["line"] for it in filtered["selected"]] == [1, 5]
    assert [it["opportunity"]["line"] for it in filtered["suppressed"]] == [5]
    assert set(filtered["operatorByLine"]) == {"1", "5"}
    assert set(filtered["opportunityIdByLine"]) == {"1", "5"}
    # scalar fields preserved
    assert filtered["sourcePath"] == "pkg/mod.py"
    assert filtered["capProfile"] == "full"


def test_aggregate_mutation_summaries_sums_and_recomputes_rate():
    from dataclasses import replace
    from uta.language.python.verification.runner import (
        _aggregate_mutation_summaries,
        _empty_mutation_summary,
    )

    base = _empty_mutation_summary(gate=80.0, runtime_lane="mutmut")
    s1 = replace(base, generated=3, killed=2, survived=1)
    s2 = replace(base, generated=2, killed=2, survived=0)

    agg = _aggregate_mutation_summaries([s1, s2], gate=80.0, runtime_lane="mutmut")

    assert agg.killed == 4
    assert agg.survived == 1
    assert agg.generated == 5
    # detected/(detected+survived) = 4/5 = 80% -> passes the 80 gate
    assert agg.rate == 80.0
    assert agg.passed is True


def _agg_plan(*, keys, eligible):
    return {
        "language": "python",
        "sourcePath": "pkg/mod.py",
        "activeToolCandidateKeys": list(keys),
        "exactToolCandidateKeys": list(keys),
        "eligibleMutationOpportunities": [{"opportunityId": f"o{i}"} for i in range(eligible)],
    }


def test_finalize_batched_outcome_fails_closed_on_batch_failure():
    from uta.language.python.verification.runner import (
        _finalize_batched_outcome,
        _empty_mutation_summary,
    )

    summary = _empty_mutation_summary(gate=80.0, runtime_lane="mutmut")
    plan = _agg_plan(keys=["m.x_a__mutmut_1"], eligible=1)

    _, out = _finalize_batched_outcome(
        summary, plan,
        batch_failures=["batch 2 metadata generation failed (exit 1)"],
        total_generated_mutants=5,
        source_path="pkg/mod.py",
        changed_lines={"pkg/mod.py": [1]},
    )
    # planningError is set -> downstream fails the target closed (I1)
    assert "batch 2 metadata generation failed" in out["planningError"]


def test_nested_also_copy_file_gets_a_destination_parent(tmp_path, monkeypatch):
    from uta_py_enforce import mutmut_adapter_runtime

    monkeypatch.chdir(tmp_path)
    support_file = tmp_path / "test" / "conftest.py"
    support_file.parent.mkdir(parents=True)
    support_file.write_text("# support", encoding="utf-8")
    monkeypatch.setattr(
        mutmut_adapter_runtime.mutmut_main.mutmut,
        "config",
        SimpleNamespace(also_copy=["test/conftest.py"]),
    )

    mutmut_adapter_runtime._prepare_also_copy_parent_dirs()

    assert (tmp_path / "mutants" / "test").is_dir()


def test_also_copy_replaces_import_compat_resource_symlink_before_copy(tmp_path, monkeypatch):
    from uta_py_enforce import mutmut_adapter_runtime

    monkeypatch.chdir(tmp_path)
    resources = tmp_path / "resources.beta"
    resources.mkdir()
    (resources / "config.properties").write_text("answer=42\n", encoding="utf-8")
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    mirror = mutants / "resources.beta"
    mirror.symlink_to(resources, target_is_directory=True)
    monkeypatch.setattr(
        mutmut_adapter_runtime.mutmut_main.mutmut,
        "config",
        SimpleNamespace(also_copy=["resources.beta"]),
    )

    mutmut_adapter_runtime._prepare_also_copy_parent_dirs()

    assert not mirror.is_symlink()
    assert not mirror.exists()


def test_mutmut_pytest_phases_do_not_reuse_repository_module_state(tmp_path, monkeypatch):
    from uta_py_enforce import mutmut_adapter_runtime

    mutants_dir = tmp_path / "mutants"
    mutants_dir.mkdir()
    isolated_tests = tmp_path / "uta-pytest-isolated-fixture" / "tests"
    isolated_tests.mkdir(parents=True)
    monkeypatch.chdir(mutants_dir)
    module_path = isolated_tests / "stateful_test_module.py"
    module_path.write_text("calls = 0\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(isolated_tests))
    observed = []

    def execute_pytest(_self, _params, **_kwargs):
        module = importlib.import_module("stateful_test_module")
        module.calls += 1
        observed.append(module.calls)
        return 0

    monkeypatch.setattr(
        mutmut_adapter_runtime.mutmut_main.PytestRunner,
        "execute_pytest",
        execute_pytest,
    )

    mutmut_adapter_runtime._install_pytest_phase_isolation()
    runner = object.__new__(mutmut_adapter_runtime.mutmut_main.PytestRunner)
    runner.execute_pytest([str(module_path)])
    runner.execute_pytest([str(module_path)])

    assert observed == [1, 1]
    assert "stateful_test_module" not in sys.modules


def test_shared_mutmut_adapter_normalizes_src_trampoline_hits(monkeypatch):
    from uta_py_enforce import mutmut_adapter_runtime

    hits = []

    def record(name):
        if name.startswith("src."):
            raise AssertionError("src prefix not normalized")
        hits.append(name)

    monkeypatch.setattr(mutmut_adapter_runtime.mutmut_main, "record_trampoline_hit", record)

    mutmut_adapter_runtime._install_src_trampoline_normalizer()
    mutmut_adapter_runtime.mutmut_main.record_trampoline_hit("src.pkg.worker.run")

    assert hits == ["pkg.worker.run"]


def test_batch_execution_with_materialized_metadata_but_no_results_fails_closed():
    from uta.language.python.verification.runner import (
        _batch_execution_failure,
        _empty_mutation_summary,
    )

    failure = _batch_execution_failure(
        batch_index=0,
        metadata_exit_code=0,
        metadata_count=3,
        execution_exit_code=1,
        execution_summary=_empty_mutation_summary(gate=95.0, runtime_lane="mutmut-modern"),
    )

    assert "produced no results for 3 generated mutants" in failure


def test_finalize_batched_outcome_fails_when_planned_opportunities_materialize_nothing():
    from uta.language.python.verification.runner import (
        _finalize_batched_outcome,
        _empty_mutation_summary,
    )

    summary = _empty_mutation_summary(gate=80.0, runtime_lane="mutmut")
    plan = _agg_plan(keys=[], eligible=3)  # eligible opps, but no keys

    new_summary, out = _finalize_batched_outcome(
        summary, plan,
        batch_failures=[],
        total_generated_mutants=0,  # nothing mutatable generated anywhere
        source_path="pkg/mod.py",
        changed_lines={"pkg/mod.py": [1, 2, 3]},
    )
    assert "materialized zero mutants" in out["planningError"]
    assert new_summary.generated == 0


def test_finalize_batched_outcome_fails_when_exact_candidate_runs_zero_mutants():
    from uta.language.python.verification.runner import (
        _finalize_batched_outcome,
        _empty_mutation_summary,
    )

    summary = _empty_mutation_summary(gate=95.0, runtime_lane="mutmut")
    plan = _agg_plan(keys=["pkg.mod.x_target__mutmut_1"], eligible=1)

    new_summary, out = _finalize_batched_outcome(
        summary,
        plan,
        batch_failures=[],
        total_generated_mutants=0,
        source_path="pkg/mod.py",
        changed_lines={"pkg/mod.py": [10]},
    )

    assert "materialized zero mutants" in out["planningError"]
    assert new_summary.generated == 0


def test_finalize_batched_outcome_fails_when_mutants_unmapped():
    from uta.language.python.verification.runner import (
        _finalize_batched_outcome,
        _empty_mutation_summary,
    )

    summary = _empty_mutation_summary(gate=80.0, runtime_lane="mutmut")
    plan = _agg_plan(keys=[], eligible=3)  # eligible opps, no keys...

    _, out = _finalize_batched_outcome(
        summary, plan,
        batch_failures=[],
        total_generated_mutants=42,  # ...but mutants WERE generated -> mapping failure (I2)
        source_path="pkg/mod.py",
        changed_lines={"pkg/mod.py": [1, 2, 3]},
    )
    assert "could not map" in out["planningError"]


def test_finalize_batched_outcome_passthrough_when_keys_present():
    from uta.language.python.verification.runner import (
        _finalize_batched_outcome,
        _empty_mutation_summary,
    )

    summary = _empty_mutation_summary(gate=80.0, runtime_lane="mutmut")
    plan = _agg_plan(keys=["m.x_a__mutmut_1"], eligible=1)

    new_summary, out = _finalize_batched_outcome(
        summary, plan,
        batch_failures=[],
        total_generated_mutants=1,
        source_path="pkg/mod.py",
        changed_lines={"pkg/mod.py": [1]},
    )
    # normal case: keys present -> no planningError, summary untouched
    assert "planningError" not in out
    assert new_summary is summary


def test_phase_isolation_drops_dependency_stubs_a_phase_injected():
    """A generated test's `sys.modules` stub must not outlive its phase.

    When the dependency overlay is unusable a generated test installs stubs --
    `sys.modules[name] = types.ModuleType(name)` -- so it can import its target.
    `_purge_modules_loaded_from` decides what to drop by `__file__`/`__path__`,
    and a bare module has neither, so the stub survived into mutmut's next
    in-process pytest phase. That phase re-imported the real dependency chain
    against the stub and died during collection with `AttributeError: module
    'requests' has no attribute 'Session'`, surfacing only as "mutation backend
    failed" with no stdout.
    """
    import sys
    import types
    from uta_py_enforce.mutmut_adapter_runtime import _purge_synthetic_modules

    before = frozenset(sys.modules)
    sys.modules["uta_probe_bare_stub"] = types.ModuleType("uta_probe_bare_stub")
    filed = types.ModuleType("uta_probe_filed_module")
    filed.__file__ = __file__
    sys.modules["uta_probe_filed_module"] = filed
    try:
        _purge_synthetic_modules(before)
        assert "uta_probe_bare_stub" not in sys.modules, "stub leaked into the next phase"
        # A module backed by a real file is a genuine import, not a stub.
        assert "uta_probe_filed_module" in sys.modules
    finally:
        sys.modules.pop("uta_probe_bare_stub", None)
        sys.modules.pop("uta_probe_filed_module", None)


def test_phase_isolation_keeps_modules_present_before_the_phase():
    """Only what the phase added is dropped; the cache is not emptied."""
    import sys
    import types
    from uta_py_enforce.mutmut_adapter_runtime import _purge_synthetic_modules

    sys.modules["uta_probe_preexisting_stub"] = types.ModuleType("uta_probe_preexisting_stub")
    before = frozenset(sys.modules)
    try:
        _purge_synthetic_modules(before)
        assert "uta_probe_preexisting_stub" in sys.modules
    finally:
        sys.modules.pop("uta_probe_preexisting_stub", None)


def test_phase_isolation_keeps_builtin_modules_loaded_during_the_phase():
    """No-file does not mean stub: CPython built-ins must stay initialized.

    Removing ``_zoneinfo`` while ``zoneinfo.ZoneInfo`` still references its C
    type leaves the next pytest phase with ``SystemError: null argument to
    internal routine``. Built-in module specs distinguish runtime modules from
    the bare ``ModuleType`` dependency stubs UTA is responsible for removing.
    """
    import importlib.machinery
    import sys
    import types
    from uta_py_enforce.mutmut_adapter_runtime import _purge_synthetic_modules

    before = frozenset(sys.modules)
    builtin = types.ModuleType("uta_probe_builtin")
    builtin.__loader__ = importlib.machinery.BuiltinImporter
    builtin.__spec__ = importlib.machinery.ModuleSpec(
        builtin.__name__,
        importlib.machinery.BuiltinImporter,
        origin="built-in",
    )
    sys.modules[builtin.__name__] = builtin
    try:
        _purge_synthetic_modules(before)
        assert builtin.__name__ in sys.modules, "legitimate built-in module was purged"
    finally:
        sys.modules.pop(builtin.__name__, None)


def test_phase_isolation_drops_the_whole_subtree_a_stub_shadowed():
    """Purging a stub must take the real submodules it shadowed with it.

    The same phase that stubs `requests` also imports the real `requests.*`
    submodules, and those are file-backed. Dropping only the bare parent left
    them cached, so the next phase re-executed `requests/__init__` against
    cached children: the rebuilt package carried whatever `__init__` imports by
    name and nothing else. `requests.Session` resolved and `requests.adapters`
    did not -- the identical AttributeError, one line further down.
    """
    import sys
    import types
    from uta_py_enforce.mutmut_adapter_runtime import _purge_synthetic_modules

    before = frozenset(sys.modules)
    sys.modules["uta_probe_pkg"] = types.ModuleType("uta_probe_pkg")
    child = types.ModuleType("uta_probe_pkg.adapters")
    child.__file__ = "/real/adapters.py"
    sys.modules["uta_probe_pkg.adapters"] = child
    unrelated = types.ModuleType("uta_probe_unrelated")
    unrelated.__file__ = "/real/unrelated.py"
    sys.modules["uta_probe_unrelated"] = unrelated
    try:
        _purge_synthetic_modules(before)
        assert "uta_probe_pkg" not in sys.modules
        assert "uta_probe_pkg.adapters" not in sys.modules, "subtree left cached"
        assert "uta_probe_unrelated" in sys.modules, "unrelated real import was dropped"
    finally:
        for name in ("uta_probe_pkg", "uta_probe_pkg.adapters", "uta_probe_unrelated"):
            sys.modules.pop(name, None)
