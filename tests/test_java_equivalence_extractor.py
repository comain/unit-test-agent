"""Java survivors for the equivalent-mutant review come only from the gate's own PIT run."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from uta.language.java.equivalence import java_scoring_survivors
from uta.language.java.generation.mutation_context import pit_compat_reports

SOURCE = "src/main/java/example/Target.java"
BASE = "\n".join(f"    int line{i}() {{ return {i}; }}" for i in range(1, 12))
CHANGED = BASE.replace("return 5;", "return 50;").replace("return 6;", "return 60;")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "src/main/java/example").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / SOURCE).write_text(f"class Target {{\n{BASE}\n}}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / SOURCE).write_text(f"class Target {{\n{CHANGED}\n}}\n")
    _git(repo, "commit", "-qam", "change")
    return repo, base


def _mutation(status: str, line: int, method: str = "line", mutator: str = "RETURN_VALS", index: int = 0) -> str:
    return (
        f'<mutation detected="{str(status == "KILLED").lower()}" status="{status}">'
        "<sourceFile>Target.java</sourceFile><mutatedClass>example.Target</mutatedClass>"
        f"<mutatedMethod>{method}{line}</mutatedMethod><methodDescription>()I</methodDescription>"
        f"<lineNumber>{line}</lineNumber><mutator>{mutator}</mutator>"
        f"<indexes><index>{index}</index></indexes><blocks><block>0</block></blocks>"
        "<description>replaced int return with 0</description></mutation>"
    )


def _gate(repo: Path, rows: list[str], detected: int, total: int, *, nonce: str = "n1", source_line: str = "diff") -> dict:
    invocation = repo / ".uta_cache/pit-compat" / nonce
    (invocation / "module-a").mkdir(parents=True, exist_ok=True)
    (invocation / "module-a/mutations.xml").write_text("<mutations>" + "".join(rows) + "</mutations>")
    rate = detected / total * 100 if total else 0.0
    survived = total - detected
    score_line = (
        (
            f"[test-enforcer] diff mutation score {rate:.2f}% passed for example.Target "
            f"({detected}/{total} detected; {survived} survived; 0 no coverage excluded)"
            if detected == total
            else f"[test-enforcer] diff mutation score {rate:.2f}% for example.Target "
            f"({detected}/{total} detected; {survived} survived; 0 no coverage excluded) "
            "is below required 100.00%"
        )
        if source_line == "diff"
        else f"Generated {total} mutations Killed {detected} ({rate:.0f}%)"
    )
    return {
        "status": "failed",
        "passed": False,
        "language": "java",
        "command": ["mvn", f"-Duta.pit.compat.evidence={invocation}/completion.xml", "verify"],
        "stdout": score_line,
        "stderr": "",
        "evidence": {"baseRef": ""},
    }


def _fixture(tmp_path: Path):
    repo, base = _repo(tmp_path)
    # Lines 6 and 7 of the file are the two changed methods (line5/line6).
    rows = [
        _mutation("SURVIVED", 6),
        _mutation("KILLED", 6, mutator="INCREMENTS"),
        _mutation("KILLED", 7),
        _mutation("KILLED", 2),  # unchanged line: outside the diff scope
    ]
    return repo, base, rows


def test_diff_scoped_survivors_match_gate_totals(tmp_path):
    repo, base, rows = _fixture(tmp_path)
    gate = _gate(repo, rows, detected=2, total=3)

    survivors = java_scoring_survivors(gate, repo, base_ref=base)

    assert survivors is not None
    assert survivors.language == "java"
    assert [(m.source_path, m.line, m.operator) for m in survivors.mutants] == [(SOURCE, 6, "RETURN_VALS")]
    assert survivors.unreviewed_scoring_failures == 0
    digest = hashlib.sha256((repo / SOURCE).read_bytes()).hexdigest()
    assert survivors.source_fingerprints == {SOURCE: digest}
    again = java_scoring_survivors(gate, repo, base_ref=base)
    assert again.keys == survivors.keys


def test_no_coverage_and_timeouts_that_lower_the_score_are_unreviewed(tmp_path):
    repo, base, rows = _fixture(tmp_path)
    rows = rows + [_mutation("NO_COVERAGE", 7, mutator="MATH")]
    gate = _gate(repo, rows, detected=2, total=4)

    survivors = java_scoring_survivors(gate, repo, base_ref=base)

    assert survivors is not None
    assert survivors.unreviewed_scoring_failures == 1


def test_mismatched_totals_are_unproven(tmp_path):
    repo, base, rows = _fixture(tmp_path)
    assert java_scoring_survivors(_gate(repo, rows, detected=2, total=5), repo, base_ref=base) is None
    assert java_scoring_survivors(_gate(repo, rows, detected=1, total=3), repo, base_ref=base) is None


def test_non_diff_score_source_is_unproven(tmp_path):
    repo, base, rows = _fixture(tmp_path)
    gate = _gate(repo, rows, detected=2, total=3, source_line="pit")
    assert java_scoring_survivors(gate, repo, base_ref=base) is None


def test_missing_or_escaped_compat_evidence_is_unproven(tmp_path):
    repo, base, rows = _fixture(tmp_path)
    gate = _gate(repo, rows, detected=2, total=3)
    assert java_scoring_survivors({**gate, "command": ["mvn", "verify"]}, repo, base_ref=base) is None
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    escaped = {**gate, "command": ["mvn", f"-Duta.pit.compat.evidence={outside}/completion.xml"]}
    assert java_scoring_survivors(escaped, repo, base_ref=base) is None


def test_unresolvable_source_file_is_unproven(tmp_path):
    repo, base, rows = _fixture(tmp_path)
    ghost = [row.replace("example.Target", "ghost.Missing") for row in rows]
    assert java_scoring_survivors(_gate(repo, ghost, detected=2, total=3), repo, base_ref=base) is None


def test_compat_reports_only_reads_the_named_invocation(tmp_path):
    repo, _base, rows = _fixture(tmp_path)
    gate = _gate(repo, rows, detected=2, total=3, nonce="current")
    stale = repo / ".uta_cache/pit-compat/stale"
    stale.mkdir(parents=True)
    (stale / "mutations.xml").write_text("<mutations/>")

    reports = pit_compat_reports(str(repo), gate)

    assert reports == [repo.resolve() / ".uta_cache/pit-compat/current/module-a/mutations.xml"]
