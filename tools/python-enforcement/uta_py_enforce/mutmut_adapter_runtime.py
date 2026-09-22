"""Runtime patch that constrains mutmut 3 generation to a UTA policy.

This file is executed as a script in the target repository. Keep it independent
of the full UTA package so sparse-checkout local enforcement uses the identical
adapter as CI and repair verification.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
import faulthandler
import importlib
import json
import os
from os import makedirs
from pathlib import Path
import signal
import sys
import time
from typing import Any, Mapping

import mutmut.__main__ as mutmut_main

from uta_py_enforce.optional_plugins import DEFAULT_TEST_TIMEOUT_SECONDS, TIMEOUT_ENV, TIMEOUT_SETTING_ENV

#: Wall-clock ceiling on one in-process ``pytest.main`` phase.
PHASE_TIMEOUT_ENV = "UTA_MUTMUT_PYTEST_PHASE_TIMEOUT_SECONDS"

#: Generous next to the per-test ``PYTEST_TIMEOUT``, because one phase runs the
#: whole selected test file. It only has to be shorter than the enforcement
#: command timeout that would otherwise reap the hang with no traceback.
DEFAULT_PHASE_TIMEOUT_SECONDS = 600

#: Wall-clock ceiling on the adapter process as a whole. A phase watchdog only
#: covers ``pytest.main`` calls; mutmut does its whole mutant-generation block
#: -- copy_src_dir, store_lines_covered_by_tests, the Pool-based create_mutants
#: -- before the first phase, and waits on forked children after them. Neither
#: region is reachable from a wrapper around execute_pytest.
ADAPTER_TIMEOUT_ENV = "UTA_MUTMUT_ADAPTER_TIMEOUT_SECONDS"

#: Wall-clock / CPU floor for one mutant. Mutmut 3.5 SIGXCPUs after
#: ``(estimated_test_time + 1) * 15`` seconds, which is ~20s for a fast
#: unmutated file and records the mutant as timeout instead of killed.
#: Flooring to the pytest-timeout keeps python-enforce and repair on one
#: threshold: a hanging test fails the pytest-timeout and kills the mutant.
PER_MUTANT_TIMEOUT_ENV = "UTA_PYTHON_MUTATION_PER_MUTANT_TIMEOUT_SECONDS"
PER_MUTANT_MEMORY_LIMIT_MB_ENV = "UTA_PYTHON_MUTATION_PER_MUTANT_MEMORY_LIMIT_MB"
DEFAULT_PER_MUTANT_MEMORY_LIMIT_MB = 1536


def _install_src_trampoline_normalizer() -> None:
    """Normalize src-layout module names in the active Mutmut process.

    The sitecustomize shim also installs this for subprocess imports, but the
    adapter owns the Mutmut process that performs stats and mutant execution.
    Installing here makes the shared CI/repair/local adapter authoritative and
    avoids depending on Python's best-effort sitecustomize import.
    """
    original = getattr(mutmut_main, "record_trampoline_hit", None)
    if not callable(original) or getattr(original, "_uta_src_normalized", False):
        return

    def record_trampoline_hit(name):
        if isinstance(name, str) and name.startswith("src."):
            name = name[4:]
        return original(name)

    record_trampoline_hit._uta_src_normalized = True
    mutmut_main.record_trampoline_hit = record_trampoline_hit


_install_src_trampoline_normalizer()


def _module_is_loaded_from(module: Any, root: Path) -> bool:
    """Return whether a module was imported from the active mutant workspace."""
    paths = []
    raw_path = getattr(module, "__file__", None)
    if raw_path:
        paths.append(raw_path)
    # Namespace packages have no __file__, but still cache workspace state.
    try:
        paths.extend(getattr(module, "__path__", ()))
    except (KeyError, OSError, RuntimeError):
        pass
    for path in paths:
        try:
            Path(path).resolve().relative_to(root)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        return True
    return False


def _purge_synthetic_modules(before: frozenset) -> None:
    """Drop stub modules a phase injected straight into ``sys.modules``.

    A generated test whose dependencies are missing installs stubs --
    ``sys.modules[name] = types.ModuleType(name)`` -- so it can import its
    target at all. ``_purge_modules_loaded_from`` cannot see those: it decides
    by ``__file__``/``__path__``, and a bare module has neither, so the stub
    outlived every phase in the interpreter. The next phase then re-imported
    the real module against the stub and died on it -- observed as
    ``AttributeError: module 'requests' has no attribute 'Session'`` during
    collection, reported only as "mutation backend failed".

    Only modules the phase *added* are dropped, and only bare ``ModuleType``
    objects with no import spec, loader, file, or package path. CPython
    built-ins such as ``_zoneinfo`` legitimately have no file; removing one
    invalidates C types retained by their public Python wrapper.

    Dropping a stub takes its whole subtree with it. A stub shadows a real
    package whose submodules the same phase imported for real, and those are
    file-backed, so a name-by-name purge left them cached. Re-importing the
    parent then re-executed its ``__init__`` against already-cached children,
    which binds whatever ``__init__`` imports by name and nothing else: the
    package came back missing exactly the submodule attributes the import
    system would have set. That reads as the same ``AttributeError`` one
    line further down -- ``requests.Session`` resolves, ``requests.adapters``
    does not.
    """
    synthetic: set[str] = set()
    for name in [name for name in sys.modules if name not in before]:
        module = sys.modules.get(name)
        if module is None:
            continue
        if getattr(module, "__spec__", None) is not None or getattr(module, "__loader__", None) is not None:
            continue
        if getattr(module, "__file__", None):
            continue
        try:
            if list(getattr(module, "__path__", ()) or ()):
                continue
        except (KeyError, OSError, RuntimeError, TypeError):
            pass
        synthetic.add(name)
    if not synthetic:
        return
    for name in list(sys.modules):
        parts = name.split(".")
        if any(".".join(parts[:end]) in synthetic for end in range(1, len(parts) + 1)):
            sys.modules.pop(name, None)


def _purge_modules_loaded_from(root: Path) -> None:
    """Prevent repository globals from leaking into mutmut's next pytest phase."""
    resolved_root = root.resolve()
    modules = list(sys.modules.items())
    # Resolve namespace paths before deleting any parents: _NamespacePath may
    # consult sys.modules[parent]. Never leave children of a purged package.
    removed = {
        name for name, module in modules
        if module is not None and _module_is_loaded_from(module, resolved_root)
    }
    for name, _module in modules:
        parts = name.split(".")
        if any(".".join(parts[:end]) in removed for end in range(1, len(parts) + 1)):
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _pytest_phase_roots(params: list[str]) -> list[Path]:
    """Return every source root pytest may load mutable test state from."""
    roots = [Path.cwd().resolve()]
    for raw_param in params:
        candidate = str(raw_param).split("::", 1)[0]
        if not candidate or candidate.startswith("-"):
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            if path.is_file():
                roots.append(path.resolve().parent)
        except OSError:
            continue
    return list(dict.fromkeys(roots))


def phase_timeout_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Seconds one pytest phase may take; 0 or negative disables the watchdog."""
    env = os.environ if environ is None else environ
    try:
        return max(int(str(env.get(PHASE_TIMEOUT_ENV, "")).strip()), 0)
    except ValueError:
        return DEFAULT_PHASE_TIMEOUT_SECONDS


def per_mutant_timeout_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Shared mutant timeout: explicit override, else pytest-timeout, else 120s."""
    env = os.environ if environ is None else environ
    for key in (PER_MUTANT_TIMEOUT_ENV, TIMEOUT_ENV, TIMEOUT_SETTING_ENV):
        raw = str(env.get(key, "") or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    return DEFAULT_TEST_TIMEOUT_SECONDS


def mutant_memory_limit_bytes(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    try:
        limit_mb = int(str(env.get(PER_MUTANT_MEMORY_LIMIT_MB_ENV, "") or ""))
    except ValueError:
        limit_mb = DEFAULT_PER_MUTANT_MEMORY_LIMIT_MB
    if limit_mb <= 0:
        limit_mb = DEFAULT_PER_MUTANT_MEMORY_LIMIT_MB
    return limit_mb * 1024 * 1024


def mutant_process_rss_bytes(pid: int) -> int | None:
    try:
        pages = int(Path(f"/proc/{pid}/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def terminate_runaway_mutant(pid: int, name: str, limit_bytes: int) -> bool:
    rss = mutant_process_rss_bytes(pid)
    if rss is None or rss < limit_bytes:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    print(f"UTA_MUTANT_RESOURCE_KILL mutant={name} pid={pid} rss={rss} limit={limit_bytes}",
          file=sys.stderr, flush=True)
    return True


def mutant_wall_timeout_seconds(
    estimated_time: float,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Mutmut's wall-clock kill, never tighter than the shared pytest-timeout."""
    native = (float(estimated_time) + 1.0) * 15.0
    return max(native, float(per_mutant_timeout_seconds(environ)))


def mutant_cpu_timeout_seconds(
    estimated_time: float,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Mutmut's RLIMIT_CPU kill, never tighter than the shared pytest-timeout."""
    native = (float(estimated_time) + 1.0) * 30.0
    return max(native, float(per_mutant_timeout_seconds(environ)))


def adapter_timeout_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Seconds the whole adapter may take; 0 disables, and is the default.

    Unset means off because only the caller knows the budget it will reap this
    process at, and a watchdog guessing high adds nothing while one guessing
    low would fail runs that were going to succeed. The enforcement lane sets
    it from its own command timeout.
    """
    env = os.environ if environ is None else environ
    try:
        return max(int(str(env.get(ADAPTER_TIMEOUT_ENV, "")).strip()), 0)
    except ValueError:
        return 0


#: Deadline and stderr descriptor per nested watchdog. faulthandler keeps ONE
#: process-global timer, so arming an inner watchdog replaces an outer one and
#: cancelling the inner destroys it outright. A production run lost its
#: adapter-wide watchdog exactly that way, the first time any pytest phase
#: completed normally, and then hung unbounded.
_WATCHDOGS: list[tuple[float, int]] = []


def _arm_nearest_watchdog() -> None:
    """Point the single global timer at whichever deadline comes first."""
    faulthandler.cancel_dump_traceback_later()
    if not _WATCHDOGS:
        return
    deadline, stderr_fd = min(_WATCHDOGS)
    remaining = deadline - time.monotonic()
    # Already past due: fire as soon as the timer thread can run.
    faulthandler.dump_traceback_later(max(remaining, 0.01), exit=True, file=stderr_fd)


if hasattr(os, "register_at_fork"):
    # Mutmut forks after collecting stats. An active faulthandler C thread
    # leaves a locked cancellation semaphore in the child, where that thread
    # no longer exists. Cancel BEFORE fork, then create process-local timers.
    # Keep absolute deadlines and inherited descriptors: neither process may
    # extend the adapter budget, and nested contexts must still unwind normally.
    os.register_at_fork(
        before=faulthandler.cancel_dump_traceback_later,
        after_in_parent=_arm_nearest_watchdog,
        after_in_child=_arm_nearest_watchdog,
    )


@contextlib.contextmanager
def _watchdog(seconds: int):
    """Abort the process with every thread's traceback if the body wedges.

    ``pytest-timeout`` guards test setup/call/teardown, so a phase that
    deadlocks while *importing* the target -- collection, before the first test
    runs -- is invisible to it, and to the per-mutant timeout that has not
    started yet. Only the enforcement command timeout catches that, and it
    reports exit 124 with no stack, which cannot name the offending import.

    ``faulthandler`` is the mechanism that survives the failure it reports: the
    watchdog is a C thread, so it fires even when every Python thread is parked
    on a lock nothing will release -- the state a wedged phase leaves behind.

    It dumps to a dup of the real stderr taken before the body starts, because
    pytest's parent-level capture redirects fd 2 into a buffer it flushes on exit,
    and ``exit=True`` never reaches that exit.

    Nesting is tracked rather than assumed: see ``_WATCHDOGS``.
    """
    try:
        stderr_fd = os.dup(2) if seconds else None
    except OSError:
        # Nothing to report through, and a watchdog that kills the process
        # silently would be worse than the hang it replaces.
        stderr_fd = None
    if stderr_fd is None:
        yield
        return
    entry = (time.monotonic() + seconds, stderr_fd)
    _WATCHDOGS.append(entry)
    _arm_nearest_watchdog()
    try:
        yield
    finally:
        _WATCHDOGS.remove(entry)
        _arm_nearest_watchdog()
        os.close(stderr_fd)


def _install_pytest_phase_isolation() -> None:
    """Reset target/test imports after stats, clean-test, and mutant pytest runs.

    Mutmut 3 invokes ``pytest.main`` repeatedly in one interpreter. Repository
    modules therefore retain class-level mocks and other mutable globals unless
    the adapter clears them. Each phase must observe the same fresh-import state
    as a normal standalone pytest command.

    Each phase is also bounded, for the reasons in ``_watchdog``.
    """
    runner_type = mutmut_main.PytestRunner
    original_execute = runner_type.execute_pytest
    if getattr(original_execute, "_uta_phase_isolated", False):
        return

    def execute_pytest(self, params, **kwargs):
        roots = _pytest_phase_roots(params)
        before = frozenset(sys.modules)
        try:
            with _watchdog(phase_timeout_seconds()):
                return original_execute(self, params, **kwargs)
        finally:
            for root in roots:
                _purge_modules_loaded_from(root)
            _purge_synthetic_modules(before)

    execute_pytest._uta_phase_isolated = True
    runner_type.execute_pytest = execute_pytest


def _install_mutant_timeout_floor() -> None:
    """Keep mutmut's SIGXCPU floor on the same clock as pytest-timeout.

    Mutmut 3.5 ignores ``runner=`` and kills a mutant after
    ``(estimated_test_time + 1) * 15`` wall seconds (and ``* 30`` CPU). A fast
    unmutated estimate makes that ~20s, so repair records timeout while
    python-enforce's pytest-timeout (120s) fails the hanging test and kills it.
    """
    if not hasattr(mutmut_main, "timeout_checker"):
        return
    original = mutmut_main.timeout_checker
    if getattr(original, "_uta_timeout_floor", False):
        return

    lock = getattr(mutmut_main, "START_TIMES_BY_PID_LOCK", None)
    memory_limit = mutant_memory_limit_bytes()
    resource_killed: set[tuple[str, int]] = set()

    def timeout_checker(mutants):
        def inner_timeout_checker():
            while True:
                time.sleep(1)
                now = datetime.now()
                for mutant, mutant_name, _result in mutants:
                    if lock is not None:
                        with lock:
                            start_times = dict(mutant.start_time_by_pid)
                    else:
                        start_times = dict(mutant.start_time_by_pid)
                    estimate = float(mutant.estimated_time_of_tests_by_mutant.get(mutant_name) or 0.0)
                    budget = mutant_wall_timeout_seconds(estimate)
                    for pid, start_time in start_times.items():
                        key = (mutant_name, pid)
                        if key not in resource_killed and terminate_runaway_mutant(pid, mutant_name, memory_limit):
                            resource_killed.add(key)
                            continue
                        if (now - start_time).total_seconds() <= budget:
                            continue
                        try:
                            os.kill(pid, signal.SIGXCPU)
                        except ProcessLookupError:
                            pass

        return inner_timeout_checker

    timeout_checker._uta_timeout_floor = True
    mutmut_main.timeout_checker = timeout_checker

    try:
        import resource
    except ImportError:
        return
    original_setrlimit = resource.setrlimit
    if getattr(original_setrlimit, "_uta_timeout_floor", False):
        return

    def setrlimit(which, limits):
        if which == getattr(resource, "RLIMIT_CPU", None):
            floor = per_mutant_timeout_seconds()
            soft, hard = limits
            limits = (max(int(soft), floor), max(int(hard), floor + 1))
        return original_setrlimit(which, limits)

    setrlimit._uta_timeout_floor = True
    resource.setrlimit = setrlimit
    mutmut_main.resource.setrlimit = setrlimit


def _prepare_also_copy_parent_dirs() -> None:
    """Prepare destinations mutmut 3 needs for support-file copies."""
    config_owner = getattr(mutmut_main, "mutmut", None)
    config = getattr(config_owner, "config", None)
    for raw_path in getattr(config, "also_copy", ()) or ():
        source = Path(str(raw_path))
        if source.is_absolute():
            try:
                source = source.relative_to(Path.cwd())
            except ValueError:
                continue
        destination = Path("mutants") / source
        # The import-compat shim may mirror resources.* into mutants/ before
        # metadata generation starts. Mutmut must replace that mirror with its
        # own copy; otherwise copytree sees source and destination as one file.
        if destination.is_symlink():
            destination.unlink()
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)


def _mutation_module():
    last_error = None
    for module_name in (
        "mutmut.file_mutation",
        "mutmut.node_mutation",
        "mutmut.mutation.file_mutation",
    ):
        try:
            return importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            last_error = exc
    raise last_error or ModuleNotFoundError("mutmut mutation module")


def _operator_label(node_type: Any, operator: Any) -> str:
    parts = (
        getattr(node_type, "__name__", ""),
        getattr(operator, "__name__", ""),
        getattr(operator, "__qualname__", ""),
        operator.__class__.__name__,
    )
    return ":".join(str(part) for part in parts if str(part))


def _operator_matches(policy_name: str, node: Any, node_type: Any, operator: Any) -> bool:
    name = str(policy_name or "")
    node_name = str(getattr(node_type, "__name__", ""))
    concrete_name = node.__class__.__name__
    label = _operator_label(node_type, operator).lower()
    is_swap = "operator_swap_op" in label
    if not name or name == "statement":
        return True
    if name == "return_value":
        return node_name == "Return" or concrete_name == "Return"
    if name == "constant_value":
        return node_name in {"Constant", "BaseNumber", "BaseString"} or concrete_name in {
            "Constant",
            "Integer",
            "Float",
            "Imaginary",
            "SimpleString",
            "ConcatenatedString",
        }
    if name == "container_value":
        return node_name in {"List", "Tuple", "Dict", "Set"} or concrete_name in {
            "List",
            "Tuple",
            "Dict",
            "Set",
        }
    if name == "math_operator":
        return node_name == "BinOp" or concrete_name in {"BinaryOperation", "AugAssign"} or (
            is_swap and concrete_name in {"BinaryOperation", "AugAssign"}
        )
    if name == "call_argument":
        return node_name == "Call" or concrete_name == "Call"
    if name == "boolean_operator":
        return node_name in {"BoolOp", "If", "While", "UnaryOp"} or concrete_name in {
            "BooleanOperation",
            "If",
            "While",
            "UnaryOperation",
        } or (is_swap and concrete_name in {"BooleanOperation", "If", "While", "UnaryOperation"})
    if name == "comparison_negation":
        return (node_name == "Compare" or concrete_name == "ComparisonTarget") and any(
            token in label for token in ("negat", "not", "boolean")
        )
    if name == "comparison_boundary":
        return (
            node_name == "Compare"
            or concrete_name == "ComparisonTarget"
            or (is_swap and concrete_name == "ComparisonTarget")
        ) and not any(token in label for token in ("negat", "not", "boolean"))
    return name.lower() in label


def _install_policy(policy: dict[str, Any]) -> None:
    file_mutation = _mutation_module()
    allowed_lines = {int(value) for value in policy.get("selectedLines") or policy.get("allowedLines") or ()}
    operator_names = {
        int(line): {str(name) for name in names or () if str(name)}
        for line, names in (policy.get("operatorByLine") or {}).items()
    }
    opportunity_ids = {
        int(line): str(value)
        for line, value in (policy.get("opportunityIdByLine") or {}).items()
        if str(value)
    }
    line_by_name: dict[str, int] = {}
    policy_operator_by_name: dict[str, str] = {}
    actual_operator_by_name: dict[str, str] = {}
    opportunity_id_by_name: dict[str, str] = {}
    emitted_lines: set[int] = set()

    original_skip = file_mutation.MutationVisitor._skip_node_and_children

    def skip_node_and_children(self, node):
        if isinstance(node, file_mutation.cst.ClassDef) and node.decorators:
            return False
        return original_skip(self, node)

    def selected_operator(line, node, node_type, operator):
        wanted = operator_names.get(int(line)) or set()
        if not wanted:
            return ""
        for policy_name in sorted(wanted):
            if _operator_matches(policy_name, node, node_type, operator):
                return policy_name
        return None

    def create_mutations(self, node):
        position = self.get_metadata(file_mutation.PositionProvider, node, None)
        line = int(position.start.line) if position else 0
        if line not in allowed_lines or line in emitted_lines:
            return
        for node_type, operator in self._operators:
            if not isinstance(node, node_type):
                continue
            policy_operator = selected_operator(line, node, node_type, operator)
            if policy_operator is None:
                continue
            for mutated_node in operator(node):
                mutation = file_mutation.Mutation(
                    original_node=node,
                    mutated_node=mutated_node,
                    contained_by_top_level_function=self.get_metadata(
                        file_mutation.OuterFunctionProvider,
                        node,
                        None,
                    ),
                )
                mutation._uta_line = line
                mutation._uta_policy_operator_name = policy_operator or ""
                mutation._uta_actual_operator_name = _operator_label(node_type, operator)
                mutation._uta_opportunity_id = opportunity_ids.get(line, "")
                self.mutations.append(mutation)
                emitted_lines.add(line)
                return

    original_arrangement = file_mutation.function_trampoline_arrangement

    def arrangement(function, mutants, class_name):
        materialized = list(mutants)
        result = original_arrangement(function, materialized, class_name)
        for mutation, name in zip(materialized, result[-1]):
            line = getattr(mutation, "_uta_line", None)
            if not line:
                continue
            key = str(name)
            line_by_name[key] = int(line)
            policy_operator_by_name[key] = str(getattr(mutation, "_uta_policy_operator_name", "") or "")
            actual_operator_by_name[key] = str(getattr(mutation, "_uta_actual_operator_name", "") or "")
            opportunity_id_by_name[key] = str(getattr(mutation, "_uta_opportunity_id", "") or "")
        return result

    original_save = mutmut_main.SourceFileMutationData.save
    original_copy_also_copy_files = mutmut_main.copy_also_copy_files

    def copy_also_copy_files():
        _prepare_also_copy_parent_dirs()
        return original_copy_also_copy_files()

    def save(self):
        original_save(self)
        line_by_key = {}
        policy_operator_by_key = {}
        actual_operator_by_key = {}
        opportunity_id_by_key = {}
        for key in self.exit_code_by_key:
            short = str(key).rsplit(".", 1)[-1]
            if short not in line_by_name:
                continue
            line_by_key[str(key)] = line_by_name[short]
            if policy_operator_by_name.get(short):
                policy_operator_by_key[str(key)] = policy_operator_by_name[short]
            if actual_operator_by_name.get(short):
                actual_operator_by_key[str(key)] = actual_operator_by_name[short]
            if opportunity_id_by_name.get(short):
                opportunity_id_by_key[str(key)] = opportunity_id_by_name[short]
        if line_by_key:
            sidecar = self.meta_path.with_suffix(self.meta_path.suffix + ".uta.json")
            sidecar.write_text(
                json.dumps(
                    {
                        "line_by_key": line_by_key,
                        "operator_by_key": policy_operator_by_key,
                        "actual_operator_by_key": actual_operator_by_key,
                        "opportunity_id_by_key": opportunity_id_by_key,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )

    file_mutation.MutationVisitor._skip_node_and_children = skip_node_and_children
    file_mutation.MutationVisitor._create_mutations = create_mutations
    file_mutation.function_trampoline_arrangement = arrangement
    mutmut_main.SourceFileMutationData.save = save
    mutmut_main.copy_also_copy_files = copy_also_copy_files


def main(argv: list[str]) -> int:
    max_children = max(1, int(argv[0]))
    mode = argv[1]
    policy_path = Path(argv[2])
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    _install_policy(policy)
    if mode == "metadata":
        if hasattr(mutmut_main, "ensure_config_loaded"):
            mutmut_main.ensure_config_loaded()
        makedirs(Path("mutants"), exist_ok=True)
        mutmut_main.copy_src_dir()
        stats = mutmut_main.create_mutants(max_children)
        if stats is not None:
            print(
                "UTA_MUTMUT_GENERATION_STATS "
                f"mutated={stats.mutated} unmodified={stats.unmodified} ignored={stats.ignored}"
            )
        mutmut_main.copy_also_copy_files()
        return 0
    if mode == "run":
        _install_pytest_phase_isolation()
        _install_mutant_timeout_floor()
        # Generation and execution share the same worker budget. Omitting this
        # lets mutmut fork CPU-count workers despite the caller requesting one.
        run_args = ["run", "--max-children", str(max_children)]
        if hasattr(mutmut_main, "cli"):
            mutmut_main.cli(args=run_args, standalone_mode=False)
        else:
            import runpy

            sys.argv = ["mutmut", *run_args]
            runpy.run_module("mutmut.__main__", run_name="__main__")
        return 0
    raise SystemExit(f"unsupported UTA mutmut adapter mode: {mode}")


if __name__ == "__main__":
    from uta_py_enforce.process_completion import finish_process

    # SystemExit has to be caught, not propagated. mutmut calls exit(1) when the
    # clean test fails (__main__.py:1227), and letting that escape skipped
    # finish_process entirely -- so nothing armed the shutdown bound, and the
    # repository's non-daemon Twisted reactor held the interpreter in
    # threading._shutdown for two hours. No watchdog can cover that window:
    # both are context managers that disarm on the way out, including on the
    # exception path. Bounding interpreter exit is finish_process's job, and it
    # only has to actually be called.
    try:
        with _watchdog(adapter_timeout_seconds()):
            _exit_code = main(sys.argv[1:])
    except SystemExit as exc:
        _exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    raise SystemExit(finish_process(_exit_code, "mutmut_adapter"))
