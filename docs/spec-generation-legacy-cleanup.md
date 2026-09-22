# Generation legacy cleanup specification

Status: approved continuation of the non-Jira executable-generation migration.

## Objective

Make the declarative, checkpointed generation cycle the only execution path
for Java and Python. Remove the legacy composite agent runtime, its cutover
flag, prompt-scope audit data, and test-only compatibility bridges without
losing standalone commands, resumability, accounting, or deterministic
language behavior.

The user explicitly skipped the K2 beta-capacity/replay exercise. This change
therefore prepares and pushes the cleanup, but does not authorize a production
deployment or claim a production observation window.

## User-visible behavior

- Managed tasks always execute `generation-cycle.yaml` through agent-core's
  `agent_turn` node.
- Standalone Java and Python generation keep their current public request and
  result shapes. Internally they receive an owner-only ephemeral task database,
  workflow identity, operation ledger, and checkpoint store. Synthetic IDs are
  never exposed as product task IDs or report/SSE identities.
- Persisted-task migration is explicitly skipped. Deployment must prove there
  are no non-terminal legacy snapshots. Old work is finished or deliberately
  cancelled before deployment; if still required, the operator submits a new
  task after the durable-only build starts. There is no in-place clean rerun or
  automatic field/lineage copy from a legacy task.
- Task creation always records the durable engine version. Acquisition and
  direct execution reject non-terminal legacy snapshots before changing task
  status, producing actionable finish/cancel-and-submit-new guidance.
- Rollback is supported only to a build that understands durable snapshots.
  Rolling back to pre-durable code requires restoring the database backup.

## Project structure

- `uta/testgen/graph/`: sole generation-cycle orchestration and ephemeral
  execution binding.
- `uta/testgen/`: neutral agent harness construction only; no product-owned
  session polling loop.
- `uta/language/{java,python}/phases/`: language prompt, interpretation,
  verification, and result projection.
- `uta/tasks/`: acquisition guards, read-only cutover audit, and retention.
- `uta/app/`: one operator command for the cutover audit; no second execution
  path such as legacy `resume-gates`.

## Scope discovery

| Candidate | Decision | Reason |
| --- | --- | --- |
| `AgentRuntime`, `create_agent_runtime`, `llm_session.run_agent_node` | remove | duplicate agent lifecycle owned by agent-core |
| Java/Python composite `run_generation_cycle` implementations | remove | bypass the declarative phase graph |
| `generation_cycle_v2_enabled` selector/default | remove | there is no second engine after cleanup; old snapshots are not rewritten |
| standalone Java/Python batch APIs | preserve via ephemeral durable binding | public tool behavior must remain available without a product task |
| deterministic Java/Python phase helpers | retain or extract | domain behavior belongs in language phase modules |
| neutral `create_agent_harness` | retain and relocate | constructs the agent-core runner used by durable phases |
| legacy prompt-scope table/audit/retention | delete writer/execution use; retain read-only schema/pruner for one 30-day window | old sensitive artifacts still require bounded deletion |
| `resume-gates` legacy command | replace with durable task resume and new-task guidance | it directly owns an OpenCode client and duplicate repair loops |
| checkpoint/result/progress retention | retain | required by the durable workflow |
| K2 beta replay and production deployment | out of scope | explicitly skipped/not authorized |

## Code style and boundaries

- Agent selection remains configuration inside agent-core; UTA never branches
  on OpenCode, Pi, or another concrete agent.
- Workflow state contains serializable data only. Harnesses, stores, and
  callbacks live in invocation context.
- Ephemeral persistence lives outside the target repository, uses `0700`
  directories and `0600` files, and is covered by the existing lease/pruner.
- No raw prompt, reasoning, command, or tool output enters task snapshots,
  events, reports, or Git delivery.
- Historical design documents remain historical; this spec and its design
  record the approved final deletion.

## Testing strategy

1. Cutover guard tests: new tasks are durable; non-terminal old snapshots are
   refused before acquisition/direct execution/resume; terminal history remains
   readable and cannot be requeued in place.
2. New-task tests: after the hard cut, ordinary task creation records a fresh
   durable lineage and does not copy identity from terminal legacy history.
3. Standalone Java/Python tests: real declarative routes with scripted harness,
   result parity, checkpoint recovery, cleanup, crash-orphan pruning, ownership
   modes, and no synthetic identity leakage.
4. Source boundaries: no production imports or calls to legacy runtime/session
   symbols, composite generation methods, or the cutover flag.
5. Durable phase and golden prompt/result tests retain Java/Python domain parity.
6. Full UTA suite and consumer compatibility suite pass before commit/push.

## Acceptance criteria

1. Every managed and standalone generation unit enters the same declarative
   cycle and every model phase uses agent-core `agent_turn`.
2. The old agent lifecycle, composite cycles, cutover selector/flag, and legacy
   prompt writer/execution lifecycle have no production callers and are
   deleted. A read-only retention compatibility reader remains for one window.
3. Non-terminal persisted legacy tasks are explicitly refused with
   finish/cancel-and-submit-new guidance; no cleanup build silently rewrites or
   executes one.
4. Standalone callers need no external task database and observe no synthetic
   task identity.
5. Managed stop/resume, operation reconciliation, provider fallback,
   token/timing accounting, delivery, and result shapes remain compatible.
   Standalone calls remain single-call operations; provider fallback completes
   inside that call and cross-call stop/resume is not a public contract.
6. README and operator documentation describe durable-only execution,
   pre-deploy audit/drain, rollback limitations, and skipped K2 evidence.
7. No unrelated worktree changes are committed.
