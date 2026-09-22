"""Write resolved Java symbols back into the context a repair turn reads.

Compile failures name symbols the model could not resolve. Resolving them once
in Python and appending the candidates to the class ``.symbols.md`` and the
project compile facts is what stops the next fix turn from re-deriving them.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional

from uta.language.java.compile import classify_compile_errors
from uta.language.java.symbol_resolver import format_candidates_markdown, resolve_symbols
from uta.testgen.project_summary_artifacts import merge_compile_fix_facts

logger = logging.getLogger("uta")


def _writeback_resolved_symbols(
    compile_errors: str,
    repo_path: str,
    class_fqn: str,
    symbols_abs: Optional[str],
) -> Dict[str, List[str]]:
    """Resolve unresolved symbols from compile errors and write candidates into
    the class-level .symbols.md and project compile_facts.md so the next fix
    iteration doesn't need to grep for the same types (strategy C).
    """
    from uta.language.java.compile import CATEGORY_UNRESOLVED_SYMBOL
    errors = classify_compile_errors(compile_errors)
    unresolved = [
        e.symbol for e in errors
        if e.category == CATEGORY_UNRESOLVED_SYMBOL and e.symbol
    ]
    if not unresolved:
        return {}

    package = class_fqn.rsplit(".", 1)[0] if "." in class_fqn else ""
    resolutions = resolve_symbols(
        unresolved,
        repo_path,
        target_package=package,
        limit_per_symbol=3,
    )
    if not any(resolutions.values()):
        return {}

    md_block = format_candidates_markdown(resolutions)

    # Append to class-level .symbols.md
    if symbols_abs:
        sym_path = Path(symbols_abs)
        if sym_path.exists():
            existing = sym_path.read_text(encoding="utf-8")
            if "## Python-Resolved Candidates" not in existing:
                sym_path.write_text(
                    existing.rstrip() + "\n\n## Python-Resolved Candidates\n\n" + md_block,
                    encoding="utf-8",
                )
            else:
                # Replace the section
                pre, _, _ = existing.partition("## Python-Resolved Candidates")
                sym_path.write_text(
                    pre.rstrip() + "\n\n## Python-Resolved Candidates\n\n" + md_block,
                    encoding="utf-8",
                )
            logger.debug("[%s] Wrote %d resolved symbols to %s", class_fqn, len(resolutions), sym_path)

    # Append terse import candidates to compile_facts.md
    facts = [
        f"Unresolved `{name}` candidates: " + ", ".join(f"`{c.fqn}`" for c in candidates)
        for name, candidates in resolutions.items()
        if candidates
    ]
    if facts:
        merge_compile_fix_facts(repo_path, facts)
    return {
        name: [candidate.fqn for candidate in candidates]
        for name, candidates in resolutions.items()
        if candidates
    }







__all__ = ["_writeback_resolved_symbols"]
