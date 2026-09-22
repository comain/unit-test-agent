"""Task-management command group for the UTA CLI.

This module registered thirty commands in one 887-line function. The commands
never shared state -- only the group they hang off and a few formatting
helpers -- so the function was long for no reason other than that nobody had
split it.

What is left here is the seam itself: the two Click groups every task command
attaches to, and the injected helpers the CLI passes down. The commands live
in `uta.app.commands.tasks`, one module per thing an operator does. Both
groups are created here rather than in a command module because they are what
the modules have in common; creating `tasks` inside `create.py` would make
every other module depend on the one that happened to be registered first.

`register_task_commands`, `prepare_cli_clone` and `legacy_clone_path` keep
this import path because the CLI and the tests use it.
"""

from __future__ import annotations

from uta.app.commands.tasks import (
    legacy_clone_path,
    prepare_cli_clone,
    register_control_commands,
    register_create_commands,
    register_inspect_commands,
    register_reporting_commands,
    register_retention_commands,
)


def register_task_commands(
    main,
    *,
    console,
    cli_git_timeout,
    fmt_int,
    fmt_optional_float,
    fmt_seconds,
    manifest_targets,
    normalize_cli_targets,
    resolve_cli_language,
    task_budget_snapshot,
    task_config_snapshot,
) -> None:
    """Attach the complete tasks command family to main."""

    @main.group("tasks")
    def tasks_group():
        """Manage production UTA repo/class tasks."""
        pass

    register_create_commands(
        tasks_group,
        console=console,
        cli_git_timeout=cli_git_timeout,
        manifest_targets=manifest_targets,
        normalize_cli_targets=normalize_cli_targets,
        resolve_cli_language=resolve_cli_language,
        task_budget_snapshot=task_budget_snapshot,
        task_config_snapshot=task_config_snapshot,
    )
    register_inspect_commands(
        tasks_group,
        console=console,
        fmt_int=fmt_int,
        fmt_optional_float=fmt_optional_float,
        fmt_seconds=fmt_seconds,
    )
    register_control_commands(
        tasks_group,
        console=console,
        task_config_snapshot=task_config_snapshot,
    )
    register_retention_commands(tasks_group, console=console)
    register_reporting_commands(tasks_group, console=console)


__all__ = ["legacy_clone_path", "prepare_cli_clone", "register_task_commands"]
