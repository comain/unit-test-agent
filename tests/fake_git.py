"""A stand-in `git` binary for tests that must not reach a real remote.

`GitWorkspaceManager` used to accept an injected `run_command`, so a test
substituted a Python function and read the command lists it was handed. It now
runs git through `agent_core.git.GitWorkspace`, which starts the process
itself -- deliberately, because what that class adds is a poll loop and a
process-group kill, and a callable seam would bypass exactly the behaviour
worth having.

So the seam moved down to the executable. This writes a small program that
answers as git would, records every invocation, and never talks to a network.

`seed_commit` makes the fake `clone` build a *real* repository at the
destination, with the base ref one commit behind HEAD. That matters for the
parts of the service that do not go through the workspace manager at all --
commit-message context reads the checkout with the shared runner and the real
git binary -- so a checkout that does not exist reports "unavailable" rather
than testing anything.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

#: Resolved now, while the fake is not yet on anyone's PATH.
REAL_GIT = shutil.which("git") or "/usr/bin/git"


def fake_git(
    tmp_path: Path,
    *,
    log: Optional[Path] = None,
    rev: str = "a" * 40,
    responses: Optional[Dict[str, str]] = None,
    fail_on: Optional[Dict[str, str]] = None,
    fail_first: Optional[Dict[str, str]] = None,
    seed_commit: str = "",
    sleep: float = 0.0,
    seed_base_ref: str = "refs/remotes/origin/master",
    name: str = "git",
) -> str:
    """Write a fake git and return its path.

    `responses` maps a subcommand to the stdout it should produce; `rev` is the
    shorthand for the common case of `rev-parse`. `fail_on` maps a subcommand
    to the stderr it should fail with, for tests about error handling, and
    `fail_first` does so for its first invocation only, for tests about retry.
    """
    bin_dir = tmp_path / "fake-git-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    script = f"""#!{sys.executable}
import json, os, subprocess, sys

argv = sys.argv[1:]
LOG = {repr(str(log)) if log is not None else "None"}
if LOG:
    # argv plus the git-relevant environment, which is how credentials reach
    # git and therefore what a test about credentials has to look at.
    record = {{"argv": argv,
              "env": {{k: v for k, v in os.environ.items() if k.startswith("GIT_")}}}}
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\\n")

# The subcommand is the first argument that is not `-C <path>`.
rest = argv[2:] if argv[:1] == ["-C"] else argv
sub = rest[0] if rest else ""

SLEEP = {json.dumps(sleep)}
if SLEEP:
    import time
    time.sleep(SLEEP)

FAILURES = {json.dumps(fail_on or {})}
if sub in FAILURES:
    sys.stderr.write(FAILURES[sub])
    sys.exit(128)

# Fail the first invocation of a subcommand only, so a test can watch a
# transient failure recover on the retry rather than merely be repeated.
FAIL_FIRST = {json.dumps(fail_first or {})}
if sub in FAIL_FIRST:
    seen = {repr(str(tmp_path / "fake-git-seen"))}
    marks = set()
    if os.path.exists(seen):
        with open(seen, encoding="utf-8") as handle:
            marks = set(handle.read().split())
    if sub not in marks:
        with open(seen, "a", encoding="utf-8") as handle:
            handle.write(sub + "\\n")
        sys.stderr.write(FAIL_FIRST[sub])
        sys.exit(128)

if sub == "clone":
    # Real git creates the destination, and a command that runs in it next
    # would otherwise fail for a reason the test never meant to arrange.
    os.makedirs(rest[-1], exist_ok=True)

SEED = {json.dumps(seed_commit)}
if sub == "clone" and SEED:
    dest = rest[-1]
    real = {json.dumps(REAL_GIT)}

    def g(*args):
        subprocess.run([real, "-C", dest, *args], check=True, capture_output=True)

    os.makedirs(dest, exist_ok=True)
    subprocess.run([real, "init", "-q", dest], check=True, capture_output=True)
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    with open(os.path.join(dest, "README.md"), "w") as handle:
        handle.write("base\\n")
    g("add", "-A")
    g("commit", "-qm", "base")
    g("update-ref", {json.dumps(seed_base_ref)}, "HEAD")
    with open(os.path.join(dest, "changed.txt"), "w") as handle:
        handle.write("work\\n")
    g("add", "-A")
    g("commit", "-qm", SEED)
    sys.exit(0)

ANSWERS = {json.dumps({"rev-parse": rev, **(responses or {})})}
if sub in ANSWERS:
    sys.stdout.write(ANSWERS[sub] + "\\n")
sys.exit(0)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def records(log: Path) -> List[dict]:
    """Every git invocation recorded: its argv and its git environment."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def calls(log: Path) -> List[List[str]]:
    """Every git invocation recorded, as argv lists without the binary."""
    return [record["argv"] for record in records(log)]


def envs(log: Path) -> List[dict]:
    """The git environment each invocation ran with."""
    return [record["env"] for record in records(log)]


def subcommands(log: Path) -> List[str]:
    """Just the subcommand of each invocation, in order."""
    out = []
    for argv in calls(log):
        rest = argv[2:] if argv[:1] == ["-C"] else argv
        out.append(rest[0] if rest else "")
    return out
