"""Read-only readiness check for repository-declared UTA agent rules."""

from pathlib import Path


class WorkspaceRulesUnavailable(ValueError):
    """A workspace prerequisite failed, not a model or test-quality failure."""

    reason_code = "workspace_rules_unavailable"


def validate_workspace_rules(repo_path: Path) -> None:
    """Reject broken rule installations before any language/backend spends tokens.

    Do not provision rules, rewrite tracked symlinks, or ignore project policy.
    Repositories without a rule installation/declaration remain unaffected.
    """
    repo = Path(repo_path)
    rules = repo / ".rd_rule"
    required = ("agent/agent_develop_rule.md", "code_rule.md")
    if not rules.exists() and not rules.is_symlink():
        guide = repo / "AGENTS.md"
        if not guide.is_file():
            return
        with guide.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(128 * 1024)
        if not any(".rd_rule/" + name in text for name in required):
            return
    missing = []
    for name in required:
        path = rules / name
        try:
            with path.open("rb") as handle:
                handle.read(1)
        except (OSError, RuntimeError):
            missing.append(".rd_rule/" + name)
    if missing:
        destination = str(rules.readlink()) if rules.is_symlink() else str(rules)
        raise WorkspaceRulesUnavailable(
            "workspace_rules_unavailable: Required repository rules are missing or unreadable: "
            + ", ".join(missing)
            + f". Rule location: {destination}. Restore the repository's approved rules at a "
            "path accessible on this execution host, then retry. No LLM work was started; "
            "changing the model or effort will not resolve this prerequisite."
        )
