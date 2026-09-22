# RDC API Trigger Usage

> RDC refers to this integration as its "CI plugin". In UTA it is the API trigger
> service (code package `uta/app/`); the two names are interchangeable.

UTA exposes an RDC AppTool-compatible API trigger for the `单元测试` pre-stage task.
RDC calls UTA, UTA runs the UTA test-enforcement Maven gate, and UTA reports the
deterministic pass/fail result back to RDC.

## Flow

```text
RDC pipeline, web app, pre-stage
        |
        | AppTool POST /unit-test/api/v1/rdc/trigger
        v
UTA API trigger
        |
        +--> clone/fetch branch into UTA CI workspace
        |
        +--> build run-scoped RDC context
        |       - RDC payload
        |       - git commit messages
        |       - Jira description when available
        |       - user supplied repair context
        |
        +--> run UTA Maven test-enforcement
        |       - diff line coverage
        |       - changed-target PIT mutation
        |       - final pass/fail from deterministic evidence
        |
        +--> write status and report pages
        |       - /unit-test/task-status/<taskId>
        |       - /unit-test/reports/<taskId>/index.html
        |
        +--> callback RDC ack endpoint
                |
                +--> passed: RDC task succeeds
                |
                +--> failed: RDC task blocks, report offers one-click repair

One-click repair path

failed report
        |
        | click 一键修复
        v
repair session
        |
        +--> create urgent UTA repo task
        |
        +--> pause lower-priority large repo task if needed
        |
        +--> generate/improve unit tests only
        |
        +--> reject production-code or runtime-artifact diffs
        |
        +--> push allowed test changes to the RDC branch
        |
        +--> rerun Maven test-enforcement
        |
        +--> callback RDC success only when final gate is green
```

## RDC Configuration

The RDC task template is `T_91_pre_unitTestAppTool`, named `单元测试`.

Current rollout mode:

- `pipeline_template.json`: `T_91_pre_unitTestAppTool` is present only in the
  shared `web` pipeline template `预发阶段`.
- `task_template_app_white_list.json`: `T_91_pre_unitTestAppTool` is enabled for
  `["*"]`, which means all RDC web pipelines that use the shared web template.
- `task_button.properties`: `T_91_pre_unitTestAppTooltaskButton=run,recover,break`.
  Manual `skip` is intentionally unavailable for this hard gate.

The beta AppTool URL is:

```text
http://ci.example.com/unit-test/api/v1/rdc/trigger
```

UTA is mounted under `/unit-test` on the shared `ci.example.com`
host so it does not collide with the existing CR plugin routes.

## Trigger Contract

RDC sends an AppTool payload to:

```text
POST /unit-test/api/v1/rdc/trigger
```

UTA accepts both top-level fields and nested `attribute` fields. Important fields:

| Field | Purpose |
|---|---|
| `appName` | RDC application/module name. |
| `gitUrl` | Git repository SSH URL. |
| `branch` | RDC development branch to check. |
| `taskId` | RDC task id for callback correlation. |
| `recordId` | RDC task record id for callback correlation. |
| `taskTemplateId` | Expected to be `T_91_pre_unitTestAppTool`. |
| `jiraId` | Jira key; inferred from branch when absent. |

The synchronous response only means UTA accepted the check:

```json
{
  "status": 0,
  "msg": "处理中",
  "data": {
    "taskId": "...",
    "status": "queued",
    "url": "http://ci.example.com/unit-test/task-status/...",
    "reportUrl": "http://ci.example.com/unit-test/reports/.../index.html"
  }
}
```

Invalid payloads return the same envelope with `status=-1`.

## Result Semantics

UTA treats UTA test-enforcement output as the source of truth:

- Green only when required diff coverage and mutation/PIT evidence is present and
  the gate passes.
- Missing evidence is not green even if Maven exits `0`.
- Unrelated existing test failures after usable enforcement evidence are ignored
  unless the output contains explicit test-enforcement failure markers.
- Branches with no changed production Java files under `origin/master...HEAD`
  pass without running Maven because there is no gate target.

The report page shows the final coverage and mutation rates when the Maven
enforcement output contains that evidence.

## Equivalent-Mutant Override

Some mutants cannot be killed: the mutated code behaves exactly like the original
for every input. When a fix session fails only because of such mutants, UTA can pass
that one session with a visible override. It never claims the mutants were killed.

How it works:

1. **Review inside the repair task.** A CI repair (`quality_mode=ci_incremental`)
   whose mutation repair would stop on `mutation_repair_no_progress` runs one LLM
   review turn, `review_equivalent_mutants`, before giving up. The review never
   passes the repair task. It records a verdict for every remaining mutant in
   `class_tasks.equivalence_review_json`.
2. **Eligibility.** The review runs only when:
   - the language can prove it found every mutant that lowers the score (Java
     rebuilds them from the gate's own `.uta_cache/pit-compat/<nonce>` PIT reports,
     Python uses mutmut ids);
   - no other mutant counts against the score (Java NO_COVERAGE, TIMED_OUT,
     RUN_ERROR, MEMORY_ERROR, unknown; Python timeout or suspicious);
   - there are at most `UTA_CI_EQUIVALENT_MUTANT_REVIEW_MAX` survivors (default 30).

   A sampled Python run is never eligible. Ineligible units fail as before and log
   `ci_equivalence_ineligible reason=...`.
3. **Verdicts.** An `equivalent` verdict must name the whole divergence region and
   argue that every input in it gives the same observable result. Arguments from
   examples count as `uncertain`. Any `killable` or `uncertain` verdict, a missing
   or invalid verdict file, or an edit to a tracked file rejects the review. The
   turn is bounded by `UTA_CI_EQUIVALENT_MUTANT_REVIEW_TIMEOUT_SECONDS` (default 1800).
4. **Recheck and grant.** The fix session's normal post-repair gate rerun is the
   recheck. The session grants the override only when:
   - tests and coverage pass, and mutation is the only failing gate;
   - every failing unit was reviewed all-equivalent;
   - the rerun reproduces exactly the reviewed mutants on unchanged source (sha256);
   - nothing else counts against the score.
5. **Result.**
   - The fix session becomes `passed_with_equivalent_mutants`.
   - The CI record status becomes `success` with a `gate_override` field; its
     `enforcement_result` keeps the raw failing gate result.
   - RDC receives one success callback (`state=0`) whose summary names the override
     and the raw score.
   - Report, status, progress and recent-jobs pages show a 等价变异豁免 badge with
     the raw score, the gate and each mutant's reasoning.

The override belongs to that fix session only. A new RDC trigger for the branch runs
the normal gate and may fail again. A later fix session may review again.

Operator signals: `ci_equivalence_review_all_equivalent`,
`ci_equivalence_review_rejected`, `ci_equivalence_granted` and
`ci_equivalence_not_granted`, each with a `reason`.

Rollback: there is no feature flag. Revert the feature commits and redeploy. Records
granted before the revert stay loadable, because `CiTaskStatus` is unchanged and the
extra fields are optional.

## Repair Session Pages

Failed reports expose `一键修复` when UTA can attempt a repair.

The report page links each fix session to:

```text
/unit-test/reports/<taskId>/fix-sessions/<sessionId>/progress
```

The progress page auto-refreshes and shows:

- repo task status and current stage
- class task progress
- final Maven test-enforcement confirmation
- final coverage and mutation rates
- recent task events

## Context Used by Repair

UTA injects RDC context into repair tasks to reduce blind code exploration:

- RDC app name, branch, git URL, task id, record id, and template id
- git commit messages for the branch
- Jira description text when available
- optional user guidance supplied from the `一键修复` form

Context artifacts are written to run-scoped runtime paths, not `.uta_cache`, so
they are not reused across unrelated sessions.

## Git Safety

Repair sessions may push to the same RDC branch, but only under strict guards:

- no force push
- fetch before push
- fail visibly on conflicts or non-fast-forward updates
- only test files and required test resources may be committed
- production-code changes are rejected
- UTA reports, `.uta_cache`, `.sisyphus`, and runtime artifacts are not pushed

The deployed node may use a configured SSH key via `UTA_CI_GIT_SSH_KEY_PATH`, or a GitLab access token via unprefixed `GIT_AC`. `GIT_AC` takes precedence and uses HTTPS Git operations without storing the token in repo remotes.

## Operator Checks

Use these URLs during verification:

```text
GET /unit-test/healthz
GET /unit-test/readyz
GET /unit-test/task-status/<taskId>
GET /unit-test/task-status/<taskId>/data
GET /unit-test/reports/<taskId>/index.html
GET /unit-test/reports/<taskId>/detail
```

Expected log markers:

- `rdc_check_started`
- `rdc_check_finished`
- `rdc_callback_finished`
- `rdc_repair_preempted`

If a repair session is running while a large repo-level batch task is active, the
CI-triggered repair has priority. The lower-priority repo task can be stopped and
resumed later.

## Rollback

Rollback is config-first:

1. Remove `T_91_pre_unitTestAppTool` from the `web` `预发阶段` template, or
2. set `task_template_app_white_list.json` for `T_91_pre_unitTestAppTool` to a
   disabled sentinel value.

UTA batch generation is independent from the RDC API trigger and should continue
running after the RDC gate is disabled.
