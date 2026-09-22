"""Command boundary for the shared mutmut 3 generation adapter."""

from __future__ import annotations

from pathlib import Path


def adapter_command(
    python_bin: str,
    *,
    max_children: int,
    policy_path: Path,
    mode: str,
) -> list[str]:
    script = Path(__file__).with_name("mutmut_adapter_runtime.py").resolve()
    bootstrap = (
        f"import sys\nsys.path.insert(0, {str(script.parent.parent)!r})\n"
        f"exec(compile({script.read_text(encoding='utf-8')!r}, {str(script)!r}, 'exec'))"
    )
    return [
        python_bin,
        "-c",
        bootstrap,
        str(max(1, int(max_children or 1))),
        str(mode),
        str(policy_path),
    ]
