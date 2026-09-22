"""Mutmut configuration, command construction, and workspace lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tokenize
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from uta_py_enforce.mutation_workspace import (
    mutation_support_copy_paths as _mutmut_support_copy_paths,
    mutmut_pytest_add_cli_args as _mutmut_pytest_add_cli_args,
)
from uta_py_enforce.mutmut_adapter import adapter_command as _shared_mutmut_adapter_command

from uta.language.python.module_resolver import resolve_python_module
from uta.language.python.verification.mutmut_import_compat import write_mutmut_import_compat
from uta.shared.config import settings


@dataclass(frozen=True)
class _MutmutConfigOverlay:
    path: Path
    source_path: str
    repo: Path
    python_bin: str
    import_compat_dir: Path
    support_copy_paths: Sequence[str] = field(default_factory=tuple)
    test_paths: Sequence[str] = field(default_factory=tuple)
    pytest_add_cli_args: Sequence[str] = field(default_factory=tuple)
    original_text: Optional[str] = None
    configured: bool = False

    def apply(self) -> None:
        if not self.configured:
            return
        if self.path.name == "pyproject.toml":
            self._apply_pyproject()
            return
        self._apply_setup_cfg()

    def _apply_setup_cfg(self) -> None:
        from configparser import ConfigParser

        parser = ConfigParser()
        if self.original_text is not None:
            parser.read_string(self.original_text)
        if not parser.has_section("mutmut"):
            parser.add_section("mutmut")
        parser.set("mutmut", "paths_to_mutate", self.source_path)
        if self.test_paths:
            parser.set("mutmut", "tests_dir", _config_list_value(self.test_paths))
            # Mutmut 3 appends tests_dir to its test-selection arguments. Keep
            # one authoritative path list; setting both fields executes every
            # selected test twice during stats, clean, and forced-fail phases.
            parser.remove_option("mutmut", "pytest_add_cli_args_test_selection")
            parser.set(
                "mutmut",
                "runner",
                _mutmut_runner_command(
                    self.python_bin,
                    self.test_paths,
                    repo=self.repo,
                    import_compat_dir=self.import_compat_dir,
                    source_path=self.source_path,
                ),
            )
        if self.pytest_add_cli_args:
            parser.set("mutmut", "pytest_add_cli_args", _config_list_value(self.pytest_add_cli_args))
        if self.support_copy_paths:
            also_copy = _merge_config_list(
                parser.get("mutmut", "also_copy", fallback=""),
                self.support_copy_paths,
            )
            parser.set("mutmut", "also_copy", _config_list_value(also_copy))
        with io.StringIO() as buffer:
            parser.write(buffer)
            self.path.write_text(buffer.getvalue(), encoding="utf-8")

    def _apply_pyproject(self) -> None:
        original = self.original_text or ""
        lines = [
            "[tool.mutmut]",
            f"paths_to_mutate = {_toml_string_list([self.source_path])}",
        ]
        if self.support_copy_paths:
            lines.append(f"also_copy = {_toml_string_list(self.support_copy_paths)}")
        if self.pytest_add_cli_args:
            lines.append(f"pytest_add_cli_args = {_toml_string_list(self.pytest_add_cli_args)}")
        if self.test_paths:
            lines.append(f"tests_dir = {_toml_string_list(self.test_paths)}")
        updated = _replace_toml_section(original, "[tool.mutmut]", "\n".join(lines))
        self.path.write_text(updated, encoding="utf-8")

    def restore(self) -> None:
        if not self.configured:
            return
        if self.original_text is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(self.original_text, encoding="utf-8")


def _mutmut_config_overlay(
    repo: Path,
    source_path: str,
    test_paths: Sequence[str],
    version_output: str,
    *,
    python_bin: str,
    import_compat_dir: Path,
    support_test_paths: Optional[Sequence[str]] = None,
) -> _MutmutConfigOverlay:
    pyproject = repo / "pyproject.toml"
    pyproject_text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else None
    configured = _mutmut_major_version(version_output) >= 3
    support_paths_source = support_test_paths if support_test_paths is not None else test_paths
    support_copy_paths = _mutmut_support_copy_paths(repo, source_path, support_paths_source)
    pytest_add_cli_args = _mutmut_pytest_add_cli_args(repo, support_paths_source)
    # Mutmut 3 treats pyproject.toml as the authoritative project config even
    # when it does not yet contain a tool.mutmut section. Overlay that file in
    # the same way as standalone enforcement; a sibling setup.cfg is ignored
    # by real projects with pyproject configuration and loses test selection.
    if configured and pyproject_text is not None:
        return _MutmutConfigOverlay(
            pyproject,
            source_path,
            repo,
            python_bin,
            import_compat_dir,
            support_copy_paths,
            tuple(str(path) for path in test_paths),
            pytest_add_cli_args,
            pyproject_text,
            configured,
        )
    setup_cfg = repo / "setup.cfg"
    original = setup_cfg.read_text(encoding="utf-8") if setup_cfg.exists() else None
    return _MutmutConfigOverlay(
        setup_cfg,
        source_path,
        repo,
        python_bin,
        import_compat_dir,
        support_copy_paths,
        tuple(str(path) for path in test_paths),
        pytest_add_cli_args,
        original,
        configured,
    )


def _merge_config_list(existing: str, values: Sequence[str]) -> List[str]:
    result: List[str] = []
    existing_values = [line.strip() for line in existing.splitlines()]
    for value in existing_values + [str(value) for value in values]:
        if value and value not in result:
            result.append(value)
    return result


def _toml_string_list(values: Sequence[str]) -> str:
    return json.dumps([str(value) for value in values])


def _replace_toml_section(text: str, header: str, body: str) -> str:
    lines = str(text or "").splitlines()
    start: Optional[int] = None
    for index, line in enumerate(lines):
        if line.strip() == header:
            start = index
            break
    if start is None:
        prefix = lines[:]
        while prefix and not prefix[-1].strip():
            prefix.pop()
        return "\n".join([*prefix, "", body]).lstrip("\n").rstrip() + "\n"
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    prefix = lines[:start]
    suffix = lines[end:]
    while prefix and not prefix[-1].strip():
        prefix.pop()
    while suffix and not suffix[0].strip():
        suffix.pop(0)
    combined = [*prefix]
    if combined:
        combined.append("")
    combined.extend(body.splitlines())
    if suffix:
        combined.append("")
        combined.extend(suffix)
    return "\n".join(combined).rstrip() + "\n"


def _config_list_value(values: Sequence[str]) -> str:
    items = [str(value) for value in values if str(value)]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return "\n" + "\n".join(items)


def _write_mutmut_import_compat(mutation_dir: Path, *, repo: Path, source_path: str) -> Path:
    return write_mutmut_import_compat(
        mutation_dir,
        repo=repo,
        source_path=source_path,
        canonical_module=_canonical_module_name_from_source_path(repo, source_path),
    )


def _mutmut_process_env(repo: Path, *, source_path: str, import_compat_dir: Path) -> Dict[str, str]:
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath_parts = [import_compat_dir.as_posix(), repo.as_posix(), repo.parent.as_posix()]
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    return {
        "UTA_MUTMUT_IMPORT_COMPAT_DIR": import_compat_dir.resolve().as_posix(),
        "UTA_MUTMUT_TARGET_REL": _normalize_relpath(source_path),
        "UTA_MUTMUT_CANONICAL_MODULE": _canonical_module_name_from_source_path(repo, source_path),
        "UTA_MUTMUT_REPO_ROOT": repo.resolve().as_posix(),
        "UTA_PYTHON_MUTATION_PER_MUTANT_TIMEOUT_SECONDS": str(
            int(getattr(settings, "python_mutation_per_mutant_timeout_seconds", 0) or 0)
            or 120
        ),
        "PYTHONPATH": os.pathsep.join(pythonpath_parts),
    }


def _canonical_module_name_from_source_path(repo: Path, source_path: str) -> str:
    return resolve_python_module(repo, source_path).module_name


def _repo_from_source_file(source_file: Path, source_path: str) -> Path:
    parts = Path(_normalize_relpath(source_path)).parts
    if not parts:
        return source_file.parent
    try:
        return source_file.resolve().parents[len(parts) - 1]
    except IndexError:
        return source_file.parent


def _mutmut_run_command(
    mutmut_bin: str,
    source_path: str,
    config_overlay: bool,
    *,
    repo: Path,
    python_bin: str,
    test_paths: Sequence[str],
    import_compat_dir: Path,
    patch_file: Optional[Path] = None,
) -> List[str]:
    if config_overlay:
        return [mutmut_bin, "run"]
    command = [mutmut_bin, "run", "--paths-to-mutate", source_path]
    if patch_file is not None:
        command.extend(["--use-patch-file", str(patch_file)])
    tests_dir = _mutmut_tests_dir(test_paths)
    if tests_dir:
        command.extend(["--tests-dir", tests_dir])
    runner = _mutmut_runner_command(
        python_bin,
        test_paths,
        repo=repo,
        import_compat_dir=import_compat_dir,
        source_path=source_path,
    )
    if runner:
        command.extend(["--runner", runner])
    return command


def _mutmut_generate_metadata_command(
    python_bin: str,
    *,
    max_children: int,
    policy_path: Optional[Path] = None,
    mode: str = "metadata",
) -> List[str]:
    if policy_path is None:
        raise ValueError("mutmut 3 adapter requires a generation policy")
    return _shared_mutmut_adapter_command(
        python_bin,
        max_children=max_children,
        policy_path=policy_path,
        mode=mode,
    )


def _mutmut_runner_command(
    python_bin: str,
    test_paths: Sequence[str],
    *,
    repo: Path,
    import_compat_dir: Path,
    source_path: str,
) -> str:
    from uta_py_enforce.pytest_env import pytest_process_command

    parts = pytest_process_command(python_bin, ["-x", "--assert=plain", *map(str, test_paths)])
    pythonpath = (
        f"{shlex.quote(import_compat_dir.as_posix())}:"
        f"{shlex.quote(repo.as_posix())}:"
        f"{shlex.quote(repo.parent.as_posix())}:$PYTHONPATH"
    )
    timeout_seconds = int(getattr(settings, "python_mutation_per_mutant_timeout_seconds", 0) or 0)
    wrapper_path = _write_mutmut_timeout_runner(import_compat_dir) if timeout_seconds > 0 else None
    command = [
        "env",
        f"UTA_MUTMUT_TARGET_REL={_normalize_relpath(source_path)}",
        f"UTA_MUTMUT_CANONICAL_MODULE={_canonical_module_name_from_source_path(repo, source_path)}",
        f"UTA_MUTMUT_REPO_ROOT={repo.resolve().as_posix()}",
        f"PYTHONPATH={pythonpath}",
    ]
    if wrapper_path is not None:
        command.extend([python_bin, wrapper_path.as_posix(), str(timeout_seconds), "--", *parts])
    else:
        command.extend(parts)
    return " ".join(shlex.quote(part) for part in command)


def _write_mutmut_timeout_runner(import_compat_dir: Path) -> Path:
    wrapper_path = import_compat_dir / "mutmut_timeout_runner.py"
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    wrapper_path.write_text(
        "\n".join(
            [
                "import os",
                "import signal",
                "import subprocess",
                "import sys",
                "",
                "try:",
                "    import resource",
                "except Exception:",
                "    resource = None",
                "",
                "def main():",
                "    timeout = max(1, int(float(sys.argv[1])))",
                "    sep = sys.argv.index('--')",
                "    command = sys.argv[sep + 1:]",
                "    def limit_cpu():",
                "        if resource is not None and hasattr(resource, 'RLIMIT_CPU'):",
                "            try:",
                "                resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout + 1))",
                "            except Exception:",
                "                pass",
                "    kwargs = {}",
                "    if os.name == 'posix':",
                "        kwargs['start_new_session'] = True",
                "        kwargs['preexec_fn'] = limit_cpu",
                "    proc = subprocess.Popen(command, **kwargs)",
                "    try:",
                "        return proc.wait(timeout=timeout)",
                "    except subprocess.TimeoutExpired:",
                "        if os.name == 'posix':",
                "            try:",
                "                os.killpg(proc.pid, signal.SIGKILL)",
                "            except Exception:",
                "                proc.kill()",
                "        else:",
                "            proc.kill()",
                "        return 124",
                "",
                "if __name__ == '__main__':",
                "    raise SystemExit(main())",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return wrapper_path


def _mutmut_tests_dir(test_paths: Sequence[str]) -> str:
    if not test_paths:
        return "."
    parent = Path(str(test_paths[0])).parent
    return "." if str(parent) in {"", "."} else str(parent)


def _write_mutation_patch_file(
    mutation_dir: Path,
    *,
    source_file: Path,
    source_path: str,
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    enabled: bool,
) -> Optional[Path]:
    if not enabled:
        return None
    normalized_changed_lines = _normalize_changed_lines(changed_lines)
    if normalized_changed_lines is None:
        return None
    normalized_source = _normalize_relpath(source_path)
    target_lines = sorted(normalized_changed_lines.get(normalized_source, set()))
    if not target_lines or not source_file.exists():
        return None
    source_lines = source_file.read_text(encoding="utf-8").splitlines()
    hunks = [
        f"@@ -{line_number - 1},0 +{line_number},1 @@\n+{source_lines[line_number - 1] if line_number <= len(source_lines) else ''}\n"
        for line_number in target_lines
    ]
    patch_text = (
        f"--- {normalized_source}\n"
        f"+++ {normalized_source}\n"
        + "".join(hunks)
    )
    patch_path = mutation_dir / "changed-lines.patch"
    patch_path.write_text(patch_text, encoding="utf-8")
    return patch_path


def _mutmut_major_version(version_output: str) -> int:
    match = re.search(r"\b(\d+)\.\d+(?:\.\d+)?\b", str(version_output or ""))
    return int(match.group(1)) if match else 0


def _maskable_pragma_lines(source: str) -> set[int]:
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        return {token.end[0] for token in tokens if token.type == tokenize.NEWLINE}
    except tokenize.TokenError:
        return set()


def _append_no_mutate_pragma(line: str) -> str:
    newline = ""
    body = line
    if line.endswith("\r\n"):
        body, newline = line[:-2], "\r\n"
    elif line.endswith("\n"):
        body, newline = line[:-1], "\n"
    return f"{body}  # pragma: no mutate{newline}"


def _normalize_changed_lines(
    changed_lines: Optional[Mapping[str, Iterable[int]]],
) -> Optional[Dict[str, set[int]]]:
    if changed_lines is None:
        return None
    normalized: Dict[str, set[int]] = {}
    for path, lines in changed_lines.items():
        normalized_path = _normalize_relpath(str(path))
        normalized[normalized_path] = {int(line) for line in lines or [] if int(line) > 0}
    return normalized


def _changed_line_payload(
    changed_lines: Optional[Mapping[str, Iterable[int]]],
    source_paths: Iterable[str],
) -> Dict[str, List[int]]:
    if changed_lines is None:
        return {}
    normalized_sources = {_normalize_relpath(path) for path in source_paths if path}
    return {
        path: sorted(int(line) for line in lines)
        for path, lines in changed_lines.items()
        if not normalized_sources or path in normalized_sources
    }


def _normalize_relpath(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _decode_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_expected_mutmut_exit(exit_code: int) -> bool:
    # Mutmut versions differ on non-zero gate/result exits, but signal-style
    # and shell failure codes still indicate the backend did not complete.
    return int(exit_code) in {0, 1, 2}


def _cleanup_mutation_state(repo: Path) -> None:
    for relative in (".mutmut-cache", ".coverage", "mutants"):
        path = repo / relative
        if path.is_dir():
            shutil.rmtree(str(path), ignore_errors=True)
        elif path.exists():
            path.unlink()


def _clean_generated_mutants(repo: Path) -> None:
    """Between-batch cleanup for batched generation (T3 / ADR-002).

    Removes generated source files under ``mutants/`` so the next batch regenerates a fresh,
    smaller module, while preserving coverage data and mutmut's per-test stats cache at
    ``mutants/mutmut-stats.json``. The source is constant across batches, so re-running the
    suite for stats every batch would be wasted work. The full ``_cleanup_mutation_state`` is
    still used for the final teardown.
    """
    path = repo / "mutants"
    if not path.exists():
        return
    if not path.is_dir():
        path.unlink()
        return
    for child in path.iterdir():
        if child.name == "mutmut-stats.json":
            continue
        if child.is_dir():
            shutil.rmtree(str(child), ignore_errors=True)
        else:
            child.unlink(missing_ok=True)

__all__ = [
    "_MutmutConfigOverlay",
    "_append_no_mutate_pragma",
    "_canonical_module_name_from_source_path",
    "_changed_line_payload",
    "_clean_generated_mutants",
    "_cleanup_mutation_state",
    "_decode_output",
    "_is_expected_mutmut_exit",
    "_maskable_pragma_lines",
    "_mutmut_config_overlay",
    "_mutmut_generate_metadata_command",
    "_mutmut_major_version",
    "_mutmut_process_env",
    "_mutmut_run_command",
    "_mutmut_runner_command",
    "_mutmut_tests_dir",
    "_normalize_changed_lines",
    "_normalize_relpath",
    "_repo_from_source_file",
    "_write_mutation_patch_file",
    "_write_mutmut_import_compat",
    "_write_mutmut_timeout_runner",
]
