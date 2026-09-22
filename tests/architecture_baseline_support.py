"""Shared normalisation and fixture plumbing for the architecture baselines.

The architecture boundary cleanup moves a large amount of code between
packages. These baselines exist so a later slice can prove it moved code
without changing behaviour, and they are therefore captured at *public,
stable boundaries only*: CLI invocations and their output, evidence JSON,
marker text, SQL observed through public ``TaskDB``/``TaskManager`` methods,
and spawn counts. No baseline reads a private function, because a baseline
that names a private function stops existing the moment that function moves —
which is the entire point of the programme it is meant to protect.

Every fixture goes through :func:`normalize` before it is written or
compared. A baseline that embeds ``/Users/<someone>`` fails on any other
machine and is worse than no baseline at all, so normalisation is one helper
used everywhere rather than per-test ad-hoc scrubbing.

Set ``UTA_BASELINE_REFRESH=1`` to rewrite the fixtures from current
behaviour. Do that only when the change to behaviour is intended and
reviewed; the port-defect rule says a baseline that only passes after
production behaviour changed is a wrong baseline, not a wrong product.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "architecture_baseline"
REPO_ROOT = Path(__file__).resolve().parents[1]

REFRESH_ENV = "UTA_BASELINE_REFRESH"

#: Keys whose values are wall-clock or machine dependent and therefore carry
#: no contract. They are replaced by a constant so their *presence* is still
#: part of the baseline while their value is not.
_VOLATILE_KEYS = {
    "generatedAt",
    "generated_at",
    "evidenceId",
    "evidence_id",
    "baseCommit",
    "headCommit",
    "base_commit",
    "head_commit",
    "duration_seconds",
    "durationSeconds",
    "elapsed_seconds",
    "elapsedSeconds",
    "started_at",
    "finished_at",
    "ts",
    "created_at",
    "updated_at",
}

#: Keys whose values are unbounded captured process output. Freezing them
#: would freeze the version banner of whatever pytest/coverage the machine
#: happens to have, so only their emptiness is kept.
_OUTPUT_KEYS = {"stdout", "stderr", "output"}

_SECRET_KEY_HINT = re.compile(
    r"(token|secret|password|passwd|api_?key|credential|cookie|authorization)",
    re.IGNORECASE,
)

_ABS_PATH = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9._+-]+/)*[A-Za-z0-9._+-]*")
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
_HEX40 = re.compile(r"\b[0-9a-f]{40}\b")
_HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
_HEX24 = re.compile(r"\b[0-9a-f]{24}\b")
_HEX16 = re.compile(r"\b[0-9a-f]{16}\b")

#: Substrings that must never appear in a committed baseline artifact.
FORBIDDEN_SUBSTRINGS = ("/Users/", "/home/", "/private/tmp", "/var/folders", "/tmp/")


def path_replacements(**extra: Any) -> "list[tuple[str, str]]":
    """Longest-first replacements for the machine-local roots we know about.

    ``extra`` maps a placeholder name to a path, e.g. ``fixture_repo=tmp``.
    """
    pairs: list[tuple[str, str]] = []
    for name, value in extra.items():
        if value is None:
            continue
        pairs.append((str(Path(str(value))), f"<{name.replace('_', '-')}>"))
    pairs.append((str(REPO_ROOT), "<uta-repo>"))
    pairs.append((str(Path.home()), "<home>"))
    for var in ("TMPDIR", "VIRTUAL_ENV"):
        raw = os.environ.get(var)
        if raw:
            pairs.append((str(Path(raw)).rstrip("/"), f"<{var.lower()}>"))
    # Resolve symlinked temp roots too (/tmp -> /private/tmp on macOS).
    resolved: list[tuple[str, str]] = []
    for source, placeholder in pairs:
        resolved.append((source, placeholder))
        try:
            real = str(Path(source).resolve())
        except OSError:  # pragma: no cover - defensive
            continue
        if real != source:
            resolved.append((real, placeholder))
    resolved.sort(key=lambda item: len(item[0]), reverse=True)
    return resolved


def normalize_text(value: str, replacements: Sequence[tuple[str, str]] = ()) -> str:
    """Scrub one string of machine-local paths, timestamps and digests."""
    text = value
    for source, placeholder in replacements:
        if source and source in text:
            text = text.replace(source, placeholder)
    text = _ISO_TIMESTAMP.sub("<timestamp>", text)
    text = _ABS_PATH.sub("<abs-path>", text)
    text = _HEX64.sub("<sha256>", text)
    text = _HEX40.sub("<sha1>", text)
    text = _HEX32.sub("<hex32>", text)
    text = _HEX24.sub("<hex24>", text)
    text = _HEX16.sub("<hex16>", text)
    return text


def normalize(value: Any, replacements: Sequence[tuple[str, str]] = ()) -> Any:
    """Recursively normalise a JSON-able structure into a comparable baseline.

    Mapping keys are preserved (they are contract), values are scrubbed.
    Volatile values become a constant token, captured process output becomes
    a presence marker, and anything whose key looks like a credential is
    dropped outright rather than scrubbed.
    """
    if isinstance(value, Mapping):
        out: MutableMapping[str, Any] = {}
        for original_key, raw in sorted(value.items(), key=lambda item: str(item[0])):
            key = normalize_text(str(original_key), replacements)
            if _SECRET_KEY_HINT.search(key):
                out[key] = "<redacted>"
                continue
            if key in _VOLATILE_KEYS:
                out[key] = "<volatile>" if raw is not None else None
                continue
            if key in _OUTPUT_KEYS and isinstance(raw, str):
                out[key] = "<empty>" if raw == "" else "<captured-output>"
                continue
            out[key] = normalize(raw, replacements)
        return dict(out)
    if isinstance(value, (list, tuple)):
        return [normalize(item, replacements) for item in value]
    if isinstance(value, str):
        return normalize_text(value, replacements)
    if isinstance(value, float):
        # Rates and gates are contract; wall-clock floats never reach here
        # because their keys are in _VOLATILE_KEYS.
        return round(value, 4)
    return value


#: Counters in an enforcement marker line whose values come from a live mutmut
#: run. Measured on 2026-08-21: three consecutive captures of the same fixture,
#: same code, same interpreter produced `killed=1`, `killed=0`, `killed=0` for a
#: single mutant. The mutation lane is timing-sensitive, so these numbers are
#: not a contract and freezing them would make the gate a coin flip.
#:
#: The marker *line* is still frozen -- its wording, its field names and their
#: order are the compatibility surface, and a change to any of those is what
#: this baseline exists to catch. Only the digits are masked.
_MUTATION_COUNTER = re.compile(
    r"\b(eligible|selected|scored|killed|survived|timeout|suspicious|no_coverage)"
    r"=(\d+)(/(\d+))?"
)


def mask_mutation_counters(text: str) -> str:
    """Replace live mutation counter values with `N`, keeping the field names."""
    def _mask(match: "re.Match[str]") -> str:
        field = match.group(1)
        return f"{field}=N/N" if match.group(3) else f"{field}=N"

    return _MUTATION_COUNTER.sub(_mask, text)


#: Evidence keys whose values come from whether a live mutant happened to be
#: killed. Measured on 2026-08-21 over five consecutive captures of the same
#: fixture: exactly these four of the mutation block's 37 leaf keys varied,
#: flipping between 0 and 1. The other 33 -- every AST-derived count, the
#: filter mechanisms, the gate, the scope -- were identical every time and stay
#: frozen.
_VOLATILE_MUTATION_KEYS = frozenset({
    "killed",
    "changedLineMutantsKilled",
    "changedLineMutantsScored",
})


def mask_mutation_outcomes(payload: Any) -> Any:
    """Mask the mutation-outcome counters, keeping every key and every other value.

    Named keys rather than a numeric sweep, so a change to any *other* count
    still fails the baseline. See `_VOLATILE_MUTATION_KEYS` for the measurement.
    """
    if isinstance(payload, Mapping):
        return {
            key: ("N" if key in _VOLATILE_MUTATION_KEYS else mask_mutation_outcomes(value))
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [mask_mutation_outcomes(item) for item in payload]
    return payload


def assert_free_of_machine_paths(payload: Any) -> None:
    """Fail if a baseline still carries a machine-local path or a secret."""
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
    offenders = [needle for needle in FORBIDDEN_SUBSTRINGS if needle in text]
    assert not offenders, f"baseline artifact leaks machine-local paths: {offenders}"


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / f"{name}.json"


def refresh_requested() -> bool:
    return os.environ.get(REFRESH_ENV, "") not in ("", "0", "false", "False")


def compare_to_baseline(name: str, observed: Any) -> None:
    """Compare ``observed`` with the frozen fixture ``name``.

    The observed payload is normalised and checked for machine-local paths
    before comparison, so a leak fails here rather than being committed.
    """
    assert_free_of_machine_paths(observed)
    path = fixture_path(name)
    if refresh_requested() or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(observed, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if not refresh_requested():
            raise AssertionError(
                f"baseline {name} did not exist and was written; re-run to compare"
            )
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert observed == expected, (
        f"behaviour drifted from the frozen baseline '{name}'.\n"
        "If the change is intended, review it explicitly and re-freeze with "
        f"{REFRESH_ENV}=1; never adjust production code to make a baseline pass."
    )


def assert_no_public_names_removed(name: str, observed: Mapping[str, Any]) -> None:
    """Assert every baselined public name still exists.

    Additions are allowed; removals are not. A slice that quietly stops
    exporting a name from a package ``__init__`` breaks importers that this
    repository cannot see, which is exactly what the baseline is for.
    """
    assert_free_of_machine_paths(observed)
    path = fixture_path(name)
    if refresh_requested() or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(observed, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if not refresh_requested():
            raise AssertionError(
                f"baseline {name} did not exist and was written; re-run to compare"
            )
        return
    expected = json.loads(path.read_text(encoding="utf-8"))
    missing_packages = sorted(set(expected) - set(observed))
    assert not missing_packages, f"packages disappeared from the import surface: {missing_packages}"
    removed: dict[str, list[str]] = {}
    for package, entry in expected.items():
        gone = sorted(set(entry["names"]) - set(observed[package]["names"]))
        if gone:
            removed[package] = gone
    assert not removed, (
        "public names disappeared from package __init__ exports: "
        f"{json.dumps(removed, indent=2, sort_keys=True)}"
    )


def sql_shape(statement: str) -> str:
    """Reduce one SQL statement to the shape that is contract.

    Literal values, whitespace and parameter counts are noise; the verb and
    the table are what a transaction-splitting refactor changes.
    """
    text = " ".join(str(statement).split())
    upper = text.upper()
    for verb in ("INSERT INTO", "SELECT", "UPDATE", "DELETE FROM", "REPLACE INTO"):
        if upper.startswith(verb):
            remainder = text[len(verb) :].strip()
            if verb == "SELECT":
                lowered = remainder.lower()
                marker = lowered.find(" from ")
                remainder = remainder[marker + 6 :].strip() if marker >= 0 else ""
            table = re.split(r"[\s(,;]", remainder, maxsplit=1)[0].strip('"`[]')
            return f"{verb.split()[0]} {table}" if table else verb.split()[0]
    for control in ("BEGIN", "COMMIT", "ROLLBACK", "PRAGMA", "CREATE", "DROP", "ALTER", "SAVEPOINT", "RELEASE"):
        if upper.startswith(control):
            return control
    return upper.split(" ", 1)[0]


def summarize_statements(statements: Iterable[str]) -> dict[str, Any]:
    """Return the ordered shapes and per-shape counts for a traced operation."""
    shapes = [sql_shape(item) for item in statements]
    counts: dict[str, int] = {}
    for shape in shapes:
        counts[shape] = counts.get(shape, 0) + 1
    return {
        "statements": shapes,
        "counts": dict(sorted(counts.items())),
        "total": len(shapes),
        "write_total": sum(
            value
            for key, value in counts.items()
            if key.split(" ", 1)[0] in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
        ),
        "commit_total": counts.get("COMMIT", 0),
    }


__all__ = [
    "FIXTURE_DIR",
    "REFRESH_ENV",
    "REPO_ROOT",
    "assert_free_of_machine_paths",
    "assert_no_public_names_removed",
    "compare_to_baseline",
    "fixture_path",
    "normalize",
    "normalize_text",
    "path_replacements",
    "refresh_requested",
    "sql_shape",
    "summarize_statements",
]
