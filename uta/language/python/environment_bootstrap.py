"""Build one cached Python test venv declared by a trusted UTA recipe."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Sequence


def _fingerprint(python: str, packages: Sequence[str]) -> str:
    payload = {"python": python, "packages": list(packages)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def ensure_environment(*, venv: Path, python: str, packages: Sequence[str]) -> None:
    venv = Path(venv).expanduser()
    if not venv.is_absolute() or len(venv.parts) < 4:
        raise ValueError("managed Python environment path must be a specific absolute path")
    venv.parent.mkdir(parents=True, exist_ok=True)
    lock_path = venv.with_suffix(".lock")
    marker = venv / ".uta-environment.json"
    expected = _fingerprint(python, packages)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        venv_python = venv / "bin" / "python"
        venv_mutmut = venv / "bin" / "mutmut"
        if marker.is_file() and venv_python.is_file() and venv_mutmut.is_file():
            try:
                if json.loads(marker.read_text(encoding="utf-8")).get("fingerprint") == expected:
                    return
            except (OSError, json.JSONDecodeError):
                pass

        subprocess.run([python, "-m", "venv", "--clear", str(venv)], check=True)
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", *packages],
            check=True,
        )
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"fingerprint": expected, "python": python, "packages": list(packages)}, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(marker)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--venv", required=True, type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--package", action="append", required=True, dest="packages")
    args = parser.parse_args(argv)
    ensure_environment(venv=args.venv, python=args.python, packages=args.packages)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
