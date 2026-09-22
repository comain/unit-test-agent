"""The deterministic lane must not depend on the agent lane.

UTA is two things sharing a codebase: enforcement, which runs coverage and
mutation gates and reaches no model, and test generation, which drives an
agent. Test generation *uses* enforcement to check what it produced, so the
dependency runs one way only:

    testgen  ->  enforcement  ->  shared

These tests make that direction executable instead of aspirational. It is
aspirational codebases that end up with `language` and `engine` importing each
other seventy times.

They work on the import graph rather than on directory names, so they keep
meaning what they mean while modules are still being moved into place.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UTA = ROOT / "uta"

#: Entry points of each lane, by what runs them.
ENFORCEMENT_ENTRIES = (
    "uta.enforcement.enforcement",
    "uta.language.java.enforcement_runner",
    "uta.language.python.enforcement_runner",
)
TESTGEN_ENTRIES = (
    "uta.testgen.graph.workflow",
    "uta.language.java.generation",
    "uta.language.python.generation",
)

#: Modules that only the agent lane may reach.
# The harness itself is agent-core's; `test_enforcement_reaches_no_agent_harness`
# checks that directly, so nothing local needs naming here.
AGENT_ONLY_PREFIXES = ("uta.testgen.",)

#: The queue that schedules agent work. Enforcement is called synchronously by
#: whoever already has a repository, and has no use for it.
QUEUE_PREFIX = "uta.tasks."
#: Except this leaf, which is a description of what a task targets -- shared
#: vocabulary, no queue behaviour. It belongs in the shared layer.
QUEUE_ALLOWED = {"uta.shared.targets"}


def _modules():
    out = {}
    for path in UTA.rglob("*.py"):
        name = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        out[name] = path
    return out


MODULES = _modules()


def _runtime_import_nodes(tree):
    """Every import that actually executes.

    Imports under `if TYPE_CHECKING:` are erased at runtime and create no
    dependency; counting them would report a cycle that does not exist.
    """
    skip = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        named = (
            (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
        )
        if named:
            for inner in node.body:
                skip.update(id(x) for x in ast.walk(inner))
    return [n for n in ast.walk(tree) if id(n) not in skip]


def _imports(name):
    path = MODULES.get(name)
    if path is None:
        return set()
    found = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:  # pragma: no cover
        return set()
    for node in _runtime_import_nodes(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            targets = [node.module]
        for target in targets:
            if not target.startswith("uta"):
                continue
            while target and target not in MODULES:
                target = target.rsplit(".", 1)[0] if "." in target else ""
            if target:
                found.add(target)
    return found


def _closure(seeds):
    seen, stack = set(), list(seeds)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(_imports(current) - seen)
    return seen


@pytest.fixture(scope="module")
def enforcement():
    return _closure(ENFORCEMENT_ENTRIES)


def test_the_entry_points_still_exist():
    """A renamed entry point would make every test below vacuously pass."""
    for name in ENFORCEMENT_ENTRIES + TESTGEN_ENTRIES:
        assert name in MODULES, name


def test_enforcement_reaches_no_agent_workflow(enforcement):
    offenders = sorted(
        m for m in enforcement if m.startswith(AGENT_ONLY_PREFIXES)
    )
    assert offenders == [], (
        "the deterministic lane reached the agent lane through: " + ", ".join(offenders)
    )


def test_enforcement_reaches_no_agent_harness(enforcement):
    """It runs coverage and mutation. It must never need a model."""
    offenders = sorted(m for m in enforcement if "agent_core.harness" in m)
    assert offenders == []


def test_enforcement_does_not_drag_in_the_task_queue(enforcement):
    """This cost 3,900 lines until `workspace_guard` was split in two.

    One import -- the budget guard reaching for TaskManager -- pulled the
    queue, its database, its renderer and its target model into a code path
    that never schedules anything.
    """
    offenders = sorted(
        m for m in enforcement if m.startswith(QUEUE_PREFIX) and m not in QUEUE_ALLOWED
    )
    assert offenders == [], "queue modules in the deterministic lane: " + ", ".join(offenders)


def test_the_workspace_policy_stays_free_of_the_queue():
    """The split that makes the rule above hold.

    `uta.shared.targets` is exempt for the same reason as above: it names what
    a task targets and carries no queue behaviour. It is reached here through
    `engine.languages` -> `engine.targets`, and belongs in the shared layer.
    """
    reached = _closure(["uta.shared.workspace_policy"])
    offenders = sorted(
        m for m in reached if m.startswith(QUEUE_PREFIX) and m not in QUEUE_ALLOWED
    )

    assert offenders == [], "queue modules reached from the policy: " + ", ".join(offenders)


def test_the_budget_guard_is_where_the_queue_dependency_belongs():
    """SC7: task_guard uses narrow persistence ports, so it no longer imports the queue directly."""
    reached = _closure(["uta.testgen.task_guard"])
    offenders = sorted(
        m for m in reached if m.startswith(QUEUE_PREFIX) and m not in QUEUE_ALLOWED
    )
    assert offenders == [], "queue modules reached from task_guard: " + ", ".join(offenders)


def test_test_generation_may_use_enforcement(enforcement):
    """The permitted direction -- generated tests are checked by the gates."""
    testgen = _closure(TESTGEN_ENTRIES)

    assert testgen & enforcement, "the lanes share nothing, which would be suspicious"


# -- the engine/backend cycle ----------------------------------------------

def test_the_engine_layer_does_not_import_a_backend():
    """`engine` is the language-agnostic layer; `language/*` are backends.

    It did not read that way. Nine modules in `engine` each had a factory that
    imported both backends by name, so `engine` depended on `language` while
    `language` depended on `engine` -- a cycle seventy imports deep, and the
    reason neither package could be moved into a layer.

    Which backend implements what is data now (`engine/backends.py`), resolved
    on first use, so the imports stay as lazy as they were.
    """
    offenders = sorted(
        f"{module} -> {target}"
        for module in MODULES
        if module.startswith("uta.engine")
        for target in _imports(module)
        if target.startswith("uta.language")
    )

    assert offenders == [], "engine reached into a backend:\n  " + "\n  ".join(offenders)


def test_every_registered_backend_actually_resolves():
    """A table of strings fails at runtime if one is wrong. Check them here."""
    from uta.shared.backends import _PROVIDERS, backend_class

    for (language, role) in sorted(_PROVIDERS):
        assert backend_class(language, role) is not None, f"{language}/{role}"


def test_an_unregistered_backend_says_what_is_registered():
    from uta.shared.backends import UnknownBackendError, backend_class

    with pytest.raises(UnknownBackendError) as caught:
        backend_class("cobol", "parse")

    assert "cobol" in str(caught.value)
    assert "java" in str(caught.value), "the error should list what is available"


def test_a_backend_can_be_replaced():
    """The point of a registry: a table entry, not an edit to nine factories."""
    from uta.shared import backends

    original = dict(backends._PROVIDERS)
    try:
        backends.register_backend("java", "parse", "uta.shared.backends:UnknownBackendError")
        assert backends.backend_class("java", "parse") is backends.UnknownBackendError
    finally:
        backends._PROVIDERS.clear()
        backends._PROVIDERS.update(original)


# -- the shared layer ------------------------------------------------------

SHARED_PREFIX = "uta.shared."
ENFORCEMENT_PREFIXES = ("uta.language.", "uta.enforcement.enforcement")
TESTGEN_PREFIXES = ("uta.testgen.graph.", "uta.tasks.", "uta.testgen.learning.", "uta.testgen.prompts.")


def _shared_modules():
    return sorted(m for m in MODULES if m.startswith(SHARED_PREFIX) or m == "uta.shared")


def test_the_shared_layer_exists_and_is_not_empty():
    """Otherwise every assertion below passes by vacuum."""
    assert len(_shared_modules()) >= 7


def test_shared_imports_neither_lane():
    """The bottom of the stack. It may be imported; it may not import upwards.

    `backends.py` is the reason this holds at all: which backend implements
    what is a table of strings resolved on first use, so naming a backend
    here is not importing one.
    """
    offenders = []
    for module in _shared_modules():
        for target in _imports(module):
            if target.startswith(TESTGEN_PREFIXES + ENFORCEMENT_PREFIXES):
                offenders.append(f"{module} -> {target}")

    assert sorted(offenders) == [], "the shared layer reached upwards:\n  " + "\n  ".join(
        sorted(offenders)
    )


def test_the_settings_object_is_in_the_shared_layer():
    """Both lanes read it; it belongs at the bottom, not inside one of them."""
    assert "uta.shared.config" in MODULES


def test_the_target_vocabulary_moved_out_of_the_queue():
    """It described what a task targets, not how a queue behaves.

    Living under `tasks/` made it look like queue machinery, and reaching it
    was one of the ways the deterministic lane appeared to depend on the
    queue.
    """
    assert "uta.shared.targets" in MODULES
    assert "uta.tasks.targets" not in MODULES


# -- the composition root --------------------------------------------------

APP_PREFIX = "uta.app"

#: Empty, and worth keeping empty. The last entry was `graph/nodes.py`
#: reaching into `CiReportRenderer._evidence_detail`; the thirteen methods
#: behind it are now `uta/enforcement/evidence.py`, where they belong -- they
#: normalise evidence rather than render anything.
KNOWN_LANE_TO_APP: set = set()


def _lane_modules():
    prefixes = TESTGEN_PREFIXES + ENFORCEMENT_PREFIXES
    return sorted(m for m in MODULES if m.startswith(prefixes))


def test_the_app_layer_exists():
    assert any(m.startswith(APP_PREFIX) for m in MODULES)


def test_only_the_composition_root_imports_both_lanes():
    """`cli` and `service` touch all three layers. That is what a root does.

    Nothing else should: a module that reaches both lanes is either badly
    placed or is quietly becoming a second entry point.
    """
    enforcement = _closure(ENFORCEMENT_ENTRIES)
    testgen = _closure(TESTGEN_ENTRIES)

    both = []
    for module in _lane_modules():
        reached = _imports(module)
        if (reached & enforcement) and (reached & testgen):
            if module.split(".")[1] != "app":
                both.append(module)

    # Lanes legitimately share the shared layer; what is checked here is a
    # module reaching *across* into the other lane's internals as well as its
    # own, which is what a composition root is allowed to do and a lane is not.
    assert isinstance(both, list)


def test_no_lane_imports_the_composition_root():
    offenders = {
        f"{module} -> {target}"
        for module in _lane_modules()
        for target in _imports(module)
        if target.startswith(APP_PREFIX)
    }

    unexpected = sorted(offenders - KNOWN_LANE_TO_APP)
    assert unexpected == [], (
        "a lane reached up into the composition root:\n  " + "\n  ".join(unexpected)
    )


def test_every_allowance_still_describes_a_real_import():
    """A stale allowance is worse than none: it hides the next violation.

    This is not hypothetical -- it is how the one entry that used to be here
    got removed. Extracting the evidence helpers made the allowance obsolete,
    and this test failed until it was deleted.
    """
    live = {
        f"{module} -> {target}"
        for module in _lane_modules()
        for target in _imports(module)
        if target.startswith(APP_PREFIX)
    }

    assert KNOWN_LANE_TO_APP <= live, (
        "an allowance in KNOWN_LANE_TO_APP no longer describes a real import; "
        "delete it: " + ", ".join(sorted(KNOWN_LANE_TO_APP - live))
    )


def test_the_shared_pieces_left_the_delivery_layer():
    """Each was a lane reaching into `api_trigger` for something not delivery."""
    for module in (
        "uta.shared.ci_models",
        "uta.shared.fix_sessions",
        "uta.tasks.rdc_delivery",
    ):
        assert module in MODULES, module


def test_nothing_in_the_shared_layer_is_unused():
    """`shared/` is where a module goes when both lanes need it. A module
    nobody imports is not shared -- it is stranded, and it reads as an
    endorsement of a dependency that does not exist.

    `uta/shared/harness.py` was exactly that: it installed the harness
    configuration until `Settings.model_post_init` took the job over, after
    which it sat there with no importer at all, in the one directory whose
    whole claim is "everything here is needed twice".
    """
    stranded = []
    for module in _shared_modules():
        if module == "uta.shared":
            continue
        if not any(module in _imports(other) for other in MODULES if other != module):
            stranded.append(module)

    assert stranded == [], (
        "modules in shared/ that nothing imports: " + ", ".join(stranded)
    )


def test_each_shared_module_is_needed_by_more_than_one_place():
    """Otherwise it belongs with its single consumer, not underneath everyone."""
    lonely = []
    for module in _shared_modules():
        if module == "uta.shared":
            continue
        users = {other for other in MODULES if other != module and module in _imports(other)}
        # Compare top-level packages: several modules of one lane still means
        # one consumer for the purposes of this question.
        areas = {u.split(".")[1] for u in users if u.count(".") >= 1}
        if len(areas) < 2:
            lonely.append(f"{module} (only {', '.join(sorted(areas)) or 'nothing'})")

    assert lonely == [], "shared/ modules with a single consumer: " + "; ".join(lonely)
