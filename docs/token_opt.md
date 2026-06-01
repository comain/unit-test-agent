# Token Optimization Plan

## Current observations

- The tree-sitter target-context work already reduced planning and compile-fix waste materially.
- Gemini reached compile pass and coverage hardening, while the earlier Ollama run spent far more tokens in planning/generation/compile-fix before getting there.
- The main remaining waste is no longer basic planning. It is:
  - under-scoped Wave 1 generation
  - expensive coverage hardening
  - missing repair-specific static facts
  - provider-limit exits that do not always end with a clean final report

## Next improvements

### 1. Enforce Wave-1 breadth before compile

- Add a generation breadth gate before compile verification.
- For strict-coverage large classes, reject generated tests that are too small for the approved plan.
- Candidate checks:
  - minimum `@Test` count
  - minimum number of planned method families represented
  - minimum number of Wave-1 items implemented

Why:
- The current workflow enforces file existence and compile, but not “enough Wave 1 was actually written”.
- This allows the model to satisfy the workflow with a compile-safe but too-small first pass.

### 2. Make planning output machine-checkable

- Replace loose `PLANNED TESTS` prose with structured entries:
  - target method
  - branch family
  - intended test name
  - wave
- Use that structure for generation-breadth validation.

Why:
- The current planning text is improved, but generation can still interpret it too loosely.
- Structured plan items make it possible to validate fidelity automatically.

### 3. Add tree-sitter call-signature cache for compile and coverage repair

- Extend target context export with collaborator call-site facts for high-yield methods.
- Include:
  - called collaborator method
  - resolved overload signature
  - source line
  - return type

Why:
- The current cache covers imports, fields, symbols, and dependency paths.
- Compile-fix still wastes tokens rediscovering overload and argument-type details.

### 4. Add repair-focused target artifact

- Generate a repair-oriented file such as:
  - `ClassName.compile_context.md`
- Include:
  - unresolved-symbol candidates
  - import candidates
  - field types
  - collaborator overloads
  - common stub patterns for high-yield methods

Why:
- Same-session context is not enough by itself.
- Repair needs compact structured facts, not a large noisy session history.

### 5. Make coverage-hardening data-driven

- Parse Jacoco uncovered methods and line ranges for the target class.
- Feed exact uncovered regions into the coverage-fix prompt.
- Do not rely only on a single percentage value.

Why:
- “Coverage is 24.7%” is too weak for a huge class like `SampleServiceImpl`.
- The model needs exact uncovered targets to spend tokens efficiently.

### 6. Keep compact planning subagent only for complex classes

- Re-enable bounded planning delegation only for large strict-coverage classes.
- Keep direct planning for simple classes.
- Require concise structured output only.

Why:
- Broad planning subagents caused earlier stalls.
- But completely disabling planning delegation did not clearly reduce token cost for complex classes.

### 7. Finish provider-limit handling for Gemini end-to-end

- The client now detects Gemini `429 RESOURCE_EXHAUSTED`.
- Next verify the workflow/reporting path always converts that to:
  - `PROVIDER_RATE_LIMITED`
- Avoid silent stalls or half-finished runs.

Why:
- Provider-limit detection is only useful if the run exits cleanly and the user gets an explicit status.

### 8. Fix final report flushing on quota exits

- Ensure `.uta_reports` is still written when the run ends due to provider quota/rate-limit failure.

Why:
- Some recent reruns exited before a final report was persisted.
- That makes regression tracking and comparison harder.

## Recommended implementation order

1. generation breadth gate
2. structured planning output
3. tree-sitter call-signature export
4. repair-focused target artifact
5. Jacoco uncovered-line coverage context
6. final provider-limit reporting cleanup
