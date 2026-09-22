"""RDC task context has to reach the repair prompts.

An RDC-triggered task already states the behaviour the change is meant to
produce -- the requester's context and the issue description -- and the Python
repair prompts were being rendered without it. The agent then had only the
implementation to infer intent from, which is exactly the failure spec context
exists to prevent: tests that mirror the code instead of the requirement.

Ported from main `79ac25c`. There the merge lived in `uta/language/python/
batch.py`, a large module this branch replaced with a thin facade over
`generation.py`, so the helper lives beside the other Python task-context
readers in `selection.py` and is wired where the cycle builds its prompt
inputs.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from uta.language.python.selection import prompt_spec_context_from_task_context
from uta.testgen.prompts.loader import render_prompt
from uta.testgen.spec_context import MAX_SPEC_CONTEXT_BYTES


def _fields_for(prompt: str, **overrides) -> dict:
    """Every variable the template references, blank unless overridden.

    Derived from the template rather than listed, so a prompt that grows a new
    variable does not turn this into a puzzle about Jinja rather than about
    spec context.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[1]
        / "uta"
        / "testgen"
        / "prompts"
        / f"{prompt}.txt"
    ).read_text(encoding="utf-8")
    names = set(re.findall(r"{{-?\s*([a-zA-Z_][a-zA-Z0-9_]*)", source))
    names |= set(re.findall(r"{%-?\s*(?:if|for .* in)\s+([a-zA-Z_][a-zA-Z0-9_]*)", source))
    fields = {name: "" for name in names}
    fields.update(overrides)
    return fields


class _Manager:
    def __init__(self, context):
        self._context = context

    def get_task(self, task_id):
        import json

        return {"rdc_context_json": json.dumps(self._context)}


def test_the_operator_value_leads():
    """Someone who supplied a spec context meant it; the task's own context
    widens it rather than displacing it."""
    manager = _Manager({"user": {"context": "from the task"}})

    merged = prompt_spec_context_from_task_context(manager, 1, explicit="from the operator")

    assert merged.startswith("from the operator")
    assert "from the task" in merged


def test_both_task_sections_are_included():
    manager = _Manager(
        {
            "user": {"context": "only repair the selected target"},
            "issue": {"description": "fallback must stay documented"},
        }
    )

    merged = prompt_spec_context_from_task_context(manager, 1)

    assert "only repair the selected target" in merged
    assert "fallback must stay documented" in merged


def test_a_repeated_section_is_not_repeated():
    """The same text is routinely pasted into both the issue and the request;
    duplicating it in the prompt spends context budget on nothing."""
    manager = _Manager(
        {"user": {"context": "same words"}, "issue": {"description": "same words"}}
    )

    merged = prompt_spec_context_from_task_context(manager, 1, explicit="same words")

    assert merged == "same words"


def test_the_result_is_bounded():
    """This goes into a prompt."""
    manager = _Manager({"user": {"context": "x" * (MAX_SPEC_CONTEXT_BYTES * 2)}})

    merged = prompt_spec_context_from_task_context(manager, 1)

    assert len(merged) <= MAX_SPEC_CONTEXT_BYTES


@pytest.mark.parametrize(
    "manager,task_id",
    [(None, 1), (_Manager({}), None)],
    ids=["no-manager", "no-task-id"],
)
def test_without_a_task_only_the_explicit_value_survives(manager, task_id):
    assert prompt_spec_context_from_task_context(manager, task_id, explicit="just this") == "just this"


@pytest.mark.parametrize(
    "prompt",
    ["python_fix_compile", "python_fix_coverage", "python_fix_mutations"],
)
def test_the_repair_prompts_render_the_supplied_context(prompt):
    """All three, because a repair that fires on the one prompt that forgot is
    indistinguishable from one that never had the context."""
    rendered = render_prompt(
        prompt, **_fields_for(prompt, spec_context="preserve the documented fallback")
    )

    assert "SUPPLIED BEHAVIOR CONTEXT" in rendered
    assert "preserve the documented fallback" in rendered


@pytest.mark.parametrize(
    "prompt",
    ["python_fix_compile", "python_fix_coverage", "python_fix_mutations"],
)
def test_the_block_is_absent_without_context(prompt):
    rendered = render_prompt(prompt, **_fields_for(prompt, spec_context=""))

    assert "SUPPLIED BEHAVIOR CONTEXT" not in rendered


def test_the_cycle_actually_merges_the_task_context(tmp_path):
    """The wiring, not the helper.

    Every test above calls `prompt_spec_context_from_task_context` directly, so
    they pass whether or not the cycle ever calls it -- the same blind spot
    that let the Java runtime resolver ship unwired. This goes through
    `_spec_context`, which is what `prepare_python_cycle_state` puts into the
    prompt, using a real task row.
    """
    import uta.language.python.batch  # noqa: F401  -- registers the backend
    from uta.language.python.cycle_inputs import _spec_context
    from uta.shared.languages import RawTargetSelection, default_registry
    from uta.tasks.manager import TaskManager

    repo = tmp_path / "repo"
    (repo / "jobs").mkdir(parents=True)
    (repo / "jobs" / "forecast.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    db = tmp_path / "tasks.db"
    manager = TaskManager(db)
    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py")
    )
    task_id = manager.create_task_targets(
        repo_path=str(repo),
        targets=[target],
        language="python",
        rdc_context={
            "user": {"context": "preserve the documented fallback"},
            "issue": {"description": "TASK-1 keep behaviour"},
        },
    )

    merged = _spec_context(
        {
            "task_id": task_id,
            "task_db_path": str(db),
            "spec_context": "operator says hi",
        }
    )

    assert "operator says hi" in merged
    assert "preserve the documented fallback" in merged
    assert "TASK-1 keep behaviour" in merged


def test_a_cycle_without_a_task_keeps_the_operator_value(tmp_path):
    from uta.language.python.cycle_inputs import _spec_context

    assert _spec_context({"spec_context": "only this"}) == "only this"
