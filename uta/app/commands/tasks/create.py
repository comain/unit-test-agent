"""Commands that bring work into the task system.

`create` and `create-manifest` take a repository already on this machine;
`enqueue` takes a Git URL and produces the checkout first. That clone is the
reason this group owns two module-level helpers: enqueueing a private
repository has to authenticate the same way every other clone in UTA does, and
`legacy_clone_path` exists only so a task still running against the pre-
agent-core directory name is not enqueued a second time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click

from uta.shared.config import settings


def prepare_cli_clone(
    git_url: str,
    *,
    branch: str = "",
    timeout: Optional[float] = None,
    clone_root: Optional[Path] = None,
) -> Path:
    """Clone or update the checkout `tasks enqueue` will queue, and return it.

    This ran `subprocess.run(["git", "clone", git_url, dest])` with no
    environment, so git authenticated with whatever ambient ssh identity the
    operator happened to have. This deployment authenticates with an access
    token, and a token reaches git two ways at once: an `extraheader` in the
    environment, and an `https://` URL for it to apply to. Neither was here, so
    enqueueing a private repository failed on any host without a usable key.

    It is one call now, because clone-or-update *is* an operation agent-core
    states -- along with rewriting the URL for the token, repointing a checkout
    whose remote predates a credential change, refusing a "branch" that is
    really an option, and asking the remote for its default branch when none
    was given. Running `clone` and `fetch` here and reassembling those around
    them is how they end up subtly different from every other clone we do.

    The checkout lands at agent-core's path for the repository, which drops the
    `.git` the old local slug rule kept: `<clone_root>/demo` where it was
    `<clone_root>/demo.git`. Existing checkouts are left where they are and
    re-cloned once under the new name; `legacy_clone_path` is what still finds
    the old one, so an already-running task on it is not enqueued twice.
    """
    from uta.shared.git import git_workspace

    workspace = git_workspace(Path(clone_root or settings.clone_root), timeout=timeout)
    # An empty branch means the remote's default, which is what the CLI has
    # always done when `--branch` is omitted.
    return workspace.prepare(git_url, branch=branch or None, local_branch=True)


def legacy_clone_path(git_url: str, clone_root: Optional[Path] = None) -> Path:
    """Where this CLI used to put a checkout, before agent-core owned the path.

    Kept only so a task still running against the old directory is found.
    """
    import re

    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", git_url.rstrip("/").split("/")[-1]).strip("-") or "repo"
    return Path(clone_root or settings.clone_root) / slug


def register_create_commands(
    tasks_group,
    *,
    console,
    cli_git_timeout,
    manifest_targets,
    normalize_cli_targets,
    resolve_cli_language,
    task_budget_snapshot,
    task_config_snapshot,
) -> None:
    """Attach `create`, `create-manifest` and `enqueue` to the tasks group."""
    _CLI_GIT_TIMEOUT = cli_git_timeout
    _manifest_targets = manifest_targets
    _normalize_cli_targets = normalize_cli_targets
    _resolve_cli_language = resolve_cli_language
    _task_budget_snapshot = task_budget_snapshot
    _task_config_snapshot = task_config_snapshot

    @tasks_group.command("create")
    @click.option("--repo", required=True, type=click.Path(exists=True), help="Path to the repository")
    @click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
    @click.option("--module", default=None, help="Target Maven module name")
    @click.option("--class-fqn", "class_fqns", multiple=True, help="Class FQN to include. Repeat for multiple classes.")
    @click.option("--target", "targets", multiple=True, help="Language-neutral target. For Python use path.py or path.py::symbol.")
    @click.option("--all", "select_all", is_flag=True, help="Select all production classes during execution")
    @click.option("--priority", default=100, show_default=True, type=int, help="Lower number runs first")
    @click.option("--branch-name", default=None, help="Explicit generation branch. Defaults to same-repo active branch reuse.")
    @click.option("--new-branch", is_flag=True, help="Force a new branch instead of reusing the active repo branch")
    @click.option("--base-ref", default="origin/master", show_default=True, help="Base ref recorded for branch reuse")
    @click.option("--coverage-gate", default=settings.coverage_gate, type=float, help="Target coverage percentage")
    @click.option("--mutation-gate", default=settings.mutation_gate, type=float, help="Target mutation score")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_create(repo, language, module, class_fqns, targets, select_all, priority, branch_name, new_branch, base_ref, coverage_gate, mutation_gate, task_db):
        from uta.tasks.manager import TaskManager

        manager = TaskManager(task_db)
        decision = _resolve_cli_language(repo, language, class_fqns=class_fqns, targets=targets)
        if decision.language == "java" and not targets:
            task_id = manager.create_task(
                repo_path=repo,
                module=module,
                class_fqns=class_fqns,
                select_all=select_all,
                priority=priority,
                branch_name=branch_name,
                new_branch=new_branch,
                base_ref=base_ref,
                coverage_gate=coverage_gate,
                mutation_gate=mutation_gate,
                config_snapshot=_task_config_snapshot(),
                budget_snapshot=_task_budget_snapshot(),
            )
        else:
            if class_fqns and decision.language != "java":
                raise click.ClickException("--class-fqn can only be used with Java targets")
            raw_targets = list(targets or ())
            if decision.language == "java":
                raw_targets.extend({"class_fqn": class_fqn} for class_fqn in class_fqns)
            target_refs = _normalize_cli_targets(decision.language, raw_targets)
            task_id = manager.create_task_targets(
                repo_path=repo,
                module=module,
                targets=target_refs,
                select_all=select_all,
                priority=priority,
                branch_name=branch_name,
                new_branch=new_branch,
                base_ref=base_ref,
                coverage_gate=coverage_gate,
                mutation_gate=mutation_gate,
                config_snapshot=_task_config_snapshot(),
                budget_snapshot=_task_budget_snapshot(),
                language=decision.language,
            )
        task = manager.get_task(task_id)
        manager.write_live_status(task_id)
        console.print(f"[green]Created task {task_id}[/green] branch={task['branch_name']} db={manager.db_path}")

    @tasks_group.command("create-manifest")
    @click.option("--manifest", "manifest_path", required=True, type=click.Path(exists=True, dir_okay=False), help="JSON manifest with repo task definitions")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_create_manifest(manifest_path, task_db):
        """Create tasks from a JSON manifest.

        Accepted shapes:
        {"tasks": [{"repo": "/repo", "module": "biz", "class_fqns": [...], "all": false, "priority": 100}]}
        or a top-level list of the same task objects.
        """
        from uta.tasks.manager import TaskManager

        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        items = raw.get("tasks") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise click.ClickException("Manifest must be a JSON list or an object with a 'tasks' list")
        manager = TaskManager(task_db)
        created = []
        for item in items:
            if not isinstance(item, dict) or not item.get("repo"):
                raise click.ClickException("Each manifest task must include a repo path")
            class_fqns = item.get("class_fqns") or item.get("classes") or []
            targets = _manifest_targets(item)
            decision = _resolve_cli_language(
                item["repo"],
                item.get("language", "auto"),
                class_fqns=class_fqns,
                targets=[target if isinstance(target, str) else (target.get("target") or target.get("target_id") or target.get("sourcePath") or target.get("source_path") or "") for target in targets],
            )
            if decision.language == "java" and not targets:
                task_id = manager.create_task(
                    repo_path=item["repo"],
                    module=item.get("module"),
                    class_fqns=class_fqns,
                    select_all=bool(item.get("all") or item.get("select_all")),
                    priority=int(item.get("priority", 100)),
                    branch_name=item.get("branch_name"),
                    new_branch=bool(item.get("new_branch")),
                    base_ref=item.get("base_ref", "origin/master"),
                    coverage_gate=item.get("coverage_gate", settings.coverage_gate),
                    mutation_gate=item.get("mutation_gate", settings.mutation_gate),
                    config_snapshot=_task_config_snapshot(),
                    budget_snapshot=_task_budget_snapshot(),
                    estimate_snapshot=item.get("estimate"),
                )
            else:
                if class_fqns and decision.language != "java":
                    raise click.ClickException("Manifest class_fqns can only be used with Java targets")
                raw_targets = list(targets)
                if decision.language == "java":
                    raw_targets.extend({"class_fqn": class_fqn} for class_fqn in class_fqns)
                task_id = manager.create_task_targets(
                    repo_path=item["repo"],
                    module=item.get("module"),
                    targets=_normalize_cli_targets(decision.language, raw_targets),
                    select_all=bool(item.get("all") or item.get("select_all")),
                    priority=int(item.get("priority", 100)),
                    branch_name=item.get("branch_name"),
                    new_branch=bool(item.get("new_branch")),
                    base_ref=item.get("base_ref", "origin/master"),
                    coverage_gate=item.get("coverage_gate", settings.coverage_gate),
                    mutation_gate=item.get("mutation_gate", settings.mutation_gate),
                    config_snapshot=_task_config_snapshot(),
                    budget_snapshot=_task_budget_snapshot(),
                    estimate_snapshot=item.get("estimate"),
                    language=decision.language,
                )
            created.append(task_id)
            manager.write_live_status(task_id)
        console.print(f"[green]Created {len(created)} task(s): {', '.join(map(str, created))}[/green]")

    @tasks_group.command("enqueue")
    @click.argument("git_url")
    @click.option("--language", default="auto", show_default=True, help="Project language: auto, java, or python")
    @click.option("--module", default=None, help="Target Maven module")
    @click.option("--target", "targets", multiple=True, help="Language-neutral target. For Python use path.py or path.py::symbol.")
    @click.option("--all", "select_all", is_flag=True, help="Select all targets during execution")
    @click.option("--branch", default=None, help="Git branch to check out")
    @click.option("--priority", default=100, type=int, show_default=True, help="Task priority (lower = sooner)")
    @click.option("--hard-cap-usd", default=None, type=float, help="Abort task when spend exceeds this USD amount")
    @click.option("--task-db", default=None, type=click.Path(dir_okay=False), help="SQLite task DB path")
    def task_enqueue(git_url, language, module, targets, select_all, branch, priority, hard_cap_usd, task_db):
        """Clone (or update) a repo and add it to the task queue."""
        from uta.tasks.manager import TaskManager

        console.print(f"[dim]Preparing {git_url}[/dim]")
        # Bounded like the rest. A human can interrupt this one, but a clone
        # over a stalled link otherwise blocks with no diagnostic at all.
        dest = prepare_cli_clone(git_url, branch=branch or "", timeout=_CLI_GIT_TIMEOUT)
        console.print(f"[dim]Ready: {dest}[/dim]")

        manager = TaskManager(task_db)
        # Skip if a RUNNING task for this path already exists. The old path is
        # checked too: a task enqueued before agent-core owned the layout is
        # recorded against `<clone_root>/<repo>.git` and is still running.
        with manager.db.connect() as _conn:
            running = _conn.execute(
                "SELECT id FROM repo_tasks WHERE repo_path IN (?, ?) AND status='RUNNING' LIMIT 1",
                (str(dest), str(legacy_clone_path(git_url))),
            ).fetchone()
        if running:
            console.print(f"[yellow]Task already RUNNING for {dest} (id={running['id']}); skipping enqueue[/yellow]")
            return

        decision = _resolve_cli_language(str(dest), language, targets=targets)
        if decision.language == "java" and not targets:
            task_id = manager.create_task(
                repo_path=str(dest),
                module=module,
                select_all=select_all,
                priority=priority,
                branch_name=branch,
                hard_cap_usd=hard_cap_usd,
            )
        else:
            task_id = manager.create_task_targets(
                repo_path=str(dest),
                module=module,
                targets=_normalize_cli_targets(decision.language, targets),
                select_all=select_all,
                priority=priority,
                branch_name=branch,
                hard_cap_usd=hard_cap_usd,
                language=decision.language,
            )
        manager.start_task(task_id)
        console.print(f"[green]Enqueued task {task_id} for {dest}[/green]")
