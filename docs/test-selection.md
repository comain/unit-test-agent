# Test Target Selection

This document describes how UTA chooses which production classes become unit-test generation targets.

## Scope

Target selection answers three questions:

1. Which Java files should be considered?
2. Which parsed classes are testable enough for UTA to generate tests?
3. Which selected classes should be grouped into the next OpenCode generation batch?

It does not decide method order inside a class. Method-level ordering is handled later by ROI/context artifacts during planning, coverage repair, and mutation repair.

## Workflow Position

Selection runs after branch setup and baseline compile:

```text
setup_branch -> baseline_compile -> scan_and_select -> parse_context -> select_batch -> generate_and_validate
```

The implementation is split across:

- `uta.engine.source_selection.get_changed_java_files`
- `uta.engine.source_selection.filter_files`
- `uta.graph.nodes.scan_and_select`
- `uta.graph.nodes.parse_context`
- `uta.graph.nodes._is_testable_class`
- `uta.graph.nodes.select_next_class`

## Inputs

The CLI options that directly affect target selection are:

| Option | Default | Effect |
|---|---:|---|
| `--repo` | required | Git repo and Maven project root. |
| `--module` | `None` | Restricts git-history scan and parse root to one Maven module. |
| `--days` | `UTA_DEFAULT_DAYS`, default `30` | Git-log lookback window for automatic selection. |
| `--max-files` | `UTA_DEFAULT_MAX_FILES`, default `10` | Number of changed source files to keep before parse/testability filtering. |
| `--all` | `false` | Use all production Java files instead of git-history ranking. Explicit `--class-fqn` still wins. |
| `--class-fqn` | repeatable, empty by default | Explicit class override. Bypasses git-history file selection. |
| `--classes-per-run` / `--batch-size` | `UTA_CLASSES_PER_AGENT_RUN`, default `1` | Number of selected class FQNs to put in one OpenCode generation batch. In production task mode, smart batching can raise this for simple classes. |

Quality gates (`--coverage-gate`, `--mutation-gate`) do not select classes. They are used later to validate and repair generated tests.

## Stage 1: Git-history File Scan

Stage 1 is optional. `scan_and_select` chooses the candidate file source in this order:

1. Explicit `--class-fqn`: skip file scanning and use the provided class FQNs.
2. `--all`: scan every production Java file under the repo/module.
3. Default: rank production Java files by git-history change frequency.

### Default Git-history Mode

When neither `--class-fqn` nor `--all` is provided, `scan_and_select` calls `get_changed_java_files(repo, days, module)`.

The scanner runs:

```bash
git -C <repo> log --since="<days> days ago" --name-only --pretty=format:
```

It keeps a path only if all of these are true:

- The path is non-empty.
- The path ends with `.java`.
- The path contains `src/main/java`.
- If `--module` is set, the path contains `<module>/`.

It excludes test sources implicitly because `src/test/java` does not contain `src/main/java`.

The scanner ranks files by change frequency using `Counter(...).most_common()`. `filter_files(..., max_files)` then truncates to the top `--max-files` paths.

Important behavior:

- Ranking is file-level, not class-level.
- Ties keep the order returned by `Counter.most_common()`, which follows first-seen order from the git log.
- The module filter is currently a substring check for `<module>/`, not a strict path-root check.

### All-files Mode

With `--all`, `scan_and_select` calls `get_all_java_files(repo, module)` and returns every file matching:

```text
<repo>/<module>/**/src/main/java/**/*.java
```

or, without `--module`:

```text
<repo>/**/src/main/java/**/*.java
```

The list is sorted by path for stable output. `--max-files` is ignored in this mode because the user's intent is to inspect or process the complete production-source set.

## Explicit Class Override

When any `--class-fqn` is provided, `scan_and_select` skips git-history selection and returns those FQNs as candidates in the same order provided by the user.

This is the preferred mode for benchmark A/B runs because it prevents recent git activity from changing the selected target set.

Example:

```bash
uta run --repo ~/src/sample-service --module biz \
  --class-fqn com.example.service.PickingService
```

The override is not an unconditional force. During `parse_context`, each explicit FQN must still exist in the parsed graph and pass `_is_testable_class`.

## Stage 2: Parse and Resolve File Paths to FQNs

`parse_context` parses all Java production files under:

```text
<repo>/<module>/**/src/main/java/**/*.java
```

or, without `--module`:

```text
<repo>/**/src/main/java/**/*.java
```

It builds a `CodeGraph`, then creates a `path_to_fqn` map from each parsed class node:

```python
path_to_fqn[node.file_path] = fqn
```

For automatic selection, each scanned relative git path is resolved by trying:

```python
abs_path = str(Path(repo_path) / path)
fqn = path_to_fqn.get(abs_path) or path_to_fqn.get(path)
```

Only paths that resolve to a parsed class FQN can continue.

For explicit selection, each provided class FQN must already be present in `graph.nodes`.

## Stage 3: Testability Filter

`_is_testable_class(fqn, graph)` removes classes that are poor unit-test targets.

It requires:

- A graph node exists for the FQN.
- The node kind is `class`.
- At least one non-private behavior method is present.
- A one-method class is allowed when its name or path looks like business/orchestration code, such as handlers, processors, services, managers, validators, strategies, or `biz` implementations.

It excludes:

- Entry-level wrappers with annotations `Controller`, `RestController`, `DubboService`, or `RequestMapping`.
- Names ending with wrapper/integration/config constants patterns such as `Controller`, `Facade`, `Endpoint`, `Resource`, `RemoteWrapper`, `Wrapper`, `Adapter`, `Proxy`, `Script`, `Tool`, `Migration`, `Patch`, `Fix`, `Config`, `Configuration`, `Properties`, `Constant`, `Constants`, or `Enum`.
- Names containing `backdoor`, `script`, `migration`, `patch`, `tool`, `demo`, or `test`.
- Paths containing `adapter/`, `wrapper/`, `controller/`, `facade/`, `endpoint/`, `script/`, `tool/`, `backdoor/`, or `migration/`.
- Classes marked `abstract`.
- Accessor-only classes where all non-private methods are JavaBean/Object methods such as `getX`, `setX`, `isX`, `equals`, `hashCode`, or `toString`.
- Data-like classes or paths, including DTO/VO/Param/Request/Response/Message/Context/Entity/Model-style names and `model/`, `bean/`, `param/`, `dto/`, `vo/`, `entity/`, `query/`, or `form/` paths, unless they have clear business-code hints and are not accessor-heavy.
- Accessor-backed thin delegators: classes with one trivial no-branch behavior method plus mostly accessors. These usually only dispatch to an injected handler collection; target the concrete handlers instead.
- Thin one-method event wrapper classes under actor/listener/schedule/workflow paths, or ending in `Actor`, `Listener`, `Schedule`, or `Task`. These are usually message-entry delegates; target the injected handler/service instead. They are still eligible when their single behavior method is complex enough by parsed control-flow/body-size signals.

The resulting `state["candidates"]` is a list of class FQNs. Automatic candidates keep the order inherited from git change frequency; explicit candidates keep user-provided order.

## Stage 4: Batch Selection

`select_next_class` chooses the next unprocessed candidates. Non-production runs keep the explicit batch-size behavior:

```python
remaining = [fqn for fqn in candidates if fqn not in results]
batch = remaining[:max(1, classes_per_agent_run)]
```

Production task runs additionally use smart batching when `UTA_SMART_BATCHING_ENABLED=true`:

- A class is complex when its source is missing, has at least `UTA_SMART_COMPLEX_LINE_THRESHOLD` lines (default `100`), or has at least `UTA_SMART_COMPLEX_PUBLIC_METHOD_THRESHOLD` public methods (default `4`).
- If the first remaining class is complex, the batch contains only that class.
- If the first remaining class is simple, following simple classes are added up to `UTA_SMART_SIMPLE_BATCH_SIZE` (default `3`), unless the user set `--classes-per-run` above `1`, in which case that explicit cap is respected up to `3`.

It returns:

- `current_batch`: the selected FQNs.
- `current_class`: the first FQN in the batch.
- `finished`: `true` when every candidate already has a result.

After each batch completes, `commit_to_branch` stages only generated test files for the current batch plus `.uta_cache/`, then the workflow loops back to `select_next_class`.

## What Does Not Affect Class Selection

These mechanisms affect planning or repair after a class has already been selected:

- Coverage ROI (`compute_class_roi`) ranks methods inside the target class.
- Plan breadth and plan feasibility validators judge whether the generated plan can plausibly reach the coverage gate.
- Coverage-fix and mutation-fix ROI rank uncovered methods or surviving mutant families.
- OpenCode model/provider routing changes execution behavior but not candidate selection.

## Known Gaps

Current target selection is intentionally simple, but the behavior has sharp edges:

- Git frequency can select high-churn plumbing before high-value business logic.
- `--max-files` is applied before parse/testability filtering, so a run can end up with fewer targets than requested after filtering.
- The module filter uses substring matching (`"<module>/" in path`), which can match unintended paths if module names overlap.
- Automatic selection does not currently use class complexity, existing coverage, mutation history, ROI, ownership, or prior run failures.
- Explicit `--class-fqn` still passes through the testability filter; this is safe by default but means a user-provided target can be silently filtered with only log output.

## Recommended Use

Use automatic git-history selection for exploratory runs:

```bash
uta run --repo ~/src/sample-service --module biz --days 30 --max-files 10
```

Use all-files mode for selection audits:

```bash
uta run --repo ~/src/sample-service --module biz --all
```

Use explicit selection for benchmarks, regressions, and model comparisons:

```bash
uta run --repo ~/src/sample-service --module biz \
  --class-fqn com.example.service.PickingService \
  --coverage-gate 80 \
  --mutation-gate 70
```

Use small batches for difficult legacy classes:

```bash
uta run --repo ~/src/sample-service --module biz --classes-per-run 1
```

Increase `--classes-per-run` only when the classes are small and the model context budget is known to be safe.
