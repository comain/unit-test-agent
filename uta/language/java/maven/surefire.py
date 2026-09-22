"""Making a repository's pinned Surefire skip flag overridable.

Some legacy repositories pin ``<skipTests>true</skipTests>`` in the Surefire
plugin's ``<configuration>``. Plugin configuration beats a command-line
property, so ``mvn test -DskipTests=false`` still skips every test -- silently,
with a one-line ``Tests are skipped.`` and a successful build. Rewriting the
literal to ``${skipTests}`` preserves the repository's default while letting
the CLI override it.

This lives beside the Maven runners rather than in the baseline phase because
it is not a one-time setup step. It was, and that was the bug: beta task 42
relaxed the flag during `baseline_compile`, measured coverage twice, and then
something restored the pom mid-run. The third measurement skipped every test,
reported no coverage, and the task failed at 0% on a target whose coverage had
just been measured at 38.7%. Anything that reverts a working tree -- an agent
tidying a file it judged out of scope, a scope guard, a stash -- puts it back.

So it is applied immediately before each Maven test invocation instead. It is
idempotent and only writes when it changes something.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_PINNED_SKIP = re.compile(
    r"(<artifactId>maven-surefire-plugin</artifactId>.*?<skipTests>)\s*true\s*(</skipTests>)",
    re.DOTALL,
)


def relax_surefire_skiptests(repo_path: str | Path) -> bool:
    """Rewrite pinned Surefire ``skipTests`` flags so CLI properties win."""
    modified = False
    for pom_path in Path(repo_path).rglob("pom.xml"):
        try:
            content = pom_path.read_text(errors="replace")
        except OSError:
            continue
        if "<artifactId>maven-surefire-plugin</artifactId>" not in content:
            continue
        relaxed = _PINNED_SKIP.sub(r"\1${skipTests}\2", content)
        if relaxed != content:
            try:
                pom_path.write_text(relaxed)
            except OSError:
                logger.warning("could not relax surefire skipTests in %s", pom_path)
                continue
            modified = True
            logger.info("Relaxed hardcoded surefire skipTests in %s", pom_path)
    return modified


__all__ = ["relax_surefire_skiptests"]
