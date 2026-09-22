"""ADR-002 batched generation in the canonical binding.

mutmut copies the whole enclosing function once per mutant, so one generated
module grows as (mutants x function size) and leaves CPython's linear parse
regime. `hard_cap` handles that by dropping selected mutants past a cap --
quietly enforcing less on exactly the diffs worth mutating. `batch` partitions
generation by function instead and scores each batch in turn, which is why it
is UTA's production default.

The binding had the partitioner vendored and unused: `partition_candidate_keys`
had zero callers, generation was a single hard-capped pass, and the missing
`mutation_batch_count` artifact was the visible edge of it. Promoting the
canonical lane in that state would have rolled ADR-002 back to its own
documented rollback path. These tests pin the behaviour that closed it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from uta_py_enforce import mutation as mutation_module
from uta_py_enforce.mutation import _generation_passes, _run_generation_passes
from uta_py_enforce.mutation_policy import build_generation_policy


SOURCE = '''
def alpha(a, b):
    x = a + b
    y = a - b
    return x * y


def beta(a, b):
    p = a * b
    q = a / b
    return p + q
'''


@pytest.fixture
def policy(tmp_path: Path):
    source_file = tmp_path / "mod.py"
    source_file.write_text(SOURCE, encoding="utf-8")
    changed = [3, 4, 5, 9, 10, 11]
    built = build_generation_policy(
        source_file,
        source_path="mod.py",
        changed_lines=changed,
        covered_lines=changed,
        hard_cap=1000,
    )
    path = tmp_path / "policy.json"
    path.write_text("{}", encoding="utf-8")
    return source_file, built, path


def test_hard_cap_is_one_pass_over_the_whole_policy(monkeypatch, policy):
    """The rollback path stays exactly what it was: one generation, one score."""
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_STRATEGY", "hard_cap")
    source_file, built, path = policy

    passes, partition = _generation_passes(source_file, built, path)

    assert len(passes) == 1
    assert partition is None
    assert passes[0].suffix == "", "a single pass must not be named like a batch"
    assert passes[0].policy is built


def test_batching_splits_by_function_and_loses_no_line(monkeypatch, policy):
    """Every selected line lands in exactly one batch.

    Whole-function granularity is what keeps a unit's mutmut keys inside one
    batch; a line appearing twice would collide on regeneration, and a line
    appearing nowhere is a mutation silently never scored.
    """
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_STRATEGY", "batch")
    # Fits one function per batch and both functions whole, so the split is a
    # split and not a trim.
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES", "600")
    source_file, built, path = policy

    passes, partition = _generation_passes(source_file, built, path)

    assert len(passes) == 2, [item.policy["selectedLines"] for item in passes]
    seen: list[int] = []
    for item in passes:
        assert item.path.exists(), "each batch is generated from its own policy file"
        seen.extend(item.policy["selectedLines"])
    assert sorted(seen) == sorted(built["selectedLines"]), "a selected line was dropped"
    assert len(seen) == len(set(seen)), "a line was placed in two batches"
    assert partition.omitted_by_generated_bytes_cap == ()
    assert [item.policy["selectedLines"] for item in passes] == [[3, 4, 5], [9, 10, 11]], (
        "batches must not straddle a function boundary"
    )


def test_work_dropped_to_fit_the_budget_is_reported(monkeypatch, policy):
    """A trimmed line is a mutation that will never be scored. Under a budget
    too small for whole functions the partitioner keeps the highest-priority
    lines and records the rest -- silence here would be a quietly weaker gate.
    """
    from uta_py_enforce.mutation import _partition_warnings

    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_STRATEGY", "batch")
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES", "400")
    source_file, built, path = policy

    passes, partition = _generation_passes(source_file, built, path)

    kept = [line for item in passes for line in item.policy["selectedLines"]]
    omitted = list(partition.omitted_by_generated_bytes_cap)
    assert omitted, "this budget cannot fit whole functions"
    assert sorted(kept + omitted) == sorted(built["selectedLines"]), (
        "every selected line is either batched or recorded as omitted"
    )

    warnings = _partition_warnings(partition)
    assert len(warnings) == 1
    assert str(len(omitted)) in warnings[0]
    for line in omitted:
        assert str(line) in warnings[0]


class _Recorder:
    """Stands in for the adapter: records what ran, returns per-batch results."""

    def __init__(self, meta_per_pass, metadata_exit_codes=None):
        self.names: list[str] = []
        self._meta = list(meta_per_pass)
        self._exits = list(metadata_exit_codes or [])
        self._pass = -1

    def run_command(self, name, command, cwd, timeout, commands, env=None):
        self.names.append(name)
        if name.startswith("mutmut_generate_metadata"):
            self._pass += 1
            exit_code = self._exits[self._pass] if self._pass < len(self._exits) else 0
            return {"name": name, "exitCode": exit_code}
        return {"name": name, "exitCode": 0}

    def read_meta(self, mutants_dir):
        return self._meta[max(0, self._pass)]


def _run(monkeypatch, passes, partition, recorder, tmp_path):
    monkeypatch.setattr(mutation_module, "run_command", recorder.run_command)
    monkeypatch.setattr(mutation_module, "read_mutmut_meta", recorder.read_meta)
    monkeypatch.setattr(mutation_module, "_candidate_plan", lambda *a, **k: {})
    return _run_generation_passes(
        passes,
        partition,
        repo=tmp_path,
        source_path="mod.py",
        test_path="tests/test_mod.py",
        changed_lines=[3, 4, 5, 9, 10, 11],
        mutants_dir=tmp_path / "mutants",
        timeout=60,
        commands=[],
        python_bin="python3",
        syntax_version="python3",
    )


def test_scores_accumulate_across_batches(monkeypatch, policy, tmp_path):
    """Each batch regenerates `mutants/` from scratch, so the result is the
    union of the batches -- not whatever the last one left behind."""
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_STRATEGY", "batch")
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES", "600")
    source_file, built, path = policy
    passes, partition = _generation_passes(source_file, built, path)

    per_pass = [({"killed": 2, "survived": 1}, [f"k{i}a", f"k{i}b"]) for i in range(len(passes))]
    recorder = _Recorder(per_pass)

    counts, keys, _plan, metadata, _run_cmd = _run(
        monkeypatch, passes, partition, recorder, tmp_path
    )

    assert counts["killed"] == 2 * len(passes)
    assert counts["survived"] == 1 * len(passes)
    assert len(keys) == 2 * len(passes) and keys == sorted(keys)
    assert int(metadata["exitCode"]) == 0
    assert [name for name in recorder.names if name.endswith("_batch_1")], recorder.names


def test_the_first_metadata_failure_is_what_the_caller_sees(monkeypatch, policy, tmp_path):
    """Fail closed. A batch that could not generate must not be averaged away
    by later batches that could -- that would certify unmutated code."""
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_STRATEGY", "batch")
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES", "600")
    source_file, built, path = policy
    passes, partition = _generation_passes(source_file, built, path)
    assert len(passes) >= 2

    exits = [0] * len(passes)
    exits[0] = 3
    recorder = _Recorder([({"killed": 1}, ["k"])] * len(passes), metadata_exit_codes=exits)

    _counts, _keys, _plan, metadata, _run_cmd = _run(
        monkeypatch, passes, partition, recorder, tmp_path
    )

    assert int(metadata["exitCode"]) == 3
    assert "mutmut_run_selected_batch_0" not in recorder.names, (
        "a batch whose metadata failed must not be scored"
    )


def test_run_mutation_forwards_the_dependency_overlay_to_generation(monkeypatch, tmp_path):
    """mutmut must run with the same import roots the coverage phase used.

    The dependency overlay is where enforcement installs the repository's own
    requirements. `run_mutation` built it, used it for the clean-test probe, and
    then called the generation passes without it -- so mutmut ran against a bare
    `os.environ` and `mutants/conftest.py` could not import those requirements.
    pytest exited 4, mutmut raised on that before its output catcher could dump
    the ImportError, and the evidence showed only "mutation backend failed" with
    an empty stdout.
    """
    from uta_py_enforce.mutation import run_mutation

    source = tmp_path / "mod.py"
    source.write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("def test_f():\n    assert True\n", encoding="utf-8")

    seen: dict = {}

    def fake_passes(passes, partition, **kwargs):
        seen.update(kwargs)
        counts = dict.fromkeys(
            ("killed", "segfault", "survived", "timeout", "suspicious",
             "not checked", "no tests", "skipped", "interrupted"), 0)
        return (counts, [], {}, {"exitCode": 0}, {"exitCode": 0})

    monkeypatch.setattr(mutation_module, "_run_generation_passes", fake_passes)
    monkeypatch.setattr(mutation_module, "write_mutmut_setup", lambda *a, **k: None)

    overlay = "/tmp/uta-overlay-marker"
    run_mutation(
        tmp_path,
        "mod.py",
        "tests/test_mod.py",
        {"mod.py": [2]},
        100.0,
        "mutmut",
        60,
        [],
        python_bin="python3",
        syntax_version="python3",
        execution_env={"PYTHONPATH": overlay},
    )

    assert "execution_env" in seen, "run_mutation did not forward execution_env at all"
    python_path = (seen["execution_env"] or {}).get("PYTHONPATH", "")
    assert overlay in python_path.split(os.pathsep)
