"""Pre-refactor behaviour, import and call-count baselines.

Task 3 of the UTA architecture boundary cleanup. These tests freeze what the
system does *now*, at boundaries that survive the refactor, so a later slice
can prove it moved code without changing behaviour.

Every baseline here is taken through a public surface:

* package ``__init__`` exports, read with ``importlib``;
* Click and argparse command/flag/help metadata;
* the evidence JSON and marker text a CLI actually prints;
* SQL observed through a ``TaskDB.connect`` trace hook while calling public
  ``TaskDB``/``TaskManager`` methods;
* process spawns counted at ``subprocess.run``;
* the generation phase labels in the workflow spec and the phases each
  language backend admits to supporting.

None of them imports a private helper. A baseline that named, say,
``uta.language.python.verification.runner._scope_mutation_to_changed_lines``
would evaporate the moment that function moved — and moving it is the point
of the programme these baselines are meant to protect.

**Port-defect rule.** If one of these fails after a refactor, the refactor
changed behaviour. Fix the refactor. Never edit production code to make a
baseline pass, and re-freeze (``UTA_BASELINE_REFRESH=1``) only for a change
that was explicitly decided and reviewed.
"""

from __future__ import annotations

import inspect

import importlib
import json
import os
import re
import subprocess
import sqlite3
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from architecture_baseline_support import (
    FIXTURE_DIR,
    assert_free_of_machine_paths,
    assert_no_public_names_removed,
    compare_to_baseline,
    normalize,
    mask_mutation_counters,
    mask_mutation_outcomes,
    normalize_text,
    path_replacements,
    summarize_statements,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENFORCEMENT_PACKAGE_ROOT = REPO_ROOT / "tools" / "python-enforcement"
if str(ENFORCEMENT_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENFORCEMENT_PACKAGE_ROOT))


# --------------------------------------------------------------------------
# 1. Public import surface
# --------------------------------------------------------------------------

#: Every package ``__init__`` under ``uta`` plus the two distributed
#: enforcement packages. Discovered rather than hand-listed so a new package
#: joins the baseline automatically.
def _package_names() -> list[str]:
    names: list[str] = []
    for init in sorted((REPO_ROOT / "uta").rglob("__init__.py")):
        rel = init.relative_to(REPO_ROOT).parent
        names.append(".".join(rel.parts))
    for package in ("uta_enforce_core", "uta_py_enforce"):
        names.append(package)
    return names


def _public_names(module) -> dict[str, object]:
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return {"declares_all": True, "names": sorted(str(name) for name in declared)}
    return {
        "declares_all": False,
        "names": sorted(name for name in vars(module) if not name.startswith("_")),
    }


def test_public_package_import_surface_loses_nothing():
    """Freeze what each package ``__init__`` exports.

    Additions are fine. Removals are the failure this catches: a slice that
    stops re-exporting a name breaks importers outside this repository, and
    nothing else in the suite would notice.
    """
    observed = {
        name: _public_names(importlib.import_module(name))
        for name in _package_names()
    }

    assert_no_public_names_removed("public_import_surface", observed)


def test_every_uta_package_still_imports_standalone():
    """Each package must import on its own, in a cold interpreter.

    Module-to-package migrations that keep the old name are the risky kind;
    a stale ``.pyc`` or a shadowed name shows up here rather than at runtime.
    """
    script = "import importlib,sys\n" + "\n".join(
        f"importlib.import_module({name!r})" for name in _package_names()
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(ENFORCEMENT_PACKAGE_ROOT)},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr[-3000:]


# --------------------------------------------------------------------------
# 2. CLI help / flag contract
# --------------------------------------------------------------------------


def _click_param(param) -> dict[str, object]:
    return {
        "name": param.name,
        "opts": sorted(param.opts),
        "secondary_opts": sorted(param.secondary_opts),
        "kind": param.param_type_name,
        "type": type(param.type).__name__,
        "required": bool(param.required),
        "multiple": bool(getattr(param, "multiple", False)),
        "is_flag": bool(getattr(param, "is_flag", False)),
        "hidden": bool(getattr(param, "hidden", False)),
        "show_default": bool(getattr(param, "show_default", False)),
        "help": _help_text(getattr(param, "help", "")),
    }


def _help_text(value: object) -> str:
    """The words of a help string, not the indentation of its docstring.

    Click dedents help text itself from 8.2 on, so the same unchanged docstring
    is stored indented by older Click and dedented by newer. That is a property
    of the installed library, never of this project's published contract, and
    freezing it makes the baseline fail on a dependency bump while the contract
    it exists to protect has not moved.
    """
    return normalize_text(inspect.cleandoc(str(value or "")))


def _click_tree(command, path: tuple[str, ...] = ()) -> dict[str, object]:
    entry: dict[str, object] = {
        "help": _help_text(command.help),
        "short_help": _help_text(getattr(command, "short_help", "")),
        "hidden": bool(getattr(command, "hidden", False)),
        "params": sorted(
            (_click_param(param) for param in command.params),
            key=lambda item: str(item["name"]),
        ),
    }
    children = getattr(command, "commands", None) or {}
    entry["commands"] = {
        name: _click_tree(child, path + (name,)) for name, child in sorted(children.items())
    }
    return entry


def test_uta_cli_command_and_flag_contract_is_frozen():
    """Command names, flags and help text are a published contract.

    Defaults are deliberately excluded: several come from ``Settings``, which
    reads ``UTA_*`` environment variables, so freezing them would fail on a
    machine that has legitimately configured one.
    """
    from uta.app.cli import main

    compare_to_baseline("cli_contract_uta", _click_tree(main))


def test_standalone_enforcement_cli_help_contract_is_frozen():
    """``tools/python-enforcement/uta_python_test_enforce.py --help``, frozen.

    Captured by actually running the entrypoint, because the rendered help is
    the contract a developer and the dev-skills gate both read. ``COLUMNS`` is
    pinned so the wrapping is the terminal-independent one.
    """
    completed = subprocess.run(
        [sys.executable, str(ENFORCEMENT_PACKAGE_ROOT / "uta_python_test_enforce.py"), "--help"],
        cwd=str(REPO_ROOT),
        env={
            **os.environ,
            "PYTHONPATH": str(ENFORCEMENT_PACKAGE_ROOT),
            "COLUMNS": "100",
        },
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    observed = {
        "help_lines": normalize_text(
            completed.stdout, path_replacements()
        ).splitlines(),
    }

    compare_to_baseline("cli_contract_standalone_enforcement", observed)


def test_uta_cli_help_text_renders_without_machine_paths():
    """``uta --help`` must not leak the developer's filesystem."""
    from uta.app.cli import main

    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert_free_of_machine_paths(normalize_text(result.output, path_replacements()))


# --------------------------------------------------------------------------
# 3./4./6. Python enforcement: evidence, markers, subprocess budget
# --------------------------------------------------------------------------

MUTMUT_BIN = Path(sys.executable).with_name("mutmut")
requires_mutmut = pytest.mark.skipif(
    not MUTMUT_BIN.exists(),
    reason="mutation enforcement baseline needs the venv's mutmut",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def enforcement_fixture_repo(tmp_path: Path) -> Path:
    """A minimal Python repo with one changed, covered, mutable line.

    Small on purpose: the point is a stable, fast, end-to-end enforcement
    run, not coverage of every enforcement branch.
    """
    repo = tmp_path / "enforcement_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Baseline")
    _git(repo, "config", "user.email", "baseline@example.test")
    (repo / "jobs").mkdir()
    (repo / "tests").mkdir()
    (repo / "jobs" / "forecast.py").write_text(
        "def run(value):\n    if value > 0:\n        return value * 2\n    return 0\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n\ndef test_run():\n"
        "    assert run(2) == 4\n    assert run(-1) == 0\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
    (repo / "jobs" / "forecast.py").write_text(
        "def run(value):\n    if value > 0:\n        return value * 3\n    return 0\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_forecast.py").write_text(
        "from jobs.forecast import run\n\n\ndef test_run():\n"
        "    assert run(2) == 6\n    assert run(-1) == 0\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _pinned_env(repo: Path) -> dict[str, str]:
    """Pin the interpreter and mutation tool so evidence is machine-stable.

    Both entrypoints read these documented variables. Without them the UTA
    path resolves the bare name ``python3`` from ``PATH``, which is whatever
    the machine happens to ship and makes evidence unreproducible.
    """
    return {
        "UTA_PYTHON_BIN": sys.executable,
        "UTA_PYTHON_MUTMUT_BIN": str(MUTMUT_BIN),
        "UTA_PYTHON_ARTIFACT_DIR": ".uta_cache/python",
    }


def _run_uta_enforcement(repo: Path) -> tuple[dict, str]:
    """Invoke ``uta python-enforce`` twice: once for JSON, once for markers."""
    from uta.app.cli import main

    args = [
        "python-enforce",
        "--repo",
        str(repo),
        "--base-ref",
        "origin/master",
        "--test-path",
        "tests/test_forecast.py",
    ]
    runner = CliRunner()
    json_result = runner.invoke(main, args + ["--json-output"], env=_pinned_env(repo))
    assert json_result.exit_code == 0, json_result.output[-4000:]
    evidence = json.loads(json_result.output)
    marker_result = runner.invoke(main, args, env=_pinned_env(repo))
    assert marker_result.exit_code == 0, marker_result.output[-4000:]
    return evidence, marker_result.output


def _run_standalone_enforcement(repo: Path) -> tuple[dict, str, list[list[str]]]:
    """Invoke the distributed entrypoint in-process and count its spawns."""
    import uta_py_enforce.cli as lightweight_cli

    spawns: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(command, *args, **kwargs):
        spawns.append([str(part) for part in command])
        return real_run(command, *args, **kwargs)

    evidence_path = repo / ".uta_cache" / "baseline-evidence.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    previous = {key: os.environ.get(key) for key in _pinned_env(repo)}
    os.environ.update(_pinned_env(repo))
    subprocess.run = counting_run  # type: ignore[assignment]
    try:
        from contextlib import redirect_stdout
        import io

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = lightweight_cli.main(
                [
                    "--repo",
                    str(repo),
                    "--base-ref",
                    "origin/master",
                    "--test-path",
                    "tests/test_forecast.py",
                    "--evidence-output",
                    str(evidence_path),
                ]
            )
        marker_output = buffer.getvalue()
    finally:
        subprocess.run = real_run  # type: ignore[assignment]
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert code == 0, marker_output[-4000:]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    return evidence, marker_output, spawns


def _evidence_baseline(evidence: dict, repo: Path) -> dict:
    """Reduce evidence to the parts that are contract.

    Captured stdout/stderr, absolute artifact paths, commit ids and
    timestamps are scrubbed by the shared normaliser; what remains is the
    verdict, the gate arithmetic, the ordered command names and the
    candidate-plan shape.
    """
    replacements = path_replacements(fixture_repo=repo, interpreter=sys.executable)
    scrubbed = normalize(evidence, replacements)
    commands = scrubbed.get("commands") or []
    return {
        "top_level_keys": sorted(scrubbed),
        "status": scrubbed.get("status"),
        "passed": scrubbed.get("passed"),
        "reasonCode": scrubbed.get("reasonCode"),
        "summary": scrubbed.get("summary"),
        "language": scrubbed.get("language"),
        "backend": scrubbed.get("backend"),
        "schemaVersion": scrubbed.get("schemaVersion"),
        "changedProductionFiles": scrubbed.get("changedProductionFiles"),
        "changedLines": scrubbed.get("changedLines"),
        "targets": scrubbed.get("targets"),
        "coverage": scrubbed.get("coverage"),
        # Four of this block's 37 leaf keys record whether a live mutant was
        # killed, and they flip run to run; the rest is frozen. See
        # `mask_mutation_outcomes`.
        "mutation": mask_mutation_outcomes(scrubbed.get("mutation")),
        "artifact_keys": sorted(scrubbed.get("artifacts") or {}),
        "command_names": [str(item.get("name")) for item in commands],
        "command_exit_codes": [item.get("exitCode", item.get("exit_code")) for item in commands],
        "command_count": len(commands),
        "setup": scrubbed.get("setup"),
    }


@requires_mutmut
def test_uta_python_enforcement_evidence_baseline(enforcement_fixture_repo):
    """Freeze the evidence the *managed* (full UTA) Python gate produces.

    This froze the legacy lane's output. That lane is gone and the managed
    gate now runs the same binding the standalone one does, so the baseline is
    re-frozen against what UTA actually produces. The two baselines are still
    distinct: this one carries UTA's envelope -- repository facts, aggregates,
    per-target payloads -- around the binding's execution evidence.
    """
    evidence, _markers = _run_uta_enforcement(enforcement_fixture_repo)

    compare_to_baseline(
        "python_enforcement_evidence_uta",
        _evidence_baseline(evidence, enforcement_fixture_repo),
    )


@requires_mutmut
def test_standalone_python_enforcement_evidence_baseline(enforcement_fixture_repo):
    """Freeze the evidence the *distributed* lightweight gate produces."""
    evidence, _markers, _spawns = _run_standalone_enforcement(enforcement_fixture_repo)

    compare_to_baseline(
        "python_enforcement_evidence_standalone",
        _evidence_baseline(evidence, enforcement_fixture_repo),
    )


def _marker_payload(output: str) -> dict:
    """Split rendered marker output into its lines and its evidence payload.

    The design records that the two renderers *already differ* — the UTA one
    emits a candidate-plan line and a test-quality line the distributed one
    lacks, and only the distributed one prefixes the payload with
    ``UTA_PYTHON_ENFORCEMENT_EVIDENCE=``. Both are frozen here as they are,
    separately. Parity is asserted on the payload, never on the block.
    """
    prefix = "UTA_PYTHON_ENFORCEMENT_EVIDENCE="
    lines = [line for line in output.splitlines() if line.strip()]
    payload_lines = [line for line in lines if line.startswith(prefix)]
    marker_lines = [line for line in lines if not line.startswith(prefix)]
    payload = json.loads(payload_lines[0][len(prefix) :]) if payload_lines else None
    return {
        "carries_evidence_prefix": bool(payload_lines),
        "marker_lines": marker_lines,
        "payload": payload,
    }


@requires_mutmut
def test_python_enforcement_marker_payloads_are_frozen_separately(enforcement_fixture_repo):
    """Freeze both marker renderers as they are today, side by side.

    They are not equivalent and this baseline does not pretend otherwise:
    unifying them is a user-visible change needing its own approval. What is
    frozen for *both* is the ``UTA_PYTHON_ENFORCEMENT_EVIDENCE=`` payload
    verdict, which is where the two paths must agree.
    """
    repo = enforcement_fixture_repo
    replacements = path_replacements(fixture_repo=repo, interpreter=sys.executable)

    _uta_evidence, uta_output = _run_uta_enforcement(repo)
    _standalone_evidence, standalone_output, _spawns = _run_standalone_enforcement(repo)

    # `mask_mutation_counters` hides the digits a live mutmut run produces and
    # keeps the field names; see its docstring for the measurement that forced
    # it. The marker wording and field order stay frozen.
    uta = _marker_payload(mask_mutation_counters(normalize_text(uta_output, replacements)))
    standalone = _marker_payload(
        mask_mutation_counters(normalize_text(standalone_output, replacements))
    )

    observed = {
        "note": (
            "The UTA and distributed marker renderers already differ. Both are "
            "frozen as they are; parity is asserted on the evidence payload "
            "verdict only, never on the surrounding marker block."
        ),
        "uta": {
            "carries_evidence_prefix": uta["carries_evidence_prefix"],
            "marker_lines": uta["marker_lines"],
            "payload_verdict": _payload_verdict(uta["payload"]),
        },
        "standalone": {
            "carries_evidence_prefix": standalone["carries_evidence_prefix"],
            "marker_lines": standalone["marker_lines"],
            "payload_verdict": _payload_verdict(standalone["payload"]),
        },
    }

    compare_to_baseline("python_enforcement_markers", observed)

    if uta["payload"] is not None and standalone["payload"] is not None:
        assert _payload_verdict(uta["payload"]) == _payload_verdict(standalone["payload"]), (
            "the two enforcement paths disagree on the evidence verdict"
        )


def _payload_verdict(payload) -> dict | None:
    if payload is None:
        return None
    coverage = payload.get("coverage") or {}
    mutation = payload.get("mutation") or {}
    return {
        "status": payload.get("status"),
        "passed": payload.get("passed"),
        "reasonCode": payload.get("reasonCode"),
        "language": payload.get("language"),
        "backend": payload.get("backend"),
        "schemaVersion": payload.get("schemaVersion"),
        "coverage_passed": coverage.get("passed"),
        "coverage_rate": coverage.get("rate"),
        "coverage_scope": coverage.get("scope"),
        "mutation_passed": mutation.get("passed"),
        "mutation_survived": mutation.get("survived"),
    }


@requires_mutmut
def test_standalone_enforcement_subprocess_budget(enforcement_fixture_repo):
    """Freeze how many processes one standalone run starts.

    Re-frozen 2026-08-20 at 6 git calls, up from 5, when the local CLI moved
    onto the shared contract. The extra call is one `git diff`: the CLI derives
    the changed-line map for the evidence envelope, and the binding derives it
    again for its own targets.

    Measured budget for the change: +1 git invocation per run, constant, not
    per target, against the removal of a second orchestration path. Removing
    the duplicate would mean widening `EnforcementRequest` to carry a
    changed-line map, which buys one cheap call at the cost of a wider
    contract. The enforcement commands -- the expensive ones -- are unchanged
    at 5, which is the number that matters.
    """
    evidence, _markers, spawns = _run_standalone_enforcement(enforcement_fixture_repo)
    replacements = path_replacements(
        fixture_repo=enforcement_fixture_repo, interpreter=sys.executable
    )

    heads: list[str] = []
    for argv in spawns:
        scrubbed = [normalize_text(part, replacements) for part in argv]
        head = scrubbed[0]
        if head == "<interpreter>" and len(scrubbed) > 2 and scrubbed[1] == "-m":
            head = f"<interpreter> -m {scrubbed[2]}"
        elif head == "<interpreter>" and Path(argv[1]).name == "pytest_process.py":
            head = "<interpreter> pytest_process.py"
        elif head == "<interpreter>":
            head = "<interpreter> -c"
        heads.append(head)

    observed = {
        "direct_spawn_count": len(spawns),
        "direct_spawn_heads": heads,
        "evidence_command_count": len(evidence.get("commands") or []),
        "evidence_command_names": [
            str(item.get("name")) for item in (evidence.get("commands") or [])
        ],
        "targets": len(evidence.get("targets") or []),
    }

    compare_to_baseline("python_enforcement_subprocess_budget", observed)


# --------------------------------------------------------------------------
# 5. SQLite statement counts for the crash-ordering critical operations
# --------------------------------------------------------------------------


class _TracingTaskDB:
    """Wrap a real ``TaskDB`` and record the SQL each public call issues.

    ``TaskDB.connect`` is public, and ``sqlite3.Connection.set_trace_callback``
    sees every statement the connection executes, so this observes real
    transactions without importing anything private.
    """

    def __init__(self, db):
        self._db = db
        self.statements: list[str] = []
        self._recording = False
        original_connect = db.connect

        def connect() -> sqlite3.Connection:
            conn = original_connect()
            if self._recording:
                conn.set_trace_callback(self.statements.append)
            return conn

        db.connect = connect  # type: ignore[method-assign]

    def record(self):
        outer = self

        class _Recorder:
            def __enter__(self):
                outer.statements = []
                outer._recording = True
                return outer

            def __exit__(self, *exc):
                outer._recording = False
                return False

        return _Recorder()

    @property
    def summary(self) -> dict:
        # PRAGMA statements are connection setup, identical for every call and
        # not part of the transaction shape being frozen.
        return summarize_statements(
            statement
            for statement in self.statements
            if not statement.strip().upper().startswith("PRAGMA")
        )


@pytest.fixture
def traced_manager(tmp_path):
    from uta.tasks.manager import TaskManager

    manager = TaskManager(tmp_path / "tasks.db")
    return manager, _TracingTaskDB(manager.db)


def _operation_fields(task_id: int, **overrides) -> dict:
    values = {
        "operation_id": "op-baseline-1",
        "repo_task_id": task_id,
        "workflow_run_id": "run-baseline",
        "unit_id": "unit-baseline",
        "phase": "generate_tests",
        "operation_step": "turn",
        "attempt": 1,
        "execution_ordinal": 0,
        "input_fingerprint": "fingerprint-a",
        "prerequisite_operation_ids": [],
        "schema_version": 1,
        "session_id": "session-baseline",
    }
    values.update(overrides)
    return values


def test_task_store_statement_counts_are_frozen(traced_manager, tmp_path):
    """Freeze the SQL shape of the operations the crash ordering depends on.

    The cross-store ordering section names these: the operation claim, the
    artifact-then-completion pair, the terminal state+event write, the
    progress counter+event write, the clean rerun, and task acquisition. A
    slice that splits one of these transactions, or opens a second connection
    mid-transition, changes these counts and fails here.
    """
    manager, traced = traced_manager
    repo = tmp_path / "repo"
    repo.mkdir()
    db = manager.db
    observed: dict[str, dict] = {}

    with traced.record():
        task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    observed["create_task"] = traced.summary

    with traced.record():
        db.start_workflow_operation(**_operation_fields(task_id))
    observed["start_workflow_operation"] = traced.summary

    with traced.record():
        db.record_workflow_operation_cost(
            "op-baseline-1",
            paid_attempt_ordinal=1,
            provider_cost_usd=0.25,
            usage={"input_tokens": 10, "output_tokens": 2},
        )
    observed["record_workflow_operation_cost"] = traced.summary

    with traced.record():
        db.complete_workflow_operation(
            "op-baseline-1",
            resulting_workspace_fingerprint="workspace-a",
            result_artifact_path="results/op-baseline-1.json",
            result_artifact_sha256="0" * 64,
            output_fingerprints={"tests/uta_generated/test_a.py": "1" * 64},
        )
    observed["complete_workflow_operation"] = traced.summary

    with traced.record():
        db.add_event(task_id, None, "baseline_event", "progress", stage="generate_tests")
    observed["add_event"] = traced.summary

    with traced.record():
        db.append_progress_event_batch(
            task_id,
            [{"event_type": "stage_progress", "message": "one", "stage": "generate_tests"}],
            max_events=50,
            max_serialized_bytes=64_000,
        )
    observed["append_progress_event_batch"] = traced.summary

    with traced.record():
        db.acquire_next_task()
    observed["acquire_next_task"] = traced.summary

    with traced.record():
        db.finish_task_with_terminal_event(
            task_id,
            status="COMPLETED",
            message="baseline complete",
            stage="complete_generation",
        )
    observed["finish_task_with_terminal_event"] = traced.summary

    assert_free_of_machine_paths(observed)
    compare_to_baseline("task_store_statement_counts", observed)


def test_clean_rerun_statement_counts_are_frozen(traced_manager, tmp_path):
    """Clean rerun is one atomic recovery transaction; freeze its shape."""
    from uta.testgen.batches import ensure_stable_generation_batches

    manager, traced = traced_manager
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A", "pkg.B"])
    ensure_stable_generation_batches(
        manager.db,
        repo_task_id=task_id,
        ordered_target_ids=["pkg.A", "pkg.B"],
        batch_size=1,
        workflow_run_id="run-before-rerun",
    )
    manager.mark_stopped(task_id, reason="baseline stop")

    with traced.record():
        manager.clean_rerun_generation(
            task_id,
            reason="architecture baseline",
            confirm_task_id=task_id,
            requested_by="baseline",
        )
    observed = {"clean_rerun_generation": traced.summary}

    assert_free_of_machine_paths(observed)
    compare_to_baseline("clean_rerun_statement_counts", observed)


def test_workflow_operation_claim_precedes_its_artifact_and_completion(traced_manager, tmp_path):
    """The claim is written first; that ordering is what makes replay safe.

    Asserted on the traced statement stream rather than on a comment: a
    refactor that moved the claim after the work would leave completed work
    nothing knows to reconcile.
    """
    manager, traced = traced_manager
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id = manager.create_task(repo_path=str(repo), class_fqns=["pkg.A"])

    with traced.record():
        manager.db.start_workflow_operation(**_operation_fields(task_id))
        manager.db.complete_workflow_operation(
            "op-baseline-1",
            resulting_workspace_fingerprint="workspace-a",
            result_artifact_path="results/op-baseline-1.json",
            result_artifact_sha256="0" * 64,
            output_fingerprints={"tests/uta_generated/test_a.py": "1" * 64},
        )

    writes = [
        shape
        for shape in summarize_statements(traced.statements)["statements"]
        if shape == "INSERT workflow_operations" or shape == "UPDATE workflow_operations"
    ]

    assert writes[0] == "INSERT workflow_operations", writes
    assert writes[1] == "UPDATE workflow_operations", writes


# --------------------------------------------------------------------------
# 7. Generation phase routes
# --------------------------------------------------------------------------

GENERATION_SPEC = REPO_ROOT / "uta" / "testgen" / "graph" / "generation-cycle.yaml"

#: Shapes that indicate a real credential rather than a flag name that merely
#: contains the same letters (``--task-db`` is not an OpenAI key).
_CREDENTIAL_PATTERNS = tuple(
    __import__("re").compile(pattern)
    for pattern in (
        r"BEGIN [A-Z ]*PRIVATE KEY",
        r"\bghp_[A-Za-z0-9]{16,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\bsk-[A-Za-z0-9_-]{20,}",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
        r"\bAKIA[0-9A-Z]{16}\b",
    )
)

_PHASE_PROBE_ORDER = (
    "precheck_existing_tests",
    "plan_tests",
    "generate_tests",
    "verify_compile",
    "fix_compile",
    "verify_tests",
    "fix_tests",
    "measure_coverage",
    "fix_coverage",
    "measure_mutation",
    "fix_mutation",
    "delegated_quality_gate",
    "delegated_quality_gate_verify",
    "complete_generation",
)


def _supports(callable_, *args) -> str:
    """Classify a backend phase as supported or not, without doing its work.

    An unsupported phase raises ``NotImplementedError`` before touching
    anything. A supported one, given an empty state, fails on the missing
    state instead — which is the signal that it exists.
    """
    try:
        callable_(*args)
    except NotImplementedError:
        return "unsupported"
    except Exception:  # noqa: BLE001 - any other failure means the phase exists
        return "supported"
    return "supported"


def test_generation_phase_route_baseline():
    """Freeze the ordered phase labels and each language's phase coverage.

    The workflow spec is language neutral; what differs per language is which
    phases the backend implements and which it reports as skipped. Both halves
    are frozen so a slice cannot quietly drop a phase from one language.
    """
    from agent_core.workflow.spec import WorkflowSpec

    from uta.language.java.generation_backend import JavaGenerationCycleBackend
    from uta.language.python.generation_backend import PythonGenerationCycleBackend

    spec = WorkflowSpec.from_file(str(GENERATION_SPEC))

    ordered_phases: list[str] = []
    nodes_by_phase: dict[str, list[str]] = {}
    for node in spec.nodes:
        label = node.config.get("operator_phase")
        if not label:
            continue
        if label not in ordered_phases:
            ordered_phases.append(label)
            nodes_by_phase[label] = []
        nodes_by_phase[label].append(f"{node.name}:{node.uses}")

    backends = {
        "java": JavaGenerationCycleBackend(),
        "python": PythonGenerationCycleBackend(),
    }
    matrix = {
        language: {
            phase: {
                "render_prompt": _supports(backend.render_prompt, phase, {}),
                "interpret": _supports(backend.interpret, phase, {}, {}),
            }
            for phase in _PHASE_PROBE_ORDER
        }
        for language, backend in backends.items()
    }

    observed = {
        "spec_name": spec.name,
        "entry_node": spec.entry,
        "node_count": len(spec.nodes),
        "ordered_operator_phases": ordered_phases,
        "nodes_by_phase": nodes_by_phase,
        "backend_phase_support": matrix,
        "backend_language_attribute": {
            language: getattr(backend, "language", None)
            for language, backend in backends.items()
        },
    }

    compare_to_baseline("generation_phase_routes", observed)


def test_unmigrated_generation_phase_reports_not_implemented():
    """The refusal contract for an unknown phase, for both languages."""
    from uta.language.java.generation_backend import JavaGenerationCycleBackend
    from uta.language.python.generation_backend import PythonGenerationCycleBackend

    for backend in (JavaGenerationCycleBackend(), PythonGenerationCycleBackend()):
        with pytest.raises(NotImplementedError):
            backend.run_phase("no_such_phase", {})
        with pytest.raises(NotImplementedError):
            backend.render_prompt("no_such_phase", {})
        with pytest.raises(NotImplementedError):
            backend.interpret("no_such_phase", {}, {})


# --------------------------------------------------------------------------
# 8. Managed vs standalone generation boundaries
# --------------------------------------------------------------------------


def test_standalone_generation_execution_boundary_baseline(tmp_path, monkeypatch):
    """Freeze the private persistence boundary a taskless call is given.

    Directory layout, permissions and the config snapshot a standalone run
    writes are the contract that keeps ``uta run`` behaving like a managed
    task without owning a shared database.
    """
    from uta.testgen.batch import BatchGenerationRequest
    from uta.shared.targets import TargetIdentity
    from uta.tasks.db import TaskDB
    from uta.testgen.standalone_execution import (
        open_standalone_generation_execution,
        project_standalone_final_state,
    )

    monkeypatch.setenv("UTA_RUNNER_HOME", str(tmp_path / "runner"))
    observed: dict[str, object] = {}

    for language, target in (
        (
            "python",
            TargetIdentity(
                language="python",
                target_id="pysymbol:src/demo.py::build",
                display_name="build",
                source_path="src/demo.py",
                symbol="build",
                granularity="function",
            ),
        ),
        (
            "java",
            TargetIdentity(
                language="java",
                target_id="javaclass:com.example.Demo",
                display_name="com.example.Demo",
                source_path="src/main/java/com/example/Demo.java",
                granularity="class",
            ),
        ),
    ):
        repo = tmp_path / f"repo_{language}"
        repo.mkdir()
        request = BatchGenerationRequest(language=language, repo_path=repo, targets=[target])
        with open_standalone_generation_execution(request) as execution:
            root = execution.root
            snapshot = json.loads(
                TaskDB(execution.request.task_db_path).get_repo_task(
                    int(execution.request.task_id)
                )["config_snapshot_json"]
            )
            layout = sorted(
                str(path.relative_to(root)) for path in root.rglob("*") if path.is_dir()
            )
            observed[language] = {
                "root_is_under_runner_home": root.parent.parent == tmp_path / "runner",
                "root_mode": oct(root.stat().st_mode & 0o777),
                "task_db_name": execution.request.task_db_path.name,
                "directory_layout": layout,
                "config_snapshot_keys": sorted(snapshot),
                "generation_engine_version": snapshot.get("generation_engine_version"),
                "generation_cycle_v2_enabled": snapshot.get("generation_cycle_v2_enabled"),
            }
        assert not root.exists(), "standalone execution must clean up its own root"

    projected = project_standalone_final_state(
        {
            "task_id": 99,
            "task_db_path": "/should/not/leak",
            "repo_path": "/should/not/leak",
            "results": {"a": 1},
            "delivery_branch": "feature/x",
            "result_summary": {"passed": 1},
            "prompt_artifact_scope": object(),
        },
        original_task_id=None,
        original_task_db_path=None,
    )
    observed["projection_keys"] = sorted(projected)

    compare_to_baseline("standalone_generation_boundary", normalize(observed, path_replacements()))


def test_managed_task_creation_contract_baseline(tmp_path):
    """Freeze the managed-generation task projection for both languages."""
    from uta.tasks.manager import TaskManager

    from uta.shared.targets import TargetIdentity

    python_target = TargetIdentity(
        language="python",
        target_id="pyfile:jobs/forecast.py",
        display_name="jobs/forecast.py",
        source_path="jobs/forecast.py",
        granularity="file",
    )
    observed: dict[str, object] = {}
    for language in ("java", "python"):
        repo = tmp_path / f"repo_{language}"
        repo.mkdir()
        manager = TaskManager(tmp_path / f"tasks_{language}.db")
        if language == "java":
            task_id = manager.create_task(
                repo_path=str(repo), class_fqns=["com.example.Demo"]
            )
        else:
            task_id = manager.create_task_targets(
                repo_path=str(repo), targets=[python_target], language="python"
            )
        task = manager.get_task(task_id)
        class_tasks = manager.list_class_tasks(task_id)
        observed[language] = {
            "task_keys": sorted(task),
            "status": task.get("status"),
            "language": task.get("language"),
            "class_task_count": len(class_tasks),
            "class_task_keys": sorted(class_tasks[0]) if class_tasks else [],
            "class_task_statuses": sorted({str(row.get("status")) for row in class_tasks}),
            "status_payload_keys": sorted(manager.status_payload(task_id)),
        }

    compare_to_baseline("managed_generation_contract", normalize(observed, path_replacements()))


# --------------------------------------------------------------------------
# 9. The baselines themselves must be portable
# --------------------------------------------------------------------------


#: Credential shapes, as anchored patterns rather than bare substrings.
#
# The substring version of this check reported `--task-db` as a credential,
# because "ta[sk-d]b" contains "sk-". A scanner that cries wolf on a CLI flag
# gets muted, so each shape now requires a token boundary and the run of
# characters a real key actually has.
_CREDENTIAL_SHAPES = (
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),      # OpenAI
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),       # GitHub personal access token
    re.compile(r"\bgho_[A-Za-z0-9]{20,}"),       # GitHub OAuth token
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),         # AWS access key id
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"),   # GitLab personal access token
)


def test_baseline_fixtures_carry_no_machine_local_paths_or_credentials():
    """Acceptance criterion 2, asserted rather than assumed.

    A fixture that embeds one developer's home directory fails everywhere
    else, so this scans every committed artifact for absolute paths and for
    anything shaped like a credential.
    """
    assert FIXTURE_DIR.is_dir(), "baseline fixtures directory is missing"
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    assert fixtures, "no baseline fixtures were captured"

    for fixture in fixtures:
        text = fixture.read_text(encoding="utf-8")
        assert_free_of_machine_paths(text)
        for pattern in _CREDENTIAL_SHAPES:
            match = pattern.search(text)
            assert match is None, (
                f"{fixture.name} may contain a credential: {match.group(0)[:12]}..."
            )
        json.loads(text)
