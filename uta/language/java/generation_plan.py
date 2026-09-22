"""Generation-plan artifacts for the Java target lifecycle."""

import logging
import re
from pathlib import Path
from typing import Any, List, Optional


logger = logging.getLogger("uta")

_MAX_PLAN_CHARS = 3000  # target cap for compressed plan in generation prompt


def _generation_plan_path(repo_path: str) -> Path:
    return Path(repo_path) / ".uta_cache" / "context" / "latest_generation_plan.md"


def _generation_plan_candidate_path(repo_path: str) -> Path:
    return Path(repo_path) / ".uta_cache" / "context" / "latest_generation_plan.candidate.md"


def _clear_generation_plan(repo_path: str) -> None:
    for plan_path in (_generation_plan_path(repo_path), _generation_plan_candidate_path(repo_path)):
        try:
            plan_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to clear stale generation plan artifact: %s", plan_path, exc_info=True)


def _write_generation_plan(repo_path: str, session_id: str, batch: List[str], plan_text: str) -> str:
    ctx_dir = _generation_plan_path(repo_path).parent
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = _generation_plan_path(repo_path)
    lines = [
        "# Latest Generation Plan",
        "",
        f"- session_id: `{session_id}`",
        f"- classes: `{', '.join(batch)}`",
        "",
        plan_text.strip() or "_No plan text captured._",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    try:
        _generation_plan_candidate_path(repo_path).unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to clear candidate generation plan after final write", exc_info=True)
    return str(out.resolve())


def _write_generation_plan_candidate(
    repo_path: str,
    session_id: str,
    batch: List[str],
    plan_text: str,
    replan_reasons: List[str],
) -> str:
    ctx_dir = _generation_plan_candidate_path(repo_path).parent
    ctx_dir.mkdir(parents=True, exist_ok=True)
    out = _generation_plan_candidate_path(repo_path)
    lines = [
        "# Candidate Generation Plan",
        "",
        f"- session_id: `{session_id}`",
        f"- classes: `{', '.join(batch)}`",
        "- status: `candidate-before-replan`",
        "",
        "## Replan Reasons",
        "",
        *(f"- {reason}" for reason in replan_reasons),
        "",
        "## Plan",
        "",
        plan_text.strip() or "_No plan text captured._",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out.resolve())


def _extract_plan_body_from_artifact(content: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "# Latest Generation Plan":
        body_started = False
        body: List[str] = []
        for line in lines[1:]:
            stripped = line.strip()
            if not body_started:
                if stripped.startswith("- session_id:") or stripped.startswith("- classes:") or stripped == "":
                    continue
                body_started = True
            if body_started:
                body.append(line)
        text = "\n".join(body).strip()
    elif lines and lines[0].strip() == "# Candidate Generation Plan":
        body_started = False
        body = []
        for line in lines[1:]:
            stripped = line.strip()
            if not body_started:
                if (
                    stripped.startswith("- session_id:")
                    or stripped.startswith("- classes:")
                    or stripped.startswith("- status:")
                    or stripped == ""
                    or stripped == "## Replan Reasons"
                    or stripped == "## Plan"
                    or stripped.startswith("- ")
                ):
                    continue
                body_started = True
            if body_started:
                body.append(line)
        text = "\n".join(body).strip()
    if text == "_No plan text captured._":
        return ""
    return text


def _generation_plan_artifact_session_id(content: str) -> str:
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- session_id:"):
            continue
        return stripped.removeprefix("- session_id:").strip().strip("`").strip()
    return ""


def _generation_plan_artifact_classes(content: str) -> List[str]:
    for line in (content or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- classes:"):
            continue
        raw = stripped.removeprefix("- classes:").strip().strip("`").strip()
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _load_generation_plan_for_resume(repo_path: str, batch: List[str]) -> str:
    artifacts = [
        (_generation_plan_path(repo_path), "generation plan"),
        (_generation_plan_candidate_path(repo_path), "candidate generation plan"),
    ]
    for plan_path, label in artifacts:
        if not plan_path.exists():
            continue
        try:
            content = plan_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            logger.warning("Resume requested but %s artifact could not be read: %s", label, plan_path, exc_info=True)
            continue

        artifact_classes = _generation_plan_artifact_classes(content)
        if artifact_classes and artifact_classes != batch:
            logger.warning(
                "Resume requested but %s classes %s do not match current batch %s; ignoring artifact",
                label,
                artifact_classes,
                batch,
            )
            continue
        return _extract_plan_body_from_artifact(content)

    logger.warning(
        "Resume requested but neither final nor candidate generation plan artifact exists for batch %s",
        batch,
    )
    return ""


def _plan_needs_stricter_replan(
    plan_text: str, strict_classes: List[dict[str, Any]]
) -> bool:
    """Reject a lenient plan when a broad target requires gate-level detail."""
    if not strict_classes:
        return False
    lowered = plan_text.lower()
    required_markers = ("methods required for gate", "estimated reach")
    if any(marker not in lowered for marker in required_markers):
        return True
    weak_markers = (
        "do not chase class-wide completeness",
        "defer heavier branches",
        "high-value public methods first",
        "fall back to",
    )
    return any(marker in lowered for marker in weak_markers)


def _compress_plan_for_generation(plan_text: str) -> str:
    """Compress verbose plan prose into a compact generation-ready summary (strategy I).

    Tries three passes in order of decreasing fidelity:
    1. Extract structured PLANNED TESTS items + WAVE assignments into a table.
    2. Extract just the section headers + bullet points (strip long explanations).
    3. Hard-truncate to _MAX_PLAN_CHARS with a note.

    If the plan is already short enough, returns it unchanged.
    """
    if not plan_text or len(plan_text) <= _MAX_PLAN_CHARS:
        return plan_text

    compressed = _extract_planned_tests_table(plan_text)
    if compressed and len(compressed) <= _MAX_PLAN_CHARS:
        return compressed

    compressed = _strip_plan_prose(plan_text)
    if len(compressed) <= _MAX_PLAN_CHARS:
        return compressed

    return (
        plan_text[: _MAX_PLAN_CHARS - 80].rstrip()
        + "\n\n... [plan truncated for token efficiency; see plan file for full details]"
    )


def _extract_planned_tests_table(plan_text: str) -> str:
    """Extract test-method entries from plan text into a compact table.

    Looks for lines like:
      - `testMethodName` — covers branch X (wave 1)
      - testFooBar: covers guard clause (WAVE 1)
    Returns empty string if no such entries are found.
    """
    test_entry_re = re.compile(
        r"^\s*[-*]\s+[`\"]?(?P<name>test\w+)[`\"]?\s*[:\-–—]?\s*(?P<desc>[^\n]{0,120})",
        re.IGNORECASE | re.MULTILINE,
    )
    wave_re = re.compile(r"\bwave\s*[12]\b", re.IGNORECASE)

    rows: List[str] = []
    for match in test_entry_re.finditer(plan_text):
        name = match.group("name").strip("`\"")
        desc = match.group("desc").strip()
        wave = (
            "W1"
            if wave_re.search(match.group(0))
            and "1" in (wave_re.search(match.group(0)) or re.match("", "")).group(0)
            else ("W2" if "wave 2" in match.group(0).lower() else "W1")
        )
        if len(desc) > 80:
            desc = desc[:77] + "..."
        rows.append(f"| `{name}` | {desc} | {wave} |")

    if not rows:
        return ""
    header = "| Test method | Description | Wave |\n|---|---|---|"
    return header + "\n" + "\n".join(rows)


def _strip_plan_prose(plan_text: str) -> str:
    """Keep section headers and bullet points; strip long explanatory paragraphs."""
    lines = plan_text.splitlines()
    out: List[str] = []
    consecutive_blank = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            consecutive_blank += 1
            if consecutive_blank <= 1:
                out.append("")
            continue
        consecutive_blank = 0
        if stripped.startswith("#") or stripped.startswith("-") or stripped.startswith("*") or stripped.startswith("|"):
            out.append(line)
        elif len(stripped) <= 100:
            out.append(line)
    return "\n".join(out).strip()


def _recover_plan_text_from_session_artifact(
    *,
    repo_path: str,
    session_id: str,
    client: Any,
) -> str:
    plan_path = _generation_plan_path(repo_path)
    candidate_path = _generation_plan_candidate_path(repo_path)
    # The artifact contains the neutral session id that produced it. That is
    # sufficient ownership evidence; inspecting an implementation-native
    # transcript here made recovery depend on OpenCode's message-part schema.
    for artifact_path in (plan_path, candidate_path):
        if not artifact_path.exists():
            continue
        try:
            content = artifact_path.read_text(encoding="utf-8", errors="replace")
            artifact_session_id = _generation_plan_artifact_session_id(content)
            if artifact_session_id and artifact_session_id != session_id:
                logger.warning(
                    "Ignoring generation plan artifact for stale session %s while recovering session %s",
                    artifact_session_id,
                    session_id,
                )
                continue
            return _extract_plan_body_from_artifact(content)
        except Exception:
            continue
    return ""


def _prepare_continue_artifact_for_phase(*, repo_path: Optional[str], phase: str) -> None:
    if phase != "plan" or not repo_path:
        return

    plan_path = _generation_plan_path(repo_path)
    candidate_path = _generation_plan_candidate_path(repo_path)
    if plan_path.exists() or not candidate_path.exists():
        return

    try:
        content = candidate_path.read_text(encoding="utf-8", errors="replace")
        plan_text = _extract_plan_body_from_artifact(content)
        session_id = _generation_plan_artifact_session_id(content) or "resume-plan"
        classes = _generation_plan_artifact_classes(content)
        _write_generation_plan(repo_path, session_id, classes, plan_text)
        logger.info("Materialized candidate generation plan into latest_generation_plan.md for planning resume")
    except Exception:
        logger.warning("Failed to materialize candidate generation plan before planning resume", exc_info=True)
