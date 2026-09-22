# ADR-012: Enforce One-Way Generation, Task, And Enforcement Boundaries

## Status

Accepted

## Date

2026-08-20

## Context

UTA's package-level dependency graph currently cycles among `engine`,
`enforcement`, Java language code, tasks, and testgen. Lazy imports prevent an
immediate import crash but hide ownership problems: task retention imports
workflow/checkpoint implementations, workflow code imports concrete task
managers, Java phases import their composing adapter/helper namespace, and the
umbrella language protocol mixes detection, generation, and workflow binding.

At the same time, task transitions and workflow/product projections rely on
SQLite atomicity. A package cleanup must not replace visible cycles with broken
transactions or parallel compatibility systems.

## Decision

UTA will use these source directions:

- app composition may depend on testgen, task persistence, concrete generation
  bindings, and concrete enforcement bindings/proxies;
- testgen may depend on agent-core, the sole enforcement contract, and
  Java/Python generation leaves;
- generation leaves depend only on dependency-light generation contracts and
  language-owned domain modules, never testgen composers or app/task managers;
- enforcement bindings/proxies depend on `uta_enforce_core`; the contract never
  imports them;
- task persistence depends only on dependency-light product values and imports
  neither testgen nor agent-core; and
- app-owned adapters implement testgen consumer ports using task repositories.

`uta.engine` will be dissolved. Enforcement contracts move to
`uta_enforce_core`; generation-specific contracts/logic move to the
generation/testgen boundary; generic values move to dependency-light modules.
The umbrella `LanguageAdapter` and competing enforcement top-level protocols
will be retired.

`TaskDB` remains the compatibility facade and sole SQLite transaction owner
while its implementation splits into repositories/services. Cross-table
operations reuse one connection and outer transaction. No schema change is
introduced solely for package organization.

A repository-owned AST dependency gate will classify eager, lazy, and
type-only imports and reject prohibited edges and unapproved logical cycles.

## Alternatives Considered

### Keep cycles hidden with function-local imports

Rejected because it avoids import-time failure without clarifying ownership or
preventing future coupling.

### Move all language behavior behind one umbrella adapter

Rejected because generation and enforcement have different consumers and
distribution requirements. The umbrella protocol caused type-level back-edges
and made every language implement unrelated methods.

### Move workflow tables into a second database

Rejected because it introduces migration, consistency, and recovery risk. Code
ownership can be corrected while retaining one transactional SQLite store.

### Delete TaskDB immediately

Rejected because numerous stable callers and compound atomic operations use
the facade. Splitting behind it yields the boundary without unnecessary public
churn.

## Amendment (2026-08-21): generation binding composition

The original decision left generation-binding selection open between "direct"
and "app-composed", which design review correctly flagged as indecisive.

Selection is **app-composed**. `uta.app` detects the language, imports exactly
one generation leaf, constructs it, and injects it into testgen. `uta.testgen`
imports only `uta/language/contracts.py` and never `uta.language.java` or
`uta.language.python`, eagerly or lazily.

This makes the lazy-loading requirement a property of the graph rather than a
rule to police: testgen has no import to make, so a Java run cannot reach the
Python toolchain. It also keeps language selection in one place, since app
already resolves the language to pick the enforcement binding. The cost is one
additional injected dependency on testgen entry points.

## Consequences

1. Package direction becomes executable and reviewable.
2. Testgen may use product-language generation bindings without putting them in
   agent-core.
3. Enforcement is always reached through the sole contract.
4. Task/workflow cooperation occurs through app-composed ports, not reverse
   concrete imports.
5. Existing transaction semantics and durable identities remain unchanged.
6. Internal import paths move substantially; characterization and dependency
   tests are required for every slice.
