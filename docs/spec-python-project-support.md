# Spec: Python Project Support

Status: Draft, pending final review before implementation.

Jira status: Confirmed non-Jira tool work. Do not require a Jira key for this spec or implementation unless the user later changes scope.

Original request: Refactor UTA, which currently supports only Java backend projects, to support Python projects. Explore `/home/user/md/store_sku_prediction_model` and Python unit-testing best practices.

## Assumptions

1. Python support should be added alongside Java support, not as a replacement for existing Java/Maven behavior.
2. The first Python target is UTA-style data/ML script repositories, including flat module layouts like `store_sku_prediction_model`, not only packaged `src/` applications.
3. The Python default test framework should be `pytest`, using `unittest.mock` and pytest fixtures/monkeypatching for isolation.
4. Python coverage should use `coverage.py` line and branch coverage. Python mutation testing is required in the first Python release, with `mutmut` as the initial concrete backend.
5. UTA should generate tests only under a project test directory and should not edit production Python files unless a future explicit `--fix-code` flow is implemented for Python.
6. Python parsing should be Tree-sitter-first. Python `ast` may be used only as optional enrichment for clean Python 3 files.
7. Python support must classify Python 2 vs Python 3 syntax before generation. Several target repos still contain Python 2 scripts that cannot be parsed with Python 3 `ast`, but Tree-sitter can still provide partial syntax structure and error nodes for source-only discovery and routing to a legacy mutation lane.

## Resolved Decisions

1. This remains non-Jira tool work.
2. Python target granularity supports both module/file-level and function-level targets. CI incremental repair should prefer function-level targets when the diff can be mapped safely.
3. CLI language handling should use auto-detection by default, with explicit override only when needed.
4. `coverage.py`, `pytest`, `tree-sitter-python`, and modern `mutmut` are UTA/runtime dependencies for Python support.
5. Python mutation gate defaults should start at the same threshold as Java.
6. UTA should maintain a configured Python 2.7 legacy runtime for Python 2 targets rather than requiring each invocation to provide one manually.
7. Python coverage gate defaults should start at the same threshold as Java.
8. target repos should default generated tests to `tests/uta_generated/`.
9. RDC should detect the project language and send it to the UTA API trigger as a parameter.
10. Python CI base ref defaults to `origin/master` for all target repos.
11. Python CI enforcement and dev-skills local enforcement must use the same source code. The canonical implementation lives in the UTA repo under `uta/enforcement/python/`; dev-skills provides only a thin launcher that delegates to `uta python-enforce`.
12. Do not plan an optional distributed Python package for repo-local enforcement in this spec.
13. Python enforcement evidence must include the UTA enforcement core version and UTA version; when invoked through dev-skills, it must also include the dev-skills launcher version.
14. Flat target repos should default to running generated tests from repo root with `python -m pytest`; generate a scoped `tests/uta_generated/conftest.py` only when import-path setup or shared generated fixtures are required.

## Objective

Add language-aware UTA support so `uta run --repo <python-repo>` can discover Python production files, build useful per-target context, generate pytest tests, run deterministic verification, collect coverage, and produce reports using the existing UTA workflow shape.

Success means a Python repository can be processed without any Maven, JaCoCo, Pitest, `src/main/java`, Java FQN, or JUnit assumptions leaking into the Python path.

The design doc contains a high-level before/after architecture diagram showing the current Java-only backend and the target multi-language backend with `LanguageAdapter`, `TargetIdentity`, Java/Python verification lanes, CI enforcement routing, and shared task/report/cost contracts.

The design should also keep the abstraction general enough that a third backend can be added without another broad rewrite. Shared task, report, progress, cost, CI repair, evidence, and dev-skills code should consume language-neutral `TargetIdentity`, `LanguageAdapter`, `VerificationResult`, and enforcement evidence contracts. Java and Python specifics should stay inside adapters, prompt bundles, verification runners, and enforcement runners.

Parsing should use a separate engine-level `ParseProvider` contract. Workflow and CLI code should call `make_parse_provider(language)`; parser packages such as `uta/language/java/parse` and `uta/language/python/parse` remain language implementation details behind the engine contract.

To make that practical, the implementation should include a backend registry and generic enforcement entrypoint. `uta enforce --language <lang>` is the neutral command; `uta python-enforce` may exist as a compatibility alias. CI routing should be table-driven by `language -> backend -> EnforcementRunner`, not hardcoded Java/Python branches in shared service code.

The registry should also own capability lookup and generated-test policy lookup. Shared code should ask the selected backend whether function-level targets, mutation, branch coverage, incremental diff enforcement, generated fixtures, and auto-push allowlists are supported, instead of encoding Java/Python feature assumptions. This keeps a later third backend limited to adding an adapter, enforcement runner, prompts, placement policy, capability declaration, and fixtures.

Python support must cover both UTA operating modes:

1. Batch-oriented generation tasks: repo/task queue jobs that select one or many Python targets, generate tests, run pytest/coverage/mutation loops, and publish UTA reports.
2. CI incremental gate and repair: an RDC API trigger path that checks diff-based coverage and mutation evidence for changed Python production files, then creates an urgent incremental UTA repair task only when a user asks for generated/improved tests.

## Current State

UTA is currently Java-specific in several core areas:

- CLI copy and options describe Java repositories, Maven modules, class FQNs, and production Java files.
- Candidate scanning selects `.java` paths under `src/main/java`.
- Context parsing depends on the Java parse binding, `tree-sitter-java`, Java symbols, class graphs, and process flows.
- Prompt templates require JUnit 4, Mockito, Maven commands, Java source lookup, and Java test paths.
- Test placement is derived as `src/test/java/<package>/<ClassName>Test.java`.
- Verification uses Maven compile/test, JaCoCo XML, and Pitest mutation reports.
- Reports and task rows use `class_fqn`, Java class terminology, and mutation buckets modeled after PIT.
- The RDC API trigger currently delegates diff coverage and mutation enforcement to a Maven test-enforcement backend hooked through Java root POM/profile configuration.
- CI-triggered repair tasks currently store Java class FQNs, `quality_mode=ci_incremental`, and `quality_gate_backend=maven_enforcer`, then rerun the Maven enforcer after the repair task completes.

## Sample Python Project Findings

Sample repo: `/home/user/md/store_sku_prediction_model`

Observed structure:

- Branch: `master`, with unrelated untracked `.antigravitycli/`.
- Top-level Python files: 43.
- Shell entrypoints: 18.
- No detected `tests/`, `test_*.py`, `*_test.py`, `pytest.ini`, `pyproject.toml`, `tox.ini`, or `noxfile.py`.
- `setup.cfg` only configures `pycodestyle` and `yapf`, both at 80-column width.
- `requirements.txt` is pinned and heavy: `pandas`, `numpy`, `catboost`, `tensorflow`, `pyarrow`, plotting libraries, notebooks, and utility libraries.
- The layout is flat: modules import each other directly by top-level names such as `from config import ...`.
- Many modules have external side effects: Hive/HDFS subprocess calls, network requests, filesystem writes, model loading, plotting, email/IM notifications.

High-value unit-test targets in this sample are pure or mostly pure functions:

- Date/index calculations in `config_resolver.py` and `validate_script.py`.
- Table/key/config helper functions in `config.py`, `io_data_util.py`, and `generate_sql.py`.
- DataFrame transformations in `data_util.py`, `input_sanity_check.py`, `evaluatedata.py`, and `prediction_alert.py`.
- Feature tuple/list expansion in `feature_util.py`.

Risky first-pass targets are script entrypoints and functions that immediately call Hive, HDFS, IM, email, CatBoost/TensorFlow model loading, or plotting. These require targeted patching and small fake DataFrames/files.

## Additional `~/md` Python Project Findings

Scanned `/home/user/md` for Python project markers and `.py` files. Primary Python candidates:

| Path | Shape | Notes |
| --- | --- | --- |
| `/home/user/md/store_sku_prediction_model` | Flat data/ML script repo | 43 top-level `.py` files, 18 shell entrypoints, no tests discovered. |
| `/home/user/md/strategy-job` | Strategy job platform repo | 562 first-party `.py` files after excluding `.venv`; 514 `.py` files under `jobs/`; 85 `.job`, 181 `.sql`, 12 `.hsql`; 3 test-like files. |
| `/home/user/md/mddf-job` | Legacy data job repo | 754 first-party `.py` files after excluding `lib/`; 721 `.py` files under `jobs/`; 1984 `.job`, 1716 `.sql`; 10 test-like files; many Python 2 scripts. |
| `/home/user/md/query_status.py` | Standalone utility | Direct SQLExchange HTTP helper, reads `/tmp/corp_cookie.txt`, posts to corp DBA endpoint. |

Excluded from primary stats:

- `/home/user/md/.worktrees`, because it is hidden worktree storage rather than a top-level project target.
- `/home/user/md/strategy-job/.venv`, because it is a local virtualenv.
- `/home/user/md/mddf-job/lib/py`, because it contains vendored third-party packages such as Selenium and their own tests.

`strategy-job` observations:

- Readme describes a strategy-computation platform where `jobs/` contains `.job` and `.py` executable scripts, `bin/` contains run/check tooling, `conf/` contains runtime configuration, and `util/` contains shared helpers.
- Job execution is path-based, for example `./bin/run-task-debug.sh python app_test/a`, not package-entrypoint based.
- Imports commonly use top-level repo modules like `util`, `jobs`, and `job_config`.
- A Python 3 `ast` scan found about 3497 functions and 153 classes in first-party files.
- Only 4 files failed Python 3 `ast` parsing, all under `util/py2`, which confirms mixed Python version support is needed but most first-party code is Python 3-compatible.
- Existing `*_test.py` files are job/strategy scripts, not necessarily pytest suites.
- Useful unit-test targets include pure date helpers in `util`, parsers such as `jobs/app_ad/measure/parse_fee_rules.py`, small data utilities such as `jobs/app_daily_down_sku_pool/data_util.py`, and text/file helpers in `util/tsvh_file.py`.

`mddf-job` observations:

- First-party code is much larger and older than `strategy-job`.
- A Python 3 `ast` scan found about 3393 parseable functions and 128 classes, but 212 first-party files failed Python 3 parsing, mostly due to Python 2 `print` syntax and `sys.setdefaultencoding`.
- Many files execute runtime work at import time: environment reads, `DATE` parsing, global DataFrame initialization, stdout mutation, and database connection constants.
- Test-like files under `jobs/.../*_test.py` look like scheduled validation or data job scripts. They import Hive/MySQL/HTTP helpers and set globals, so UTA should not assume existing `*_test.py` files are pytest examples.
- The repo has a vendored `lib/py` subtree with third-party package tests configured by its own `setup.cfg`; UTA should skip vendored/library trees unless the user explicitly targets them.

Cross-project implications:

- Candidate scanning must support include/exclude rules for `jobs/`, `util/`, `bin/`, `etc/`, flat roots, vendored trees, virtualenvs, hidden worktrees, caches, generated outputs, and existing runtime artifacts.
- Python target IDs should probably be file path plus optional symbol name, not package/module import name only, because many files are scripts and some are not import-safe.
- Context building must be source-only. Importing target modules during discovery would trigger production side effects or fail on missing runtime environment variables.
- Tree-sitter should be the primary parser because it can build syntax trees for Python source without importing modules and can tolerate incomplete or legacy syntax better than Python `ast`.
- Existing `*_test.py` naming is ambiguous in target repos. Generated pytest files should live in a dedicated `tests/uta_generated/` or `tests/` area, and UTA should mark generated files clearly instead of mixing with scheduled job scripts.
- Python 2 files are in scope for discovery, context extraction, test generation, and mutation, but they must be routed through explicit legacy Python 2 execution settings rather than the modern Python 3 verification environment.
- The Python implementation needs side-effect heuristics: top-level calls, `if __name__ == "__main__"`, `os.getenv`, subprocess, requests, Hive/MySQL/Trino helpers, filesystem writes, plotting, email/IM, and model training/loading.

## Python Testing Best Practices

Use `pytest` as the default runner because it supports simple function tests, fixture composition, standard `unittest.TestCase` discovery, and a conventional separate `tests/` layout. Pytest’s own guidance recommends virtualenv isolation, editable installs where packaging exists, and test discovery through `test_*.py` / `*_test.py` files under configured test paths.

For UTA flat-script repos without packaging:

- Prefer `python -m pytest` so current-directory imports work predictably.
- Do not create a repo-wide `tests/conftest.py` by default.
- Add a scoped `tests/uta_generated/conftest.py` only when generated tests need explicit import-path setup or shared generated fixtures.
- Keep generated tests in `tests/test_<module>.py`, matching pytest discovery.
- Use `tmp_path` for filesystem effects and `monkeypatch.setenv`, `monkeypatch.chdir`, and `monkeypatch.setattr` for global configuration, working directories, and external dependency replacement.
- Use `unittest.mock.patch` where the target must be replaced in the namespace where production code looks it up.
- Favor small deterministic DataFrame fixtures over real Hive/model/data files.
- Prefer assertions on returned values, DataFrame shapes/columns/content, raised exceptions, and calls to patched side-effect functions.
- Avoid broad integration behavior in unit generation: no real Hive, HDFS, network, email, IM, GPU, CatBoost/TensorFlow training, or large data files.

Coverage should be collected with `coverage run -m pytest ...`, followed by `coverage xml` or JSON parsing. Coverage.py supports statement and branch coverage and can report missed lines; UTA should use those deterministic artifacts for repair prompts.

Mutation testing should be collected with `mutmut` in the first release. UTA should run mutation only after targeted pytest and coverage pass, then parse the surviving-mutant output into the same repair-loop shape currently used for Java/PIT: mutation family, file/function, line, operator/detail, killability, and a concise diff or source snippet when available. A missing or failing mutation backend is a failed Python mutation gate, not a skipped metric.

`mutmut` support must be version-aware:

- Modern Python 3 targets use current `mutmut` in a Python 3 environment that satisfies the package metadata. As of the latest PyPI metadata checked on 2026-05-21, current `mutmut` is Python 3 only and requires Python `>=3.10`.
- Legacy Python 2 targets use `mutmut==1.5.0` in an isolated Python 2.7 environment. Current `mutmut` project metadata explicitly says Python 2 codebases should use `mutmut 1.5.0`.
- UTA must not install current `mutmut` into a Python 2 environment or treat its failure as proof that Python 2 mutation is impossible.
- UTA must expose the selected mutation runtime in reports, for example `mutmut-modern` vs `mutmut-legacy-py2`, including interpreter path, mutmut version, test runner command, target path, and config source.
- If a Python 2 interpreter or `mutmut==1.5.0` environment is unavailable, the target fails with an actionable legacy-runtime diagnostic. It is not reported as mutation `N/A`.

Reference sources:

- Pytest good integration practices: https://docs.pytest.org/en/stable/explanation/goodpractices.html
- Pytest monkeypatching: https://docs.pytest.org/en/stable/how-to/monkeypatch.html
- Python `unittest.mock`: https://docs.python.org/3/library/unittest.mock.html
- Coverage.py: https://coverage.readthedocs.io/en/latest/
- Mutmut Python mutation tester: https://mutmut.readthedocs.io/en/latest/
- Mutmut current PyPI metadata: https://pypi.org/project/mutmut/
- Mutmut 1.5.0 PyPI release: https://pypi.org/project/mutmut/1.5.0/

## Tech Stack

Existing UTA stack remains:

- Python 3.9+
- `tree-sitter` and `tree-sitter-java`
- Click CLI
- LangGraph workflow
- OpenCode sessions
- Pydantic settings
- Rich reports

New Python-project support:

- `tree-sitter-python` for primary Python syntax parsing.
- Python `ast` only as optional enrichment for Python 3 files that parse cleanly.
- `coverage.py` for Python line/branch coverage.
- `pytest` as the generated-test runner.
- `unittest.mock` and pytest `monkeypatch` patterns in prompts.
- `mutmut` as the required first Python mutation backend.
- Modern mutation lane: current `mutmut` for Python 3 targets, installed in a Python 3-compatible verification environment.
- Legacy mutation lane: pinned `mutmut==1.5.0` for Python 2.7 targets, installed in an isolated legacy verification environment.
- A Python mutation adapter boundary so UTA can select modern vs legacy `mutmut` without changing the workflow contract, and can add other mutation engines later.
- A Python CI enforcement backend owned by UTA, using `git diff`, `pytest`, `coverage.py`, and `mutmut` instead of Maven/root-POM hooks.
- A dev-skills Python enforcement launcher, so local developers can run the same diff coverage and mutation gate without requiring every Python repo to consume a new internal Python package. The launcher delegates either to the full canonical UTA `uta python-enforce` implementation or to UTA's single-file standalone Python enforcement tool, which can be sparse-checked-out without a full UTA repo.

Dependency policy:

- Always use Tree-sitter as the primary Python parser.
- Use standard-library `ast` only after Tree-sitter parsing, and only when a Python 3 file parses cleanly.
- For files that fail Python 3 `ast` parsing, preserve the Tree-sitter context, classify syntax/error nodes, and report the syntax family instead of crashing the whole run.
- Add `tree-sitter-python` as a UTA dependency for Python support.
- Add runtime dependencies needed for deterministic verification. `pytest`, `coverage`, and modern `mutmut` are required for the first Python implementation. Python 2 mutation requires a maintained configured legacy Python 2.7 environment with pinned `mutmut==1.5.0`.
- Do not require target repos to adopt packaging before UTA can test flat script layouts.

## Operating Modes

### Batch UTA Tasks

Batch mode is the Python equivalent of current repo-level UTA generation:

- Triggered by `uta run`, `uta tasks create`, or the task daemon.
- Selects targets by explicit file/symbol, recent git history, or `--all`.
- Supports both file/module-level and function-level target IDs.
- Uses the Python language adapter for scanning, context, prompts, generated test paths, pytest verification, coverage parsing, mutation execution, and repair loops.
- Writes normal `.uta_cache` and `.uta_reports` artifacts under the target repo.
- Owns generation and repair directly. There is no external CI gate involved.
- Uses direct quality backends: `pytest`, `coverage.py`, and `mutmut`.

Batch task selection must support Python target IDs in addition to Java `class_fqn` rows. The task DB and report layer should preserve backward-compatible Java fields while adding language-aware target metadata:

```json
{
  "language": "python",
  "targets": [
    {
      "id": "pyfile:jobs/app_ad/measure/parse_fee_rules.py",
      "sourcePath": "jobs/app_ad/measure/parse_fee_rules.py",
      "symbol": null
    }
  ],
  "targetGranularity": "file",
  "qualityMode": "batch",
  "qualityGateBackend": "python_direct"
}
```

### CI Incremental Gate And Repair

The Python CI path should mirror the Java API trigger contract but replace the Maven/root-POM enforcement backend with a Python enforcement backend.

Java path:

- The target repo configures the UTA Maven test-enforcement plugin/profile in the root POM.
- The UTA API trigger clones the branch, runs the configured Maven enforcer command, parses diff coverage and PIT/mutation evidence, and blocks RDC when the gate fails.
- On user request, the report creates an urgent UTA repair task with `quality_mode=ci_incremental` and `quality_gate_backend=maven_enforcer`.
- After UTA pushes allowed test-only changes, the API trigger reruns the Maven enforcer and callbacks RDC success only when final deterministic evidence is green.

Python path:

- The target repo does not have a universal root-POM equivalent. UTA should provide the enforcement backend as a UTA-owned runner, for example `uta python-enforce`, invoked by the API trigger in the checked-out workspace.
- The Python enforcement runner computes changed production Python files from the configured base ref, defaulting to `origin/master`.
- The UTA API trigger receives language from the RDC trigger payload and uses that value to select `maven_enforcer` vs `python_enforcer`. It should not guess language when RDC provides it.
- It excludes tests, virtualenvs, vendored directories, generated outputs, hidden worktrees, `.uta_cache`, and `.uta_reports`.
- If no changed production Python files are present, the gate passes with explicit "no changed production Python files" evidence.
- It runs deterministic checks only; it never asks the LLM to judge pass/fail.
- It runs or reuses targeted pytest execution, coverage.py diff-line analysis, and mutmut mutation analysis for the changed production files.
- It emits a structured evidence JSON plus human-readable stdout markers so the API trigger can classify `passed`, `failed`, `timeout`, `command_error`, `missing_evidence`, and `skipped` consistently across Java and Python.
- On gate failure, the report offers the same one-click repair flow. The repair task uses `quality_mode=ci_incremental`, `quality_gate_backend=python_enforcer`, `language=python`, and selected Python target IDs such as `pyfile:<path>` or `pysymbol:<path>::<symbol>`.
- Function-level Python targets are preferred for CI incremental repair when diff-to-symbol mapping is reliable; fall back to file-level targets when a diff spans top-level code, multiple symbols, or parser uncertainty.
- After repair, UTA pushes only generated/updated test files and allowed test resources, then reruns the Python enforcer. RDC callback success requires final green Python enforcement evidence.

The Python enforcer should prefer zero target-repo configuration for flat target repos. Optional repo config can be read from `pyproject.toml`, `setup.cfg`, or `.uta/python-enforce.toml` when present, but missing config should not fail basic diff gating if UTA can infer safe defaults.

## Additional Design Requirements

Python support must preserve the original UTA progress reporting, cost accounting, and cost estimation features:

- Repo and target progress must continue to use existing task status, stage, target-row, event, and heartbeat flows.
- Python target rows should display path or path-plus-symbol IDs without assuming Java class FQNs.
- Token usage and provider cost must still be recorded per target and rolled up to the repo task.
- Cost estimation should use target count, source size, context size, and historical per-language/task data when available. Until Python history exists, use conservative Java-derived defaults and label the estimate as Python-derived-from-Java assumptions.
- Existing Java progress dashboards, cost reports, and estimates must remain compatible.

Add a target compatibility abstraction instead of spreading Python conditionals through existing task/progress/cost/report code:

- Introduce a small `TargetIdentity` model with `language`, `target_id`, `display_name`, optional `source_path`, optional `symbol`, `granularity`, and optional Java `legacy_class_fqn`.
- Add helper/facade functions for target storage key, target display name, target count, result key, and event payloads.
- Keep existing Java APIs and DB columns working. Java callers can still use `class_fqn`; Python callers use target IDs.
- Include an additive, backward-compatible task DB migration in the first Python release.
- Add `repo_tasks.language` and class-task target columns: `language`, `target_id`, `source_path`, `symbol`, `target_granularity`, and `display_name`.
- Backfill existing Java rows with `language='java'`, `target_id=class_fqn`, `target_granularity='class'`, and `display_name=class_fqn`.
- Store Python target metadata in both `selection_json` and the migrated target columns. While `class_tasks.class_fqn` remains `NOT NULL`, Python should also store `target_id` there as a compatibility key, but renderers and reports must not treat it as a Java FQN.
- Keep the existing `UNIQUE(repo_task_id, class_fqn)` constraint for the first migration; do not add a new uniqueness contract until historical report compatibility is proven.
- Add target-oriented TaskManager wrappers such as `create_task_targets`, `ensure_target_tasks`, `record_stage_for_targets`, and `sync_target_results`, with existing class-oriented methods retained as Java compatibility wrappers.
- Renderers, reporters, stage events, estimator, and budget checks should depend on target facade helpers, not direct `class_fqn` string semantics.

Runtime and environment selection:

- UTA orchestration runs in the deployed UTA runtime.
- Python 3 target tests run in the configured target Python environment.
- Python 2 target tests and mutation run through the configured Python 2.7 legacy environment with pinned `mutmut==1.5.0`.
- Preflight must report interpreter paths, pytest, coverage.py, mutmut version, and config source before running gates.

Configuration precedence:

1. CLI arguments from UTA API trigger or dev-skills launcher.
2. Environment variables from UTA deployment, CI runtime, or developer shell.
3. Repo-local config files: `.uta/python-enforce.toml`, `.uta-test-enforcement.toml`, `pyproject.toml`, `setup.cfg`, `tox.ini`, or `noxfile.py`.
4. UTA defaults.

The resolved configuration must be written to evidence. Required fields include base ref, coverage gate, mutation gate, test command, Python 3 interpreter override, Python 2 interpreter, Python 2 mutmut executable, source roots, test roots, excludes, and timeouts.

Evidence and CI commit binding:

- Python enforcement evidence must include `schemaVersion`, `evidenceId`, repo, base ref/commit, head ref/commit, language, backend, status, reason code, artifact paths, generated timestamp, UTA version, and enforcement core version.
- JSON evidence is the source of truth; stdout markers are human-readable diagnostics only.
- Unknown major evidence schema versions must not pass CI.
- Repair sessions must be bound to the failed head commit. If the branch moved, rerun enforcement instead of repairing stale evidence.
- RDC success callback may use only final post-repair evidence for the pushed repair commit.

Dependency setup:

- Python enforcer must support an optional setup command/environment profile for target dependencies.
- Setup status, environment profile, dependency fingerprints, and setup failure must be recorded in evidence.
- CI environment cache reuse must include Python version, dependency file hashes, and runtime lane in the cache key.
- Python 2 and Python 3 environments must use separate setup commands/caches.
- UTA must not modify target dependency manifests as part of this spec.

Target selection limits:

- Large Python repos must use deterministic selection caps for files, functions, generated tests, and mutation targets.
- Reports must list skipped targets and skip reasons when caps truncate selection.
- Cost estimates must reflect selected and skipped targets.

Auto-detection:

- Language detection order is RDC language, explicit `--language`, explicit target flags, repo markers, then changed-file suffixes.
- Mixed Java/Python repos without an explicit language should fail with an ambiguity diagnostic.
- Evidence should include the chosen language, decision source, and detected candidates.

Diff coverage semantics:

- Diff coverage is scoped to executable changed Python lines.
- Ignore deleted lines, comments, blanks, import-only hunks, formatting-only hunks, tests, virtualenvs, vendored trees, generated outputs, hidden worktrees, `.uta_cache`, and `.uta_reports`.
- For function/class body changes, prefer function-level repair targets when Tree-sitter mapping is reliable. Fall back to file-level targets for top-level code, multi-symbol hunks, or parser uncertainty.

Mutation execution:

- Run mutation only after pytest and coverage pass.
- Scope mutmut to changed or selected files/symbols where supported.
- Isolate and clean mutation state between targets.
- Write mutation artifacts to `.uta_cache/python/mutation/` or the CI artifact root.
- Do not commit `.mutmut-cache`, `.coverage`, `.uta_cache`, `.uta_reports`, or other runtime artifacts.

Status taxonomy:

- Reuse shared statuses: `passed`, `failed`, `timeout`, `command_error`, `missing_evidence`, and `skipped`.
- Python reason codes should include missing Python runtime, missing pytest, missing coverage, missing mutmut, missing Python 2 runtime, missing Python 2 mutmut, unsupported syntax, no changed production Python, no executable changed lines, unsupported mutation target, mutation backend failure, coverage gate failure, and mutation gate failure.

Generated test idempotency:

- Generated tests must use deterministic paths for target IDs.
- UTA-owned generated files must include target ID, source path, task ID or generation timestamp, and UTA version.
- Repeated runs should update UTA-owned test blocks or files without overwriting human-authored tests.
- Generated file paths must be recorded in reports and CI evidence.

Safe import and test execution:

- Discovery and context building must never import target modules.
- Generated tests should import side-effect-heavy modules only after monkeypatching required environment, working directory, and dependency shims.
- For risky modules, prefer `importlib` inside test functions after setup rather than top-level test imports.
- If a target cannot be imported safely, fail with actionable import-safety diagnostics instead of generating broad integration tests.

Artifact paths:

- Python context artifacts live under `.uta_cache/python/context/`.
- Coverage artifacts live under `.uta_cache/python/coverage/coverage.json` and `coverage.xml`.
- Mutation artifacts live under `.uta_cache/python/mutation/`.
- Enforcement evidence lives under `.uta_cache/python/enforcement/evidence.json` and `.uta_reports/python-enforcement.json`.
- CI runtime may use isolated artifact roots, but final paths must be present in evidence and reports.

Python CI enforcement evidence should include:

- language and backend, for example `python` / `python_enforcer`
- base ref and head commit
- changed production files
- changed executable lines per file
- pytest command and result
- diff line coverage covered/total/rate, by file and overall
- mutation runtime lane: `mutmut-modern` or `mutmut-legacy-py2`
- mutation generated/killed/survived/timeout/error/rate, by file or target when available
- survivor summaries suitable for `python_fix_mutations.txt`
- actionable missing-environment diagnostics, such as unavailable Python 2 legacy runtime or missing modern `mutmut`
- UTA enforcement core version, UTA version, and dev-skills launcher version when invoked through dev-skills

Missing Python enforcement evidence is not green. A plain `pytest` success without diff coverage and mutation evidence is equivalent to the Java path's "Maven exited green but no enforcement evidence" failure.

### Local Developer Distribution

Java local development gets the enforcement gate from normal Maven dependency management:

- The project inherits `example-root` / `example-parent-generic`.
- The root POM exposes the `test-enforcement` profile.
- Developers run Maven with `-Dtest.enforcement.enabled=true`.
- The dev-skills `uta_dev_gate.py` wrapper receives a repo command, checks POM tooling versions, runs the command locally, and requires `[test-enforcer]` diff coverage and mutation evidence before review or ship.

Python should provide the same local-developer affordance through the existing `dev-skills` plugin first, not through target-repo package distribution:

- Add a skill-provided launcher script, for example `/path/to/dev-skills/scripts/uta_python_test_enforce.py`.
- The script is invoked directly from the developer's checkout, the same way `scripts/uta_dev_gate.py` is currently invoked by dev-skills commands.
- Target repos do not need to publish, install, or depend on a UTA enforcement library just to run the gate.
- The script uses the target repo's configured/current Python environment for pytest, coverage.py, project imports, and modern mutmut.
- For Python 2 targets, the script runs from a modern Python 3 tool environment but delegates test execution and `mutmut==1.5.0` to the configured Python 2.7 legacy environment.
- Optional target-repo config can be read from `.uta-test-enforcement.toml`, `.uta/python-enforce.toml`, `pyproject.toml`, `setup.cfg`, `tox.ini`, or `noxfile.py` when present, but basic operation should work from CLI flags and inferred defaults.
- The skill script emits the same structured JSON and `[test-enforcer]`-style stdout markers as CI, so dev-skills, UTA CI, and RDC reports all validate the same evidence format.
- The skill script contains no enforcement algorithm. It resolves the configured UTA executable or UTA checkout/module path, then delegates to `uta python-enforce`. If UTA cannot be resolved, local enforcement fails with an actionable setup message.
- Deployed UTA never imports or reads from the skill directory.

Recommended source split:

```text
uta/enforcement/python/
  __init__.py
  cli.py                    # Argument parsing used by UTA CLI and dev-skills launcher.
  config.py
  diff.py
  evidence.py
  source_filter.py
  coverage.py
  mutation.py

plugins/plugins/dev-skills/scripts/
  uta_dev_gate.py              # Existing umbrella gate; extended to understand Python evidence.
  uta_python_test_enforce.py   # User-facing launcher that delegates to full UTA or the UTA lightweight Python enforcement tool.
```

The source of truth for the enforcement algorithm is `uta/enforcement/python/`. Local developers call the dev-skills launcher, which delegates to UTA. UTA CI calls the same module directly. Contract tests should prove the dev-skills launcher and direct UTA command produce identical evidence for the same sample diff.

Example skill-script invocation for modern Python projects:

```bash
python3 /path/to/dev-skills/scripts/uta_python_test_enforce.py \
  --repo . \
  --base-ref origin/master \
  --coverage-gate 95 \
  --mutation-gate 100 \
  --test-command 'python -m pytest -q' \
  --output-json .uta_reports/local-test-enforcement.json
```

Example optional flat UTA script-repo config:

```toml
# .uta-test-enforcement.toml
base_ref = "origin/master"
coverage_gate = 95
mutation_gate = 100
test_command = "python -m pytest tests/uta_generated -q"
source_roots = ["jobs", "util", "."]
test_roots = ["tests/uta_generated"]
exclude = ["lib", ".venv", ".worktrees", ".uta_cache", ".uta_reports"]
```

Legacy Python 2 repos cannot run modern coverage/mutation orchestration inside the application interpreter. The skill script should run under Python 3 and use configured legacy runtime settings maintained by UTA/dev-skills:

- configured Python 2.7 interpreter.
- configured pinned legacy `mutmut==1.5.0` executable.
- `--test-command` for Python 2 pytest/unittest execution.

The dev-skills `scripts/uta_dev_gate.py` should be extended rather than bypassed:

- Accept Python test-enforcement commands through the existing `--test-enforcement-cmd` option.
- Add language-aware enforcement evidence parsing. Java keeps the current Maven stdout/POM tooling checks; Python uses JSON evidence as the source of truth and treats stdout markers as diagnostics.
- Reject plain `pytest`, plain `coverage`, or any command that does not produce Python enforcement evidence.
- Validate Python evidence schema version, language, backend, status, reason code, base/head commits, coverage gate result, mutation gate result, and artifact paths.
- Detect Python tooling versions from the skill script output and, when available, target-repo config or lock files. Unlike Java, absence of repo-pinned enforcement tooling is not a failure because the enforcement script is distributed by dev-skills.
- Treat missing, skipped, stale, or failed Python enforcement evidence as blockers before review or ship.
- Keep Java behavior unchanged.

## Commands

Repository commands for UTA itself:

```bash
python -m pytest tests
python -m pytest tests/test_cli.py tests/test_project_summary.py tests/test_prompt_render.py
python -m pytest tests/test_python_support.py
```

Expected Python target verification commands:

```bash
python -m pytest tests/test_<module>.py -q
coverage run --branch --source=. -m pytest tests/test_<module>.py -q
coverage xml -o .uta_cache/coverage/coverage.xml
coverage json -o .uta_cache/coverage/coverage.json
mutmut run "<module-or-function-pattern>"
mutmut show all > .uta_cache/mutation/mutmut-show.txt
<legacy-venv>/bin/mutmut run "<legacy-module-or-function-pattern>"
<legacy-venv>/bin/mutmut show all > .uta_cache/mutation/mutmut-py2-show.txt
```

For flat or path-based target repos, UTA must derive the `mutmut` target pattern or run-scoped configuration from the selected source path and generated pytest file. If a repo cannot be auto-configured for `mutmut`, UTA should fail with an actionable mutation-configuration error instead of silently reporting mutation as unavailable.

Python 2 mutation commands are illustrative. The implementation should execute them through the configured legacy interpreter path and pinned legacy environment, not assume `python2` or `mutmut` is globally available.

Expected Python CI enforcement commands:

```bash
uta python-enforce \
  --repo /path/to/checkout \
  --base-ref origin/master \
  --head-ref HEAD \
  --coverage-gate 95 \
  --mutation-gate 100 \
  --output-json .uta_reports/ci-enforcement.json

uta python-enforce \
  --repo /path/to/checkout \
  --base-ref origin/master \
  --changed-file jobs/app_ad/measure/parse_fee_rules.py \
  --coverage-gate 95 \
  --mutation-gate 100 \
  --output-json .uta_reports/ci-enforcement.json
```

The exact command can be configured through the API trigger, but the output contract must be stable so report rendering, fix-session availability, and RDC callbacks do not depend on fragile stdout parsing alone.

Expected local developer commands through dev-skills:

```bash
python3 /path/to/dev-skills/scripts/uta_python_test_enforce.py \
  --repo . \
  --base-ref origin/master \
  --coverage-gate 95 \
  --mutation-gate 100 \
  --test-command 'python -m pytest -q' \
  --output-json .uta_reports/local-test-enforcement.json

python3 /path/to/dev-skills/scripts/uta_dev_gate.py \
  pre-review \
  --jira TASK-12345 \
  --test-enforcement-cmd 'python3 /path/to/dev-skills/scripts/uta_python_test_enforce.py --repo . --base-ref origin/master --coverage-gate 95 --mutation-gate 100 --test-command "python -m pytest -q"'
```

Expected UTA usage after implementation:

```bash
uta enforce --language python \
  --repo . \
  --base-ref origin/master \
  --coverage-gate 95 \
  --mutation-gate 100 \
  --test-command 'python -m pytest -q' \
  --output-json .uta_reports/python-enforcement.json

uta run --repo /home/user/md/store_sku_prediction_model --language python --all
uta run --repo /home/user/md/store_sku_prediction_model --language python --target config_resolver.py
uta run --repo /home/user/md/strategy-job --language python --target jobs/app_ad/measure/parse_fee_rules.py
uta scan --repo /home/user/md/store_sku_prediction_model --language python --all
uta query-index --repo /home/user/md/store_sku_prediction_model --language python --target config_resolver.py
```

CLI language handling should auto-detect Java vs Python by default. `--language` remains an override for mixed or ambiguous repos. Java may keep `--class-fqn`; Python should use path/symbol target options. Both forms should normalize into `TargetRef` before shared task, report, cost, or CI code sees the request.

## Project Structure

Proposed UTA structure:

```text
uta/languages/
  __init__.py
  base.py              # LanguageAdapter protocol / dataclasses
  registry.py          # BackendRegistry for language adapters and enforcement runners
  java.py              # Existing Java-specific behavior behind an adapter
  python.py            # Python scanner, parser, path, prompt, and gate config

uta/enforcement/
  __init__.py
  base.py              # Neutral EnforcementRunner protocol, request/result/evidence contracts
  evidence.py          # Versioned evidence schema helpers shared by CI and local dev gates
  registry.py          # Enforcement backend lookup by language/backend

uta/api_trigger/enforcement/
  __init__.py
  maven.py             # Existing Java Maven enforcer behavior registered as maven_enforcer
  python.py            # Python diff coverage/mutation enforcer behavior registered as python_enforcer

uta/language/python/parse/
  __init__.py
  tree_sitter_parser.py # Tree-sitter Python parser, symbol/import extraction, error-node classification
  ast_enrichment.py    # Optional Python 3-only enrichment when ast.parse succeeds
  coverage.py          # coverage.py command runner and JSON/XML parser
  mutation.py          # mutmut modern/legacy runner, result parser, survivor family summarizer
  diff_enforce.py      # diff-line coverage and mutation gate calculator for CI incremental path

uta/language/python/
  context.py           # Python ContextProvider adapter
  context_builder.py   # Python target context and side-effect hints

uta/enforcement/python/
  __init__.py
  cli.py
  config.py
  diff.py
  evidence.py
  source_filter.py
  coverage.py
  mutation.py

uta/prompts/
  python_plan_tests.txt
  python_generate_test.txt
  python_fix_compile.txt
  python_fix_coverage.txt
  python_fix_mutations.txt

tests/fixtures/python_flat_project/
  config_resolver.py
  config_schema.py
  data_util.py

tests/test_python_parser.py
tests/test_python_context_builder.py
tests/test_python_coverage.py
tests/test_python_mutation.py
tests/test_python_support_cli.py
tests/test_cli_language_options.py
tests/test_query_index_python.py
tests/test_language_scanner.py
tests/test_python_context_exports.py
tests/test_python_ci_enforcement.py
tests/test_api_trigger_python_incremental.py
tests/test_api_trigger_python_fix_sessions.py
tests/test_ci_auto_push_python_guard.py
tests/test_learning_target_identity.py
tests/test_report_cli_python_targets.py
tests/test_python_enforcement_launcher.py

dev-skills changes:

plugins/plugins/dev-skills/scripts/
  uta_python_test_enforce.py
plugins/plugins/dev-skills/tests/
  test_python_test_enforce.py
```

Target Python repo test placement:

```text
tests/
  conftest.py
  test_config_resolver.py
  test_data_util.py

tests/uta_generated/
  conftest.py                  # Optional; generated only when import-path setup or shared fixtures are needed
  test_jobs_app_ad_measure_parse_fee_rules.py
```

For target repos where existing `*_test.py` files may be executable jobs, prefer `tests/uta_generated/` so generated pytest tests are visually and operationally separate.

## Implementation Slices

Implement this as staged, reviewable work rather than one large change:

1. Baseline and inventory: lock Java regressions and inventory Java-shaped surfaces, including CLI/task commands, query-index, deterministic dev gate, scripts, deployment wrappers, progress/cost/reporting, learning, API trigger, and E2E behavior.
2. Multi-language foundation: add `TargetIdentity`/`TargetRef`, additive task DB migration, target compatibility facade, progress/report/cost/estimator wrappers, backend registry, capability lookup, deterministic language detection, generic `uta enforce --language`, and CLI target normalization.
3. Python discovery and context: add Tree-sitter Python parsing, Python 2 classification, source filters, bounded target selection, skipped-target reasons, side-effect hints, companion-file discovery, optional Python 3 `ast` enrichment, Python query-index, and language-aware scanner/context exports.
4. Python batch generation: add Python prompt bundles, generated pytest placement/idempotency, import-safety diagnostics, source-only context into OpenCode, OpenCode-faked workflow tests, Python batch reports/progress/cost display, and learning/retrospective target IDs.
5. Python verification and mutation: add runtime preflight, config precedence, dependency setup/cache contract, pytest runner, coverage.py parser, Python 2-compatible coverage lane, modern mutmut lane, legacy Python 2 `mutmut==1.5.0` lane, mutation scoping/cleanup, survivor summaries, and mutation repair prompt loop.
6. Enforcement core and local dev gate: add UTA-owned Python enforcement core, versioned evidence contract, reason taxonomy, artifact paths, dependency setup evidence, commit binding, `uta python-enforce` alias, dev-skills launcher equivalence tests, and `uta_dev_gate.py` Python evidence validation while preserving Java behavior.
7. CI incremental path: route RDC `language=python` to `python_enforcer`, parse Python JSON evidence, render CI reports, compute diff executable-line coverage and diff mutation, create path/symbol repair tasks, reject stale heads, rerun enforcement after repair, update RDC context markdown, and restrict auto-push to generated tests/resources.
8. Existing scripts and operational wrappers: update or document `bin/uta-query-index`, `scripts/enqueue.sh`, daemon/CI/deploy scripts, `setup-fetchcode.py`, prompt lookup commands, and OpenCode retrospective hints so shared surfaces are language-aware and Java-only helpers stay explicit.
9. Staged E2E and deployment: refactor E2E harness for Java, Python 3, and Python 2 lanes; then deploy to node2 with DB migration, runtime/dependency setup checks, Python enforcement checks, repair-path verification, Java regression checks, and updated README/architecture/RDC docs.

Each slice must preserve existing Java behavior and include its own regression tests before the next slice starts. The design doc contains the detailed implementation plan and review boundaries.

## Code Style

UTA implementation should use small typed adapters instead of branching on language throughout the graph:

```python
@dataclass(frozen=True)
class TargetRef:
    language: str
    id: str
    source_path: str
    display_name: str
    test_path: str


class LanguageAdapter(Protocol):
    language: str

    def scan_candidates(self, repo_path: str, *, module: str | None, all_files: bool) -> list[str]:
        ...

    def build_context(self, repo_path: str, targets: list[str]) -> ContextBundle:
        ...

    def expected_test_path(self, repo_path: str, target_id: str) -> str:
        ...

    def verification_commands(self, target: TargetRef) -> VerificationPlan:
        ...
```

Generated pytest style should be direct, isolated, and side-effect free:

```python
def test_resolve_index_returns_unit_offsets():
    result = ConfigResolver._resolve_index(
        start_date="2026-01-08",
        end_date="2026-01-22",
        base_date="2026-01-01",
        unit_days=7,
    )

    assert result == [1, 2]
```

Side-effect isolation example:

```python
def test_get_sku_key_df_reads_expected_table(monkeypatch):
    calls = []

    def fake_get_hive_df(sql):
        calls.append(sql)
        return pd.DataFrame({"sku_code": ["1001"]})

    monkeypatch.setattr(io_data_util, "get_hive_df", fake_get_hive_df)

    df = io_data_util.get_sku_key_df("sku-store-week", ["sku_code"], "20260521")

    assert list(df["sku_code"]) == ["1001"]
    assert "dm_md_features_sku_info_sku_v1" in calls[0]
```

## Testing Strategy

UTA unit tests:

- Add Tree-sitter parser tests for flat Python modules, imports, functions, classes, decorators, and side-effect calls.
- Add parser tests for Python 2 syntax and Tree-sitter error-node classification so one legacy file does not abort repo-wide discovery.
- Add optional `ast_enrichment` tests showing it runs only for clean Python 3 files and is skipped safely otherwise.
- Add scanner tests for git-history ranked `.py` files and `--all` production Python files.
- Add scanner tests that exclude `.venv`, `venv`, `__pycache__`, `.git`, `.worktrees`, vendored/library directories, and generated output trees.
- Add CLI language option tests for `run`, `scan`, `parse`, `tasks create`, `tasks create-manifest`, and `tasks enqueue`.
- Add context-builder tests that export target context for a module/function without requiring imports of heavy target dependencies.
- Add Python query-index tests for file/function target lookup and Java regression tests for existing `uta-query-index --class-fqn` behavior.
- Add language scanner tests covering Java `src/main/java`, Python flat roots, Python job roots, virtualenv/vendor excludes, and git-history ranking.
- Add Python context export tests for source-only context, symbol map, import hints, side-effect hints, and companion files.
- Add prompt-render tests for stable Python planning/generation prompts.
- Add coverage parser tests using small checked-in `coverage.json` or XML fixtures.
- Add mutation runner/parser tests using checked-in modern and legacy `mutmut show all` fixtures and command-runner fakes.
- Add mutation repair prompt tests that verify survivor families are fed into `python_fix_mutations.txt`.
- Add mutation environment-selection tests that verify Python 3 targets use the modern lane and Python 2 targets use the pinned `mutmut==1.5.0` lane.
- Add Python diff-enforcement tests for changed-line extraction, executable-line filtering, no-production-change pass, missing-evidence failure, and coverage/mutation gate failures.
- Add UTA Python enforcement core tests for CLI config loading, evidence JSON schema, stdout marker rendering, optional target config detection, and Python 2 legacy runtime selection.
- Add evidence contract tests for schema versions, pass/fail evidence fixtures, no-target pass, Python 2 legacy lane, missing runtime, unknown schema version rejection, and stale commit rejection.
- Add dependency setup tests for setup skipped/reused/executed, setup failure, environment profile reporting, dependency fingerprints, and Python 2/Python 3 cache separation.
- Add target selection limit tests for large repos, deterministic caps, skipped-target reasons, and estimates that reflect selected vs skipped targets.
- Add API trigger tests that route Python projects to `quality_gate_backend=python_enforcer`, create Python target repair tasks, rerun Python enforcement after repair, and preserve existing Java Maven behavior.
- Add CI fix-session tests for Python `pyfile:`/`pysymbol:` targets, stale branch-head rejection, Python evidence target extraction, and `quality_gate_backend=python_enforcer`.
- Add CI auto-push tests proving Python repair commits can include only generated pytest files/resources and must reject production `.py` edits.
- Add dev-skills compatibility tests or fixtures showing `uta_dev_gate.py` accepts Python enforcement output and still rejects missing evidence.
- Add dev-skills deterministic gate tests proving Java behavior is unchanged, Python JSON evidence passes, and plain pytest/coverage output is rejected.
- Add contract tests proving the dev-skills launcher, the lightweight UTA enforcement tool, and direct full `uta python-enforce` command emit equivalent evidence for the same sample diffs.
- Add CI trigger tests proving RDC-provided language selects the enforcement backend, with explicit override behavior for ambiguous repos.
- Add migration tests proving existing task DBs are upgraded in place, Java rows are backfilled with target metadata, Python rows can be inserted with target metadata, and old Java readers still work.
- Add target compatibility facade tests proving Java rows still resolve to class FQNs and Python rows resolve to path/symbol target IDs while storage keys, display names, event payloads, and result keys remain stable.
- Add backend registry tests proving languages and enforcement runners register by name, CI routing is table-driven, and `uta enforce --language python` and `uta python-enforce` produce the same command contract.
- Add a fake third-language adapter test proving task DB insertion, target display, progress events, token/cost rollup, report rendering, and CI routing work without adding third-language branches to shared code.
- Add task/report compatibility tests proving Python targets preserve progress reporting, per-target token/cost accounting, repo-level cost rollups, and cost estimation without changing Java report behavior.
  These tests must explicitly cover Python file-level and function-level target display, stage-event rendering without Java `class_fqn`, simulated OpenCode token/cost aggregation into target rows and repo tasks, budget enforcement reuse, estimator no-history fallback, estimator history-present behavior, and estimate explanations labelled as Python.
- Add learning/retrospective tests proving records are keyed by language-aware target ID and old Java class-FQN records remain readable.
- Add report CLI tests proving Python target metadata renders in batch/repo reports without Java-only class labels.
- Add generated-test idempotency tests proving repeated Python runs reuse deterministic paths, update only UTA-owned generated tests, and avoid churn in import/path setup.
- Add workflow tests with monkeypatched OpenCode and coverage runners so no real LLM or sample repo dependencies are required.
- Keep existing Java tests green.

E2E testing strategy:

- Split verification into development-phase and post-deployment gates.
- Development-phase verification must include unit tests plus staged E2E tests over real local Python 3 and Python 2 repos when configured.
- Refactor existing E2E repo-path helpers so E2E tests can run against Java, Python 3, and Python 2 backends through a shared staged harness.
- Configure real local Python repos with `UTA_E2E_PY3_REPO`, `UTA_E2E_PY3_TARGET`, `UTA_E2E_PY3_TEST_COMMAND`, `UTA_E2E_PY2_REPO`, `UTA_E2E_PY2_TARGET`, `UTA_E2E_PY2_TEST_COMMAND`, `UTA_E2E_PY2_BIN`, and `UTA_E2E_PY2_MUTMUT_BIN`.
- Stage 1: scan/parse/context only, no LLM and no mutation.
- Stage 2: Python enforcer only, with pytest, coverage, and mutation evidence.
- Stage 3: batch UTA task with OpenCode mocked/faked where possible to verify task state, target selection, reports, and gates.
- Stage 4: optional real OpenCode generation against a small Python 3 target.
- Stage 5: API trigger trigger and repair flow using Python language parameter and Python enforcer rerun.
- Add a Python 2 E2E lane that at minimum verifies Tree-sitter discovery, Python 2 classification, configured legacy runtime detection, and pinned `mutmut==1.5.0` mutation execution when the local Python 2 runtime is available.

Post-deployment verification:

- Deploy the UTA API trigger and Python enforcement configuration to node2 before production rollout.
- Follow the design doc's node2 deployment instructions, including task DB backup, additive DB migration, service rollout, health checks, Java regression checks, Python 3/Python 2 enforcement checks, and rollback boundaries.
- Verify node2 health and readiness endpoints.
- Run deployed-service checks against controlled real Python 3 and Python 2 branches, not only local mocks.
- For Python 3, verify deployed pytest, diff coverage, mutation, report rendering, and RDC callback behavior.
- For Python 2, verify deployed Tree-sitter discovery, Python 2 classification, configured legacy runtime use, and pinned `mutmut==1.5.0` mutation when the runtime is available.
- Verify missing Python 2 runtime fails with actionable diagnostics, not mutation `N/A`.
- Verify failed Python reports can create repair sessions, push only allowed test changes, rerun `python_enforcer`, and callback success only after final deterministic evidence passes.
- Verify Java API trigger behavior still routes to `maven_enforcer` after deployment.

Generated Python project tests:

- Use `pytest` tests under `tests/`.
- Start with pure function coverage, then patch external dependencies for side-effect-heavy functions.
- Use `tmp_path` for file reads/writes and `monkeypatch` for env/config globals.
- Run targeted tests before coverage.
- Run coverage with branch mode when the tool is available.
- Run mutation after coverage passes; mutation is a hard Python quality gate.
- Default coverage and mutation thresholds should start at the same values as Java.

Coverage and mutation gates:

- First implementation should support line coverage from `coverage.py`.
- Branch coverage should be parsed and reported when present.
- First implementation must support mutation score from `mutmut` for Python 3 targets and from pinned `mutmut==1.5.0` for Python 2 targets.
- Default Python coverage and mutation gates should start at the same thresholds as Java.
- Python mutation reports must include total mutants, killed mutants, survived mutants, timeout/error buckets when available, mutation score, and top survivor families for repair.
- Python mutation reports must include the selected runtime lane and exact tool version.
- Python mutation repair should follow the Java/PIT pattern: run mutmut, summarize survivors, ask OpenCode for a focused test-strength patch, rerun pytest/coverage/mutation, and stop only when the gate passes or configured attempts are exhausted.
- CI incremental Python gates must use diff-scoped executable lines and diff-scoped mutation targets, not whole-repo aggregate coverage as a substitute.
- CI incremental repair must rerun the Python enforcement backend after pushed test changes before callback success.

## Boundaries

- Always: preserve existing Java behavior and tests.
- Always: keep language-specific logic behind adapters or clearly named modules.
- Always: generate Python tests under `tests/` by default.
- Always: keep generated pytest tests separate from UTA scheduled job scripts named `*_test.py`.
- Always: isolate side effects in generated Python tests; patch Hive, HDFS, network, email, IM, model loading, GPU probes, and filesystem globals.
- Always: parse Python source with Tree-sitter instead of importing target modules during discovery.
- Always: treat Python `ast` output as enrichment only; do not make repo discovery depend on `ast.parse` success.
- Always: skip virtualenvs, vendored dependencies, caches, hidden worktrees, and generated artifacts during default discovery.
- Always: run Python mutation testing with the configured backend after pytest and coverage pass.
- Always: use current `mutmut` only for compatible Python 3 targets.
- Always: use pinned `mutmut==1.5.0` in an isolated legacy environment for Python 2 targets.
- Always: treat mutation backend/setup failure as a gate failure with actionable diagnostics, not as a pass or `N/A`.
- Always: keep CI gate pass/fail deterministic and tool-based; the LLM may generate repairs, but it must not decide whether an RDC gate passed.
- Always: make local dev, UTA CI, and UTA batch consume the same UTA Python enforcement core.
- Always: keep dev-skills Python enforcement as a launcher only; the lightweight local algorithm lives in the UTA-owned `tools/python-enforcement/` tool, not inside the skill.
- Always: fail local dev-skills enforcement with an actionable setup message if the UTA executable/module cannot be resolved.
- Always: rerun the same language-specific CI enforcement backend after an incremental repair task before sending RDC success.
- Always: make Python CI repair tasks language-aware and path/symbol-based; do not coerce Python targets into Java class FQNs.
- Always: distribute Python local enforcement through dev-skills scripts instead of requiring every target repo to install the full UTA service or consume a new internal Python package.
- Always: default Python CI base ref to `origin/master`.
- Always: use RDC-provided language to route CI enforcement when present.
- Ask first: adding target-repo dependencies, rewriting target repo structure, or introducing required packaging.
- Ask first: changing the configured Python 2 interpreter, legacy virtualenv location, or pinned legacy mutation dependency versions.
- Ask first: requiring Python target repos to add a checked-in `.uta` config, pytest plugin, pyproject section, or new enforcement dependency for CI enforcement.
- Ask first: replacing the dev-skills launcher path with a distributed Python package.
- Ask first: changing production task DB schema in a non-backward-compatible way.
- Ask first: altering API trigger contracts or RDC semantics.
- Never: run generated tests against real production Hive/HDFS/network/email/IM services.
- Never: edit generated `.uta_reports` or `.uta_cache` manually as source changes.
- Never: break existing Java/Maven options, Java prompt contracts, or report compatibility without a migration plan.
- Never: report plain pytest success as a passing CI gate when diff coverage or mutation evidence is missing.
- Never: introduce a distributed Python enforcement package as part of this spec.

## Success Criteria

- `uta scan --language python --all` lists production `.py` files from the sample repo while excluding tests, caches, virtualenvs, generated outputs, and hidden tool dirs.
- `uta scan --language python --all` can scan `strategy-job` and `mddf-job` without crashing on vendored trees, virtualenvs, or Python 2 syntax.
- Python context extraction works from Tree-sitter parse trees even when Python `ast` cannot parse the file.
- Python target IDs do not pretend to be Java class FQNs; reports clearly identify module/file/function targets.
- UTA can build target context for at least `config_resolver.py`, `data_util.py`, and `input_sanity_check.py` without importing the target modules.
- UTA can build source-only target context for at least one `strategy-job/jobs/...` file with companion `.sql`/`.job` files nearby.
- UTA can generate pytest tests for at least one pure function target and one side-effect-isolated function target in a flat Python repo.
- Verification uses `python -m pytest`, `coverage.py`, and `mutmut`, not Maven/Jacoco/Pitest.
- UTA computes and reports Python mutation score from modern `mutmut` for Python 3 targets.
- UTA computes and reports Python mutation score from pinned `mutmut==1.5.0` for Python 2 targets when a legacy runtime is configured.
- UTA can run a Python mutation repair loop from surviving mutants and improve or pass the configured mutation gate on a controlled fixture project.
- UTA can run the same mutation repair contract against a controlled Python 2 fixture using the legacy `mutmut==1.5.0` lane.
- The batch task path can run Python targets with `quality_gate_backend=python_direct`.
- The API trigger can route a Python repo/branch to `quality_gate_backend=python_enforcer` instead of `maven_enforcer`.
- The Python CI enforcer passes branches with no changed production Python files and emits explicit no-target evidence.
- The Python CI enforcer fails when pytest passes but diff coverage or mutation evidence is missing.
- The Python CI enforcer computes diff line coverage and diff mutation score for changed production Python files.
- Python coverage and mutation gates default to the Java thresholds.
- Python CI routing uses the language parameter provided by RDC.
- Python CI enforcement evidence includes UTA enforcement core version and UTA version.
- Dev-skills local enforcement evidence additionally includes the dev-skills launcher version.
- UTA has contract tests that compare dev-skills launcher output with the lightweight UTA enforcement tool and direct full `uta python-enforce` output.
- Existing E2E tests are refactored into a language-aware staged harness covering Java, Python 3, and Python 2 backends.
- Python 3 E2E can run against a real local Python 3 repo through staged scan/context, enforcement, batch task, optional real generation, and API trigger checks.
- Python 2 E2E can run against a real local legacy repo through staged scan/context, legacy-runtime detection, and mutation checks when the configured Python 2 runtime is available.
- Post-deployment verification on node2 passes for controlled Python 3 and Python 2 branches using the deployed UTA API trigger environment.
- Node2 verification confirms health/readiness, UTA enforcement core availability, Python enforcer evidence, repair-session creation, restricted test-only push behavior, rerun enforcement, and RDC callback semantics.
- Node2 verification confirms Java triggers still use `maven_enforcer`.
- Python local developer enforcement is distributed as a dev-skills script and can run against existing flat Python repos without adding a new UTA enforcement dependency.
- `uta_dev_gate.py` can run a Python enforcement command and validate the same diff coverage and mutation evidence before review or ship.
- Flat target repos run generated tests from repo root by default and only get `tests/uta_generated/conftest.py` when needed.
- A failed Python CI report can create an urgent incremental repair task with Python file/symbol targets, user context, RDC context, and `quality_mode=ci_incremental`.
- After a Python CI repair task completes, the API trigger reruns the Python enforcer and only reports final success when deterministic Python coverage and mutation evidence pass.
- Generated tests for the sample project do not require real Hive, HDFS, network, email, IM, CatBoost/TensorFlow training, or large data artifacts.
- Generated tests for job repos are placed away from scheduled job scripts and do not rely on live scheduler, Hive, MySQL, Trino, WNotice, or corp HTTP endpoints.
- Existing Java tests continue to pass.
- README and architecture docs describe Java and Python support accurately.

## Open Questions

None.
