"""`--json-output` must emit JSON, not JSON wearing terminal colours.

Rich's `print_json` styles its output when it believes it is attached to a
terminal. That belief depends on the environment, so the same command produced
parseable JSON under one test ordering and ANSI-wrapped JSON under another --
which is how this stayed latent, passing in a full-suite run and failing in
isolation.

The user-visible failure is worse than the test one: `uta query-index
--json-output | jq` gets escape codes and dies. A machine-readable flag that is
only machine-readable when stdout is not a terminal is not machine-readable.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from uta.app.cli import main


def _plain(text: str) -> None:
    assert "\x1b[" not in text, f"ANSI escapes in machine-readable output: {text[:120]!r}"


@pytest.mark.parametrize(
    "argv",
    [
        ["query-index", "--class-fqn", "com.example.Missing", "--json-output"],
        ["enforce", "--language", "java", "--dry-run", "--json-output"],
    ],
)
def test_json_output_parses_and_carries_no_escape_codes(tmp_path, argv):
    result = CliRunner().invoke(main, [*argv[:1], "--repo", str(tmp_path), *argv[1:]])

    assert result.exit_code == 0, result.output
    _plain(result.output)
    json.loads(result.output)


def test_no_command_prints_machine_output_through_rich():
    """The rule, checked at the source rather than one command at a time."""
    import ast
    from pathlib import Path

    offenders = []
    for path in sorted((Path(__file__).resolve().parents[1] / "uta" / "app").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "print_json"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "print_json colourises when it thinks it has a terminal; "
        f"use click.echo(json.dumps(...)) for machine output: {offenders}"
    )
