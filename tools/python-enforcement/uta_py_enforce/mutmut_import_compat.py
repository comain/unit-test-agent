"""Generate the sitecustomize shim used by mutmut subprocesses."""

from __future__ import annotations

from pathlib import Path

from uta_py_enforce.mutmut_import_hooks import (
    MUTANTS_PACKAGE_ALIAS_FINDER_SOURCE,
    TARGET_IMPORT_FINDER_SOURCE,
)


def _normalize_relpath(path: str) -> str:
    normalized = str(path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def write_mutmut_import_compat(
    mutation_dir: Path,
    *,
    repo: Path,
    source_path: str,
    canonical_module: str,
) -> Path:
    """Write the generated import shim and return its PYTHONPATH directory."""
    compat_dir = mutation_dir / "import_compat"
    compat_dir.mkdir(parents=True, exist_ok=True)
    source = mutmut_import_compat_source(
        repo,
        source_path,
        canonical_module=canonical_module,
    )
    (compat_dir / "sitecustomize.py").write_text(source, encoding="utf-8")
    return compat_dir


def mutmut_import_compat_source(repo: Path, source_path: str, *, canonical_module: str) -> str:
    target_rel = _normalize_relpath(source_path)
    repo_root = repo.resolve().as_posix()
    return f'''"""UTA mutmut import compatibility shim.

Loaded through PYTHONPATH during mutmut pytest runs. It normalizes file-based
loads of the target source file to the canonical module name so mutmut can map
test execution to generated mutant trampoline names.
"""
from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import ast
import os
from pathlib import Path
import runpy
import shutil
import sys

TARGET_REL = os.environ.get("UTA_MUTMUT_TARGET_REL") or {target_rel!r}
CANONICAL_MODULE = os.environ.get("UTA_MUTMUT_CANONICAL_MODULE") or {canonical_module!r}
REPO_ROOT = Path(os.environ.get("UTA_MUTMUT_REPO_ROOT") or {repo_root!r})
_TARGET_CANONICAL_REL = TARGET_REL.replace("\\\\", "/").lstrip("/")
_TARGET_ALIASES = tuple(
    alias
    for alias in dict.fromkeys(
        [_TARGET_CANONICAL_REL, _TARGET_CANONICAL_REL[4:] if _TARGET_CANONICAL_REL.startswith("src/") else ""]
    )
    if alias
)
_TARGET_REAL_PATHS = {{
    (REPO_ROOT / alias).resolve().as_posix()
    for alias in _TARGET_ALIASES
}}
_TARGET_REAL_PATHS.update(
    (REPO_ROOT / "mutants" / alias).resolve().as_posix()
    for alias in _TARGET_ALIASES
)

# Some target-specific tests inspect generated mutmut source through ast.parse.
# A compatibility helper may then materialize the trampoline AST in place. The
# generated source and active mutant are immutable within one pytest phase, so
# retain that single tree and let repeated inspections reuse the materialized
# result. The one-entry cache is replaced at every mutant boundary and cannot
# accumulate one large AST per mutant.
_UTA_ORIGINAL_AST_PARSE = ast.parse
_UTA_MUTANT_AST_CACHE_KEY = None
_UTA_MUTANT_AST_CACHE_TREE = None


def _uta_cached_mutant_ast_parse(source, *args, **kwargs):
    global _UTA_MUTANT_AST_CACHE_KEY, _UTA_MUTANT_AST_CACHE_TREE
    mutant = os.environ.get("MUTANT_UNDER_TEST") or ""
    marker = b"_mutmut_trampoline" if isinstance(source, (bytes, bytearray)) else "_mutmut_trampoline"
    if not mutant or not isinstance(source, (str, bytes, bytearray)) or marker not in source:
        return _UTA_ORIGINAL_AST_PARSE(source, *args, **kwargs)
    key = (mutant, source, repr(args), repr(sorted(kwargs.items())))
    if key != _UTA_MUTANT_AST_CACHE_KEY:
        _UTA_MUTANT_AST_CACHE_KEY = key
        _UTA_MUTANT_AST_CACHE_TREE = _UTA_ORIGINAL_AST_PARSE(source, *args, **kwargs)
    return _UTA_MUTANT_AST_CACHE_TREE


ast.parse = _uta_cached_mutant_ast_parse


def _ensure_mutant_resource_mirrors():
    """Make repo-root resource dirs visible to modules imported from mutants/.

    Some legacy projects compute config paths from ``__file__`` at import time.
    When mutmut imports a copied module from ``repo/mutants/...``, those lookups
    shift to ``repo/mutants/resources.*``. Mirror only top-level resource dirs so
    mutated imports keep the same runtime config without changing production code.
    """
    mutants_root = REPO_ROOT / "mutants"
    try:
        mutants_root.mkdir(parents=True, exist_ok=True)
        children = list(REPO_ROOT.iterdir())
    except Exception:
        return
    for real_path in children:
        try:
            if not real_path.is_dir():
                continue
            name = real_path.name
            if name == "mutants" or not name.startswith("resources"):
                continue
            mirror_path = mutants_root / name
            if mirror_path.exists():
                continue
            if mirror_path.is_symlink():
                mirror_path.unlink(missing_ok=True)
            try:
                mirror_path.symlink_to(real_path, target_is_directory=True)
            except Exception:
                if not mirror_path.exists():
                    shutil.copytree(real_path, mirror_path, dirs_exist_ok=True)
        except Exception:
            continue


_ensure_mutant_resource_mirrors()


def _is_target_path(path) -> bool:
    if not path:
        return False
    value = str(path).replace("\\\\", "/")
    if value in _TARGET_ALIASES:
        return True
    try:
        resolved = Path(path).resolve().as_posix()
    except Exception:
        return False
    # A basename/suffix comparison aliases unrelated modules with common names
    # such as ``__main__.py``. In particular, it renamed mutmut.__main__ to the
    # repository target and made Mutmut parse UTA adapter argv as CLI commands.
    return resolved in _TARGET_REAL_PATHS


def _should_load_mutant_copy() -> bool:
    mutant_under_test = os.environ.get("MUTANT_UNDER_TEST") or ""
    return "__mutmut_" in mutant_under_test


def _mutant_path_for(path):
    if not _should_load_mutant_copy() or not _is_target_path(path):
        return path
    cwd = Path.cwd()
    target_rel = _TARGET_CANONICAL_REL
    stripped_src_rel = target_rel[4:] if target_rel.startswith("src/") else ""
    if cwd.name == "mutants":
        candidates = [cwd / target_rel]
        if stripped_src_rel:
            candidates.append(cwd / stripped_src_rel)
        candidates.append(cwd.parent / "mutants" / target_rel)
        if stripped_src_rel:
            candidates.append(cwd.parent / "mutants" / stripped_src_rel)
    else:
        candidates = [cwd / "mutants" / target_rel]
        if stripped_src_rel:
            candidates.append(cwd / "mutants" / stripped_src_rel)
        candidates.append(cwd / target_rel)
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate.as_posix()
        except Exception:
            pass
    return path


def _canonical_name(name, path):
    return CANONICAL_MODULE if CANONICAL_MODULE and _is_target_path(path) else name


def _bind_target_module_aliases(module, *, canonical_name, alias_name=None):
    if not canonical_name:
        return module
    module.__name__ = canonical_name
    sys.modules[canonical_name] = module
    if alias_name and alias_name != canonical_name:
        sys.modules[alias_name] = module
    return module


def _extend_mutant_package_to_real_siblings(module):
    paths = getattr(module, "__path__", None)
    module_file = getattr(module, "__file__", None)
    if paths is None or not module_file:
        return
    try:
        package_dir = Path(module_file).resolve().parent
        mutants_root = (REPO_ROOT / "mutants").resolve()
        relative = package_dir.relative_to(mutants_root)
    except Exception:
        return
    if str(relative) == ".":
        return
    real_package_dir = REPO_ROOT / relative
    if not real_package_dir.exists():
        return
    normalized = []
    for item in list(paths) + [real_package_dir.as_posix()]:
        value = str(item)
        if value and value not in normalized:
            normalized.append(value)
    module.__path__ = normalized


_orig_spec_from_file_location = importlib.util.spec_from_file_location

{TARGET_IMPORT_FINDER_SOURCE}


def _uta_spec_from_file_location(name, location, *args, **kwargs):
    canonical_name = _canonical_name(name, location)
    redirected_location = _mutant_path_for(location)
    spec = _orig_spec_from_file_location(canonical_name, redirected_location, *args, **kwargs)
    if spec is not None and canonical_name != name:
        spec._uta_alias_name = name
        spec._uta_canonical_name = canonical_name
        if spec.loader is not None:
            spec.loader._uta_alias_name = name
            spec.loader._uta_canonical_name = canonical_name
    return spec


importlib.util.spec_from_file_location = _uta_spec_from_file_location

_orig_module_from_spec = importlib.util.module_from_spec


def _uta_module_from_spec(spec):
    module = _orig_module_from_spec(spec)
    canonical_name = getattr(spec, "_uta_canonical_name", None)
    if canonical_name:
        _bind_target_module_aliases(
            module,
            canonical_name=canonical_name,
            alias_name=getattr(spec, "_uta_alias_name", None),
        )
    return module


importlib.util.module_from_spec = _uta_module_from_spec

_orig_source_file_loader_init = importlib.machinery.SourceFileLoader.__init__


def _uta_source_file_loader_init(self, name, path):
    canonical_name = _canonical_name(name, path)
    redirected_path = _mutant_path_for(path)
    if canonical_name != name:
        self._uta_alias_name = name
        self._uta_canonical_name = canonical_name
    return _orig_source_file_loader_init(self, canonical_name, redirected_path)


importlib.machinery.SourceFileLoader.__init__ = _uta_source_file_loader_init

_orig_source_file_loader_exec_module = importlib.machinery.SourceFileLoader.exec_module


def _uta_source_file_loader_exec_module(self, module):
    canonical_name = getattr(self, "_uta_canonical_name", None)
    alias_name = getattr(self, "_uta_alias_name", None)
    if canonical_name:
        _bind_target_module_aliases(module, canonical_name=canonical_name, alias_name=alias_name)
    result = _orig_source_file_loader_exec_module(self, module)
    _extend_mutant_package_to_real_siblings(module)
    if canonical_name:
        _bind_target_module_aliases(module, canonical_name=canonical_name, alias_name=alias_name)
    return result


importlib.machinery.SourceFileLoader.exec_module = _uta_source_file_loader_exec_module

try:
    import imp
except Exception:
    imp = None

if imp is not None:
    _orig_load_source = imp.load_source

    def _uta_load_source(name, pathname, file=None):
        return _orig_load_source(_canonical_name(name, pathname), _mutant_path_for(pathname), file)

    imp.load_source = _uta_load_source

_orig_run_path = runpy.run_path


def _uta_run_path(path_name, init_globals=None, run_name=None):
    if _is_target_path(path_name) and CANONICAL_MODULE:
        redirected_path = _mutant_path_for(path_name)
        if redirected_path != path_name:
            return _orig_run_path(redirected_path, init_globals=init_globals, run_name=CANONICAL_MODULE)
        module = importlib.import_module(CANONICAL_MODULE)
        return dict(module.__dict__)
    return _orig_run_path(path_name, init_globals=init_globals, run_name=run_name)


runpy.run_path = _uta_run_path


def _install_pytest_mutants_package_alias():
    repo = REPO_ROOT
    cwd = repo / "mutants"
    package_name = repo.name
    if not package_name.isidentifier():
        return
    repo_init = repo / "__init__.py"
    mutants_init = cwd / "__init__.py"
    if not repo_init.exists() or not mutants_init.exists():
        return
    if package_name not in sys.modules:
        package = importlib.util.module_from_spec(
            importlib.machinery.ModuleSpec(package_name, loader=None, is_package=True)
        )
        package.__file__ = repo_init.as_posix()
        package.__path__ = [repo.as_posix()]
        sys.modules[package_name] = package
    mutants_name = package_name + ".mutants"
    if mutants_name not in sys.modules:
        mutants_package = importlib.util.module_from_spec(
            importlib.machinery.ModuleSpec(mutants_name, loader=None, is_package=True)
        )
        mutants_package.__file__ = mutants_init.as_posix()
        mutants_package.__path__ = [cwd.as_posix()]
        sys.modules[mutants_name] = mutants_package
        setattr(sys.modules[package_name], "mutants", mutants_package)


{MUTANTS_PACKAGE_ALIAS_FINDER_SOURCE}
_install_pytest_mutants_package_alias()


def _install_mutmut_src_trampoline_normalizer():
    try:
        import mutmut.__main__ as mutmut_main
    except Exception:
        return
    original = getattr(mutmut_main, "record_trampoline_hit", None)
    if not callable(original) or getattr(original, "_uta_src_normalized", False):
        return

    def _uta_record_trampoline_hit(name):
        if isinstance(name, str) and name.startswith("src."):
            name = name[4:]
        return original(name)

    _uta_record_trampoline_hit._uta_src_normalized = True
    mutmut_main.record_trampoline_hit = _uta_record_trampoline_hit


_install_mutmut_src_trampoline_normalizer()
'''

__all__ = ["mutmut_import_compat_source", "write_mutmut_import_compat"]
