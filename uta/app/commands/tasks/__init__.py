"""The `uta tasks` command family, one module per thing an operator does.

`task_commands.py` registered all thirty of these in a single 887-line
function, so the only way to find the command you wanted was to scroll past
the other twenty-nine. The commands themselves are independent -- they share
the group they hang off and a handful of formatting helpers, nothing else --
so the split is by what an operator is trying to do:

- `create`    -- bring work into the system (create, create-manifest, enqueue)
- `inspect`   -- look at it without changing it (list, show, watch, summary,
                 export, dashboard, audit-generation-cutover)
- `control`   -- change its execution state (start, daemon, stop, resume,
                 cancel, unblock, reprioritize*)
- `retention` -- delete or redo past work (clean-rerun-generation,
                 prune-workflows)
- `reporting` -- the `tasks report` subgroup (batch, repo)

Nothing about the CLI contract moved: every command name, flag, default and
help string is the one it had, and `uta.app.task_commands` remains the import
the CLI and the tests use.
"""

from __future__ import annotations

from uta.app.commands.tasks.control import register_control_commands
from uta.app.commands.tasks.create import (
    legacy_clone_path,
    prepare_cli_clone,
    register_create_commands,
)
from uta.app.commands.tasks.inspect import register_inspect_commands
from uta.app.commands.tasks.reporting import register_reporting_commands
from uta.app.commands.tasks.retention import register_retention_commands

__all__ = [
    "legacy_clone_path",
    "prepare_cli_clone",
    "register_control_commands",
    "register_create_commands",
    "register_inspect_commands",
    "register_reporting_commands",
    "register_retention_commands",
]
