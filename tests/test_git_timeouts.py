"""No git command in this project runs unbounded.

`fetch`, `push` and `rebase` talk to a remote, where a server that accepts a
connection and never answers hangs the caller indefinitely. The worst case is
auto-push: it runs *after* a task has succeeded, so a hang strands finished
work, holds the slot until someone kills the process, and can leave a
half-rebased checkout behind.

The local reads matter less but fail the same way, and `git log --since` over
a wide window on a large history is slow rather than hung -- the harder one to
recognise.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from agent_core.git import GitTimeout

ROOT = Path(__file__).resolve().parent.parent


# -- RDC delivery, the one with the worst failure ---------------------------

def _pusher(tmp_path):
    from uta.app.delivery import RdcRepairPublisher

    return RdcRepairPublisher(repo_path=tmp_path)


def test_auto_push_applies_a_timeout(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (seen.update(kw), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )

    _pusher(tmp_path).publisher.runner.run(tmp_path, "status")

    assert seen.get("timeout") == 600


def test_a_hung_push_raises_a_typed_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, kw.get("timeout"))),
    )

    with pytest.raises(GitTimeout) as caught:
        _pusher(tmp_path).publisher.runner.run(tmp_path, "push", "-u", "origin", "branch")

    assert "push -u origin branch" in str(caught.value)


def test_a_hang_raises_even_with_check_false(tmp_path, monkeypatch):
    """A command that never returned has no exit code for a caller to read."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 1)),
    )

    with pytest.raises(GitTimeout):
        _pusher(tmp_path).publisher.runner.run(tmp_path, "fetch", "origin", check=False)


def test_zero_disables_the_bound(tmp_path, monkeypatch):
    """For an operator with a push that legitimately takes longer."""
    from uta.shared.config import settings

    monkeypatch.setattr(settings, "ci_git_command_timeout_seconds", 0)
    seen = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: (seen.update(kw), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )

    _pusher(tmp_path).publisher.runner.run(tmp_path, "status")

    assert seen["timeout"] is None


def test_every_path_shares_one_budget():
    """One answer to "how long", not one per feature."""
    from uta.shared.config import settings
    from uta.shared.git import git_timeout

    assert git_timeout() == settings.ci_git_command_timeout_seconds


# -- and nothing anywhere else is unbounded ---------------------------------

#: Sites that legitimately pass no timeout, with the reason.
ALLOWED_WITHOUT_TIMEOUT = {
    # Delegates to agent-core, which applies its own bound.
    "uta/shared/workspace_policy.py",
    # Its `_run` supplies `timeout=self.command_timeout_seconds` at the call.
    "uta/app/workspace.py",
}


def _sets_timeout_default(tree, call):
    """Whether the function containing `call` sets a timeout into its kwargs.

    Matches `kwargs.setdefault("timeout", ...)`, which is how a wrapper gives
    every one of its call sites a bound while still letting one override it.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(inner is call for inner in ast.walk(node)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and getattr(inner.func, "attr", None) == "setdefault"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and inner.args[0].value == "timeout"
            ):
                return True
    return False


def _git_calls_without_timeout():
    offenders = []
    for path in (ROOT / "uta").rglob("*.py"):
        relative = str(path.relative_to(ROOT))
        if relative in ALLOWED_WITHOUT_TIMEOUT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if target not in {"run", "Popen", "check_output", "call"}:
                continue
            if not node.args:
                continue
            first = node.args[0]
            literals = [
                e.value for e in getattr(first, "elts", [])
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
            if not literals or literals[0] != "git":
                continue
            has_keyword = any(k.arg == "timeout" for k in node.keywords)
            # `**kwargs` where the enclosing function set a default first --
            # the timeout is real, it just is not visible at the call.
            forwards_kwargs = any(k.arg is None for k in node.keywords)
            if not has_keyword and not (forwards_kwargs and _sets_timeout_default(tree, node)):
                offenders.append(f"{relative}:{node.lineno}")
    return sorted(offenders)


def test_no_git_command_runs_unbounded():
    offenders = _git_calls_without_timeout()

    assert offenders == [], (
        "git invoked with no timeout at:\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_still_describes_real_files():
    """A stale entry would hide the next unbounded call in that file."""
    for relative in ALLOWED_WITHOUT_TIMEOUT:
        assert (ROOT / relative).exists(), relative


# -- one place, not five ---------------------------------------------------

#: The only module that may call `subprocess` with git directly.
GIT_OWNER = "uta/shared/git.py"

#: Wrappers that forward to the owner rather than invoking git themselves.
#: They exist so their call sites keep a familiar name, not to re-derive
#: anything, and each is a one-line delegation.
ALLOWED_WRAPPERS = {
    "uta/app/workspace.py",   # adds stage logging and its own retry policy
    "uta/app/cli.py",         # interactive clone/fetch, no credentials needed
    "uta/app/context.py",     # injectable `run_command` for tests
    "uta/engine/source_selection.py",
    "uta/enforcement/enforcement.py",
    "uta/language/java/enforcement_runner/planning.py",
    "uta/language/python/enforcement_runner.py",
    "uta/language/python/verification/runner.py",
}


def test_git_is_invoked_in_one_place():
    """Four helpers each worked out a different subset of credentials, a
    timeout, typed failures and retry. None had all four, and a fix to one was
    invisible to the other three -- which is what happens when a concern has no
    owner.
    """
    offenders = sorted(
        {
            site.split(":")[0]
            for site in _git_calls_without_timeout()
        }
    )

    assert offenders == [], "git invoked outside " + GIT_OWNER + ": " + ", ".join(offenders)


def test_the_consolidated_helpers_no_longer_build_their_own():
    """Each of these had its own credential wiring; now they ask for a runner."""
    import uta.language.java.generation.commands as generation_commands
    import uta.shared.delivery as auto_push
    import uta.shared.workspace_policy as policy

    for module in (generation_commands, policy, auto_push):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "from uta.shared.git import" in source, module.__name__


def test_the_owner_exists_and_is_the_only_credential_resolver():
    """`env_with_identity` resolved in several places is how one gets forgotten,
    and a forgotten one fails only on the hosts that need it."""
    owner = ROOT / GIT_OWNER
    assert owner.exists()

    # Parsed, not grepped. Matching text found `app/workspace.py`, whose
    # docstring explains why it does *not* build a runner -- prose about a
    # symbol is not a use of it, and a guard that cannot tell the difference
    # trains people to delete the explanation.
    def builds_a_runner(path):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover
            return False
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("agent_core.git"):
                imported.update(a.asname or a.name for a in node.names)
        return bool(imported & {"runner_for", "GitRunner"})

    resolvers = sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "uta").rglob("*.py")
        if builds_a_runner(p)
    )

    assert resolvers == [GIT_OWNER], "credentials built outside the owner: " + ", ".join(resolvers)
