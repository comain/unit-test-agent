# UTA Model Discovery Integration

Status: approved direction with review fixes incorporated. Non-Jira.
Canonical design: [agent-core overview](../../agent-core/docs/design-model-discovery.md).

## 1. Changes And Flow

Add optional model policy/cache/binding settings in `uta/shared/config.py`.
Load them from trusted `AGENT_MODEL_SELECTION_CONFIG` using the shared loader;
the refresher reads the same file. Do not translate/discover from target .env.
Default manual mode remains unchanged; discovery defaults to threshold 70 when
enabled. Operator-owned `AGENT_MODEL_CODING_INDEX_MIN` overrides the application
minimum when supplied; absent both, use 70. Load at service startup, validate
as finite [0,100], and never take it from a target repo/task. Schedule refresh
daily. CLI, daemon batch, and CI repair invoke shared cached resolution at
agent execution/resume, not task creation. Do not
require catalog availability for enforcement-only CI requests.

`uta/app/cli.py::_apply_task_opencode_selection` currently restores selected model
and provider only. In discovery mode bypass these historical model/index
overrides and use the current shared resolver. Keep manual behavior unchanged.
Trace `uta/tasks/creation.py` and generation commands in tests.

## 2. Data Model And Failure Handling

Do not add model_selection to task snapshots. No table migration. Missing cache
or eligibility fails clearly before LLM execution. Resume uses current catalog
and policy, honoring shared availability/cooldown state by identity rather than
the task's historical index.
Do not serialize API keys. Report concise exclusion reasons and AA attribution
when scores are shown. Cost/progress/task status schemas remain unchanged.

## 3. Verification And Delivery

Test batch and CI repair create/resume against the same catalog; check unrelated
Java/Python generation, budgets, progress, and provider fallback. Package-pin the
released agent-core version, configure/refresh cache and validate mappings before
enabling. Follow existing remote deployment docs via git pull, idle-only restart,
then real repair verification. Rollback subsequent invocations to manual mode;
do not interrupt active calls. No production changes during design.
Provision the AA key only to the isolated refresh job, not UTA's worker env.
Verify daily launcher and worker resolve identical provider scopes/cache paths.

## 4. Changelog

- 2026-09-07: Initial consumer detail for automatic model admission.
- 2026-09-07: Dropped task snapshot integration; fresh resolution on resume.
- 2026-09-07: Daily refresh and production-owned minimum override documented.
