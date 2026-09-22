"""Optional externally supplied behavior context for generation prompts.

The value is an operator-provided string: either a path to a local file or
inline text. It is injected verbatim into plan/generate prompts so the agent
can derive expected behavior from stated business rules instead of mirroring
the implementation. Content is bounded and never written into reports or
evidence payloads — only its presence and length may be logged.
"""

from __future__ import annotations

from pathlib import Path

MAX_SPEC_CONTEXT_BYTES = 16 * 1024
SPEC_CONTEXT_TRUNCATION_MARKER = "...[spec context truncated]"

# A path-like argument longer than this is treated as inline text outright.
_MAX_PATH_PROBE_CHARS = 1024


def resolve_spec_context(value: str | None, *, max_bytes: int = MAX_SPEC_CONTEXT_BYTES) -> str:
    """Resolve a CLI/API spec-context value to bounded prompt text.

    An existing file path is read; anything else is inline text. The result is
    capped at ``max_bytes`` with a visible truncation marker.
    """
    if not value:
        return ""
    text = str(value)
    if len(text) <= _MAX_PATH_PROBE_CHARS and "\n" not in text:
        try:
            candidate = Path(text).expanduser()
            if candidate.is_file():
                with candidate.open("rb") as handle:
                    text = handle.read(max_bytes + 1).decode("utf-8", errors="replace")
        except OSError:
            pass
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text.strip()
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return f"{truncated}\n{SPEC_CONTEXT_TRUNCATION_MARKER}"
