# ADR-011: Use One Enforcement Contract With A Distributed Python Binding

## Status

Accepted

## Date

2026-08-20

## Context

UTA currently presents several overlapping boundaries: `LanguageAdapter`, an
unused `EnforcementCore`, a command-level `EnforcementRunner`, a per-target
`VerificationRunner`, language-specific entrypoints, and dictionary evidence
utilities. The lightweight Python client under `tools/python-enforcement` must
remain independently distributable, but full UTA still contains a second
Python enforcement orchestration path.

The required runtime paths are:

- `UTA app/testgen -> enforcement contract -> selected language binding`; and
- `distributed tool -> enforcement contract -> selected language binding`.

The contract must not import concrete languages, UTA task/workflow code, or
agent-core.

## Decision

`uta_enforce_core` will own the sole neutral request, capability, normalized
result/evidence, validation, binding protocol, an immutable registry mapping,
and one stateless `enforce()` dispatch function.

`uta_py_enforce` under `tools/python-enforcement` will implement the canonical
Python binding and high-level enforcement use case. The lightweight CLI will
register it directly. Full UTA will register a thin Python proxy that injects
UTA-owned cancellation, progress, command, and CI-only mutation-selection
policy, then delegates exactly once to the same distributed binding.

The Java binding will remain UTA-local and implement the same contract. The
contract selects bindings from an application-owned registry and never
imports them.

Existing evidence marker names, schema/backend identifiers, reason taxonomy,
CLI flags/exit codes, Python 2 behavior, and failure-closed policy remain
compatible.

## Alternatives Considered

### Move the Python binding into UTA

Rejected because local developers would need a full UTA checkout/package and
the sparse-checkout distribution requirement would be lost.

### Keep two orchestrators sharing low-level utilities

Rejected because command ordering, candidate policy, evidence, and failure
semantics can still drift. One high-level binding must execute both paths.

### Let the contract import/register built-in bindings

Rejected because it reverses source dependencies and makes the neutral package
load concrete toolchains. Composition owns registration.

### Put enforcement in agent-core

Rejected because Java/Python quality enforcement is product-language domain
logic, not an agent capability.

## Consequences

1. Full UTA and the local CLI share one Python algorithm.
2. `uta_enforce_core` stays sparse-checkout safe and dependency-light.
3. UTA CI sampling remains possible only through explicit proxy policy
   injection; local/full/repair modes cannot enable it.
4. Java and Python produce one normalized contract while retaining
   language-owned execution/parsing.
5. Parallel UTA enforcement contract surfaces and wildcard bridges can be
   retired.
6. Contract and evidence parity tests become release gates.

## Relationship To Earlier Decisions

This ADR refines and extends ADR-003. ADR-003's lightweight distribution and
one-algorithm decisions remain accepted; this ADR makes the sole contract,
dispatch function, and UTA proxy boundary explicit.

## Amendment (2026-08-21): no service object, no mode enum

Second-pass design review observed that the dispatch machinery was apparatus for
choosing between two bindings, one of which is a proxy to the other. Two
narrowings, both approved by the requester:

- `EnforcementRegistry` takes its whole mapping at construction and is immutable
  from that point. There is no `register()`, therefore no `freeze()`, and no
  window in which a half-built registry can be used. `EnforcementService` is
  replaced by a stateless module-level `enforce(request, *, registry, context)`.
  The spec's requirement for a registry is met; a service object with a
  lifecycle is not needed to meet it.
- `EnforcementMode` is removed. Encoding caller identity (`local_dev`,
  `full_cli`, `repair`, `ci_report`) into the neutral contract is the coupling
  the contract exists to remove. CI mutation sampling arrives instead as
  `sampling_policy` on the invocation context. This is a stronger guarantee than
  the enum gave: "only the CI composition constructs a context with a sampler"
  is a source fact a boundary test checks, where a mode was a runtime value any
  caller could set wrongly.
