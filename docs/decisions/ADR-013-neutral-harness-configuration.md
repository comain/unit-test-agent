# ADR-013: Neutral Harness Configuration, With One Named OpenCode Exemption

## Status

Accepted

## Date

2026-08-21

## Context

The approved specification says agent selection is configuration only:
selecting Pi or another harness must not require changing UTA application,
task, workflow, enforcement-contract, or language-domain code. The initial
design satisfied that for *code* but said nothing about *configuration*, and
configuration is where the remaining provider knowledge actually lives.

Measured on 2026-08-21:

- `uta/shared/config.py` declares 41 `opencode_*` settings
  (`UTA_OPENCODE_*` in the environment).
- Only 8 distinct settings are read anywhere outside `config.py`, across 28
  reference lines in 6 modules. The other 33 are declared by UTA and consumed
  by agent-core's harness construction; UTA never interprets them.
- `uta/app/opencode_assessment.py` (394 lines) reads OpenCode's private
  SQLite database (`~/.local/share/opencode/opencode.db`, `part` table) and is
  reached only from `uta assess` via `uta/app/assessment_commands.py`.

Two problems follow. First, adding a second harness today means adding a
parallel `UTA_PI_*` block and a branch in composition, which is exactly the
outcome the specification forbids. Second, the planned dependency checker bans
concrete OpenCode names under `uta/`, so `uta assess` would fail the gate it
was never intended to be judged by, and the design did not say so.

## Decision

**Configuration.** UTA keeps only the settings whose *meaning* is UTA's, and
gives them neutral names. Everything else becomes opaque passthrough.

1. Harness selection stays `UTA_AGENT_HARNESS` (already neutral).
2. The 33 settings UTA never interprets move into one opaque mapping,
   `UTA_HARNESS_OPTIONS` (JSON object), forwarded verbatim into the
   `HarnessSpec`. UTA does not validate, default, or branch on its keys;
   agent-core's harness factory owns their meaning.
3. The 8 interpreted settings are re-homed by what they actually mean, not by
   who produced them. The per-setting disposition table is in
   `docs/design-uta-architecture-boundary-cleanup-uta.md`.
4. A bounded compatibility reader still accepts every `UTA_OPENCODE_*` name for
   one release. It maps each to its new home, logs one deduplicated deprecation
   warning per run naming the replacement, and is deleted in the release
   following the one that ships this iteration — one release, not "a release". Precedence is explicit: a new
   name always wins over a legacy name, and setting both logs a conflict.

**Diagnostics.** `uta assess` stays OpenCode-specific and stays where it is.
It is exempted from the concrete-harness ban by one named allowlist entry
covering exactly `uta/app/opencode_assessment.py` and its command module, with
an owner and a review date twelve months out. The earlier deletion condition —
"when a second harness ships and a second forensic reader is wanted" — is not an
observable event and is replaced by the date.

The gate asserts what makes the exemption safe, phrased as the existing test at
`tests/test_opencode_assessment_placement.py:43` already phrases it: the reader
has exactly one importer, `uta/app/assessment_commands.py`, and no module under
`uta/tasks`, `uta/testgen`, `uta/enforcement`, or `uta/language` imports either.

Note that this is a rule about *importers*, not about reachability.
`uta/app/cli.py:847` imports the command module eagerly, so anything importing
`uta.app.cli` pulls in the OpenCode reader. That is acceptable — the CLI root is
the delivery layer — but "unreachable from a running task" would be the wrong
claim to make, and the gate does not make it.

## Alternatives Considered

### Move assessment behind an optional neutral agent-core capability

Rejected. The command's value is that it reads what OpenCode itself wrote,
independently of the numbers UTA records from `TurnResult`; the disagreement
between the two is the failure it exists to detect. A neutral capability with
exactly one implementation would relocate a private schema dependency into the
shared library and lose the independence that makes the reading worth having.
Per-step tool counts and reasoning-character verbosity have no neutral meaning
to define.

### Rename every `UTA_OPENCODE_*` setting to a neutral equivalent

Rejected. Thirty-three of them have no UTA-side meaning to rename *to*; giving
them neutral aliases would assert a shared vocabulary UTA cannot maintain and
would break again for the first harness whose options differ.

### Keep `UTA_OPENCODE_*` names indefinitely as aliases

Rejected. A permanent alias is a permanent statement that OpenCode is the
harness. The compatibility window is bounded and its deletion condition is
recorded.

## Consequences

1. Adding a harness becomes a configuration and agent-core change, with no UTA
   settings block and no composition branch.
2. Operators must migrate environment variables within one release; the
   deprecation warning names each replacement.
3. UTA stops owning defaults it cannot validate, so a bad harness option now
   fails in agent-core with agent-core's message instead of silently taking a
   UTA default.
4. One OpenCode-specific diagnostic remains in the tree, deliberately, named in
   the gate rather than hidden from it.
