# ADR-006: A fresh agent session per phase and per deliberate repair

Status: proposed
Date: 2026-08-17
Context: [spec](../spec-executable-generation-agent-turn.md), [design](../design-executable-generation-agent-turn.md)

## Context

**This documents existing behaviour rather than proposing new policy.** Java
already opens a session per phase: `_create_phase_session`
(`generation.py:796-809`) is called separately for planning (`:3108`) and
generation (`:3571`), and each repair opens its own via `client.open_session`
(`:1473, :1888, :2249, :2342`). The risk in this ADR is therefore not "will a new policy work" but
"will the migration preserve one that does".

The target generation cycle exposes thirteen phase labels per unit, several of them repair
attempts. Each is a model-driven turn. Sessions could be scoped per unit (one
long conversation), per phase, or per turn.

Two forces pull in opposite directions. Reusing a session keeps context and
saves the model re-reading the repository. Isolating sessions keeps phase token
accounting attributable, keeps parallel progress timelines separable, and stops
unrelated phase context leaking into a later repair.

## Decision

Each model-driven phase, and each deliberate repair attempt, starts a **fresh**
session. UTA explicitly configures `agent_turn(session_scope="phase")`.
agent-core's config-free default remains `"none"` for compatibility, and the
only supported scopes are `"none"` and `"phase"`.

Only **bounded stalled-turn recovery** reuses the current session, because that
is recovery of the same logical turn rather than a new phase.

## Consequences

- Token usage and timing are attributable to a phase, which is what the report
  claims to show.
- Parallel sessions stay in separate progress timelines; the spec forbids
  merging them and a shared session would make separation impossible.
- A coverage repair does not inherit the mutation phase's context, which is a
  correctness property, not a tidiness one: stale context has produced repairs
  aimed at the wrong failure.
- The cost is that each phase re-establishes context. This is accepted, and is
  already being paid today — so the migration adds no cost here, it preserves
  one.
- Session open/close is process-local: `create_session` is a `uuid4()` and a
  dict entry, `delete_session` a `pop` (`client.py:140-155`).

## Alternatives considered

**One session per target.** Rejected: phase accounting becomes an estimate, and
two phases running in parallel cannot be told apart in the event stream.

**One session per turn, including recovery.** Rejected: recovery exists
precisely to avoid paying for the exploration a stalled turn already did; a new
session for the nudge defeats it.
