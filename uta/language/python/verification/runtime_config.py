"""Runtime configuration resolution and interpreter/tool selection.

One place decides what the verifier will run with: the layered
default/repo-config/env/CLI precedence behind ``PythonRuntimeConfig``, the
dependency fingerprints and cache key derived from it, and the lane-dependent
choice of python and mutmut binaries. It reads configuration and the
filesystem but executes nothing, so it stays testable without subprocesses.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Dict, Mapping, Optional

from uta.language.python.verification.models import PythonRuntimeConfig


_REPO_MUTMUT_CONFIG_KEYS = {"mutmut_bin", "python2_mutmut_bin"}


def resolve_python_runtime_config(
    repo_path: Path,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> PythonRuntimeConfig:
    repo = Path(repo_path)
    env = os.environ if environ is None else environ
    values: Dict[str, Any] = {
        "python_bin": "python3",
        "python2_bin": None,
        "mutmut_bin": "mutmut",
        "python2_mutmut_bin": None,
        "setup_command": (),
        "dependency_overlay_enabled": True,
        "environment_profile": "default",
        "timeout_seconds": 1800,
        "artifact_dir": ".uta_cache/python",
    }
    sources = {field_name: "default" for field_name in values}

    for config_path in (".uta/python-enforce.toml", ".uta-test-enforcement.toml"):
        parsed = _read_simple_toml(repo / config_path)
        for key, value in parsed.items():
            if key not in values:
                continue
            if key in _REPO_MUTMUT_CONFIG_KEYS:
                continue
            values[key] = _coerce_config_value(key, value)
            sources[key] = config_path

    env_map = {
        "python_bin": "UTA_PYTHON_BIN",
        "python2_bin": "UTA_PYTHON2_BIN",
        "mutmut_bin": "UTA_PYTHON_MUTMUT_BIN",
        "python2_mutmut_bin": "UTA_PYTHON2_MUTMUT_BIN",
        "setup_command": "UTA_PYTHON_SETUP_COMMAND",
        "dependency_overlay_enabled": "UTA_PYTHON_DEPENDENCY_OVERLAY_ENABLED",
        "environment_profile": "UTA_PYTHON_ENVIRONMENT_PROFILE",
        "timeout_seconds": "UTA_PYTHON_GATE_TIMEOUT_SECONDS",
        "artifact_dir": "UTA_PYTHON_ARTIFACT_DIR",
    }
    for key, env_name in env_map.items():
        if env_name not in env:
            continue
        values[key] = _coerce_config_value(key, env[env_name])
        sources[key] = f"env:{env_name}"

    for key, value in (overrides or {}).items():
        if key not in values or value is None:
            continue
        values[key] = _coerce_config_value(key, value)
        sources[key] = "cli"

    fingerprints = _dependency_fingerprints(repo)
    cache_key = _python_cache_key(values, fingerprints)
    return PythonRuntimeConfig(
        python_bin=str(values["python_bin"]),
        python2_bin=_optional_str(values["python2_bin"]),
        mutmut_bin=str(values["mutmut_bin"]),
        python2_mutmut_bin=_optional_str(values["python2_mutmut_bin"]),
        setup_command=tuple(values["setup_command"] or ()),
        dependency_overlay_enabled=bool(values["dependency_overlay_enabled"]),
        environment_profile=str(values["environment_profile"] or "default"),
        timeout_seconds=int(values["timeout_seconds"]),
        artifact_dir=str(values["artifact_dir"]),
        dependency_fingerprints=fingerprints,
        cache_key=cache_key,
        config_sources=sources,
    )


def _runtime_lane(syntax_version: str) -> str:
    return "mutmut-legacy-py2" if str(syntax_version or "").lower().startswith("python2") else "mutmut-modern"


def _python_bin(config: PythonRuntimeConfig, lane: str) -> str:
    if lane == "mutmut-legacy-py2":
        return config.python2_bin or "python2"
    return config.python_bin


def _python_runtime_fallback_bin(config: PythonRuntimeConfig, lane: str, current_python_bin: str) -> Optional[str]:
    if lane == "mutmut-legacy-py2":
        return None
    if (config.config_sources or {}).get("python_bin") != "default":
        return None
    candidates = [
        os.environ.get("UTA_SERVICE_PYTHON_BIN"),
        sys.executable,
    ]
    current = str(current_python_bin or "")
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and value != current:
            return value
    return None


def _mutmut_bin(config: PythonRuntimeConfig, lane: str, python_bin: str) -> str:
    if lane == "mutmut-legacy-py2":
        return config.python2_mutmut_bin or "mutmut"
    if (config.config_sources or {}).get("mutmut_bin") == "default":
        sibling = Path(str(python_bin)).expanduser().parent / "mutmut"
        if sibling.exists():
            return sibling.as_posix()
    return config.mutmut_bin


def _source_path_from_target(target_id: str) -> str:
    raw = str(target_id or "")
    if raw.startswith("pyfile:"):
        return raw[len("pyfile:") :]
    if raw.startswith("pysymbol:"):
        raw = raw[len("pysymbol:") :]
    return raw.split("::", 1)[0]


def _read_simple_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    values: Dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _parse_simple_toml_value(value.strip())
    return values


def _parse_simple_toml_value(value: str) -> Any:
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        items = []
        for item in value[1:-1].split(","):
            parsed = _parse_simple_toml_value(item.strip())
            if parsed != "":
                items.append(parsed)
        return items
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _coerce_config_value(key: str, value: Any) -> Any:
    if key == "setup_command":
        if isinstance(value, str):
            return tuple(shlex.split(value))
        return tuple(str(part) for part in (value or ()))
    if key == "timeout_seconds":
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1800
    if key == "dependency_overlay_enabled":
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)
    return value


def _dependency_fingerprints(repo: Path) -> Dict[str, str]:
    candidates = (
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "noxfile.py",
        ".uta/python-enforce.toml",
        ".uta-test-enforcement.toml",
    )
    fingerprints: Dict[str, str] = {}
    for relative in candidates:
        path = repo / relative
        if path.is_file():
            fingerprints[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fingerprints


def _python_cache_key(values: Mapping[str, Any], fingerprints: Mapping[str, str]) -> str:
    payload = {
        "python_bin": values.get("python_bin"),
        "python2_bin": values.get("python2_bin"),
        "mutmut_bin": values.get("mutmut_bin"),
        "python2_mutmut_bin": values.get("python2_mutmut_bin"),
        "setup_command": list(values.get("setup_command") or ()),
        "dependency_overlay_enabled": bool(values.get("dependency_overlay_enabled", True)),
        "environment_profile": values.get("environment_profile"),
        "dependency_fingerprints": dict(sorted(fingerprints.items())),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"python-env:{digest}"


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None
