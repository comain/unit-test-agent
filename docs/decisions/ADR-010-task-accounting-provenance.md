# ADR-010: Record Only Provider-Supplied Task Costs

## Status

Accepted

## Date

2026-08-19

## Context

`TaskManager` currently derives currency cost from token counts using a
hard-coded table keyed by OpenCode model names. It then stores this inferred
number as both actual and provider cost, uses it in estimates and budget
checks, and exposes it as verified report data. The table cannot reliably
represent changing provider pricing, routing, cache policy, or a future
non-OpenCode harness.

Agent-core already owns harness/provider execution and fallback. UTA owns the
product task ledger and must not re-derive provider facts from implementation
details.

## Decision

UTA will persist currency cost only when the normalized durable result provides
a numeric provider cost. Missing cost is represented as unavailable, not zero
and not an inferred value. Historical inferred values remain readable but are
identified as legacy-unverified in newly rendered provenance.

An explicitly requested USD cap is rejected at task creation unless the
selected durable harness contract guarantees authoritative provider cost. If a
running cost-capped task receives an unknown cost, execution blocks before the
next paid turn. Token/turn limits remain independently enforceable. Measured
task history can estimate a later task only from `recorded` observations; if
there is no usable observation, the estimate is unavailable.

Every paid attempt is durably recorded before workspace acceptance. Agent-core
retries the accounting sink and, on failure, writes an owner-only spool artifact
that UTA adopts without rerunning the model. Every new projection carries explicit provenance:
`recorded`, `unavailable`, or `allocated`. Existing numeric values are
`legacy_unverified` because they may have been inferred. Task-level cost sums
each durable paid attempt once; a shared batch cost may be allocated to class
rows for product reporting, but is never relabeled as a class-level provider
charge.

## Alternatives Considered

### Keep the price table in UTA

Rejected. It duplicates provider policy in the product layer, becomes stale,
and falsely labels a calculation as a provider result.

### Move the price table to agent-core

Rejected. Agent-core can expose provider-reported accounting, but a universal
price table still cannot establish the price actually charged by a provider.

### Treat missing cost as zero

Rejected. Zero is materially different from unknown and would silently weaken
cost visibility and budget controls.

## Consequences

- New tasks and summaries may show unavailable cost until a provider reports
  cost data.
- Historical data is preserved without a backfill or schema rewrite.
- Product accounting no longer depends on model names, OpenCode configuration,
  or an agent-local SQLite database.
- A user who needs a hard USD stop must run on a harness/provider contract that
  reports authoritative cost; otherwise task creation fails before paid work.
