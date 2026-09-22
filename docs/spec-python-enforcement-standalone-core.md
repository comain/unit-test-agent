# Spec: Standalone UTA Python Enforcement Core

## Status

- Phase: Spec generation
- Jira: Non-Jira tool work assumed. If this must be attached to a Jira, move this document to `doc/spec-<JIRA>.md` and record Jira context before design.
- Design document: `docs/design-python-enforcement-standalone-core.md` after spec approval
- Usage document: update UTA README and dev-skills `references/test-enforce-usage.md`

## Objective

Refactor UTA Python enforcement so local development can run the same Python enforcement logic without checking out the full UTA repository.

Target users are UTA Python project developers using dev-skills locally, UTA operators maintaining CI enforcement, and UTA repair-session workflows. Success means a developer can sparse-checkout a lightweight UTA-owned Python enforcement tool, or copy it with its version file preserved, point dev-skills at it, and get the same evidence contract and mutation semantics as UTA CI and repair verification.

The current simplified standalone script is not sufficient because it uses stock file-scoped `mutmut run` and does not support UTA's batch generation, candidate planner, operator-level filtering, hard caps, or exact-key evidence. This refactor must remove that semantic fork.

## Scope Discovery

| Candidate | Current role | Decision | Reason |
| --- | --- | --- | --- |
| `uta/language/python/enforcement.py` | Builds Python enforcement evidence, aggregates coverage/mutation, validates schema. | In scope | Core local/CI evidence contract must remain shared. |
| `uta/language/python/verification/runner.py` | Runs pytest, coverage, mutmut, candidate planning, batch generation, operator filtering, hard caps. | In scope | This is where the real Python enforcement semantics live. |
| `uta/language/python/mutation_candidates.py` | Python mutmut candidate planning and exact-key mapping. | In scope | Required for operator-level filtering parity. |
| `uta/language/python/mutation_batching.py` | Function-granular batch partition and aggregation helpers. | In scope | Required for default batch mode parity. |
| `uta/engine/mutation_candidates.py` | Language-neutral candidate-plan identity/counts. | In scope | Lightweight tool must reuse the same evidence contract. |
| `uta/engine/diff.py`, `uta/engine/enforcement.py` | Git diff and evidence envelope helpers. | In scope | Local evidence must be commit-bound and match CI. |
| `uta/language/python/test_selection.py` | Strict target-specific test selection. | In scope | Local and repair verification must select the same tests. |
| `uta/config.py` Python mutation settings | Batch strategy, hard caps, timeout/env defaults. | In scope | Lightweight tool must expose the same knobs or compatible defaults. |
| `uta/cli.py` `python-enforce` | Full UTA CLI entrypoint. | In scope for wiring | CLI should call the extracted core, not a separate implementation. |
| `scripts/uta_python_test_enforce.py` | Current newly added simplified standalone script. | In scope for replacement | It should be deleted, or left only as a hard-failing setup message that exits non-zero and cannot run enforcement. |
| dev-skills `scripts/uta_python_test_enforce.py` | User-facing launcher. | In scope | Must locate the lightweight UTA tool without requiring a full UTA checkout. |
| dev-skills `scripts/uta_dev_gate.py` | Validates evidence marker. | In scope only if evidence wording changes | It should continue validating the same contract and rejecting sampling in local dev. |
| UTA report failure page | Shows local enablement guidance after CI failure. | In scope for wording | Python failures must point users to lightweight local Python enforcement, not Java/Maven or full-checkout-only setup. |
| Java Maven enforcer and Java UTA paths | Java local/CI enforcement. | Out of scope | This refactor is Python-only and must not change Java behavior. |
| RDC/API trigger endpoints | CI report and repair creation APIs. | Out of scope for API shape | They keep calling UTA; only their underlying Python enforcement implementation is shared. |

No endpoint/API traffic table is required because this is a local tool and internal UTA/dev-skills refactor, not a service endpoint change.

## Requirements

1. Extract Python enforcement into a lightweight UTA-owned module set that can run outside the full UTA checkout.
2. Preserve one source of truth for Python enforcement semantics across:
   - full UTA CLI: `uta python-enforce`;
   - UTA CI/API trigger Python enforcement;
   - UTA repair-session verification;
   - dev-skills local enforcement through a sparse-checkout tool.
3. The lightweight tool must support the same Python 3 mutation behavior currently used by UTA:
   - default `batch` mutation generation strategy;
   - mutation candidate planner;
   - changed-line filtering;
   - operator-level filtering;
   - one-useful-mutant-per-line policy;
   - deterministic hard caps;
   - exact mutmut key mapping;
   - candidate-plan evidence and counts;
   - no CI sampling unless the UTA CI adapter injects its internal sampling policy.
4. Python 2 legacy enforcement must remain supported through the existing legacy lane and must not pretend to have Python 3 exact-key/operator-filter capabilities.
5. Dev-skills must not embed enforcement algorithms. It remains a launcher plus validator.
6. A local developer must be able to use only the lightweight UTA tool checkout, not the full UTA app, API trigger, daemon, OpenCode integration, task DB, reports UI, or Java code.
7. Evidence must remain compatible with dev-skills validation:
   - `schemaVersion=1`;
   - `language=python`;
   - `backend=python_enforcer`;
   - current `HEAD` binding;
   - changed production file scope;
   - coverage pass/fail;
   - mutation pass/fail;
   - `candidatePlan` consistency;
   - no local CI sampling.
8. The simplified stock-mutmut standalone implementation must not remain as the recommended path after this refactor, because it can diverge on large files and operator selection.
9. User-facing guidance must be updated in both places users see it:
   - dev-skills local enablement docs and command/skill text;
   - UTA CI report failure page's "本地启用 test-enforcement" section.
10. Python guidance must explicitly tell users to configure `UTA_PYTHON_ENFORCE_SCRIPT` for lightweight local enforcement. If a wrapper is needed, it may document `UTA_PYTHON_ENFORCE_CMD`; it must not document full-checkout fallback setup.
11. Python guidance must not say or imply that local Python enforcement is plain `pytest`, plain `coverage.py`, Java Maven `test-enforcement`, PIT, or a full UTA checkout requirement.

## Commands

Full UTA CLI:

```bash
uta python-enforce \
  --repo . \
  --base-ref origin/master \
  --coverage-gate 95 \
  --mutation-gate 95 \
  --syntax-version python3 \
  --json-output
```

Lightweight local tool, invoked directly:

```bash
python3 /path/to/uta-python-enforcement/uta_python_test_enforce.py \
  --repo . \
  --base-ref origin/master \
  --coverage-gate 95 \
  --mutation-gate 95 \
  --syntax-version python3 \
  --evidence-output .uta_reports/python-enforcement.json
```

Dev-skills launcher using lightweight tool:

```bash
UTA_PYTHON_ENFORCE_SCRIPT=/path/to/uta-python-enforcement/uta_python_test_enforce.py \
python3 /path/to/dev-skills/scripts/uta_python_test_enforce.py \
  --repo . \
  --base-ref origin/master \
  --coverage-gate 95 \
  --mutation-gate 95 \
  --evidence-output .uta_reports/python-enforcement.json
```

Focused target iteration:

```bash
python3 /path/to/uta-python-enforcement/uta_python_test_enforce.py \
  --repo . \
  --base-ref origin/master \
  --target path/to/module.py \
  --test-path tests/test_module.py \
  --coverage-gate 95 \
  --mutation-gate 95 \
  --evidence-output .uta_reports/python-enforcement.json
```

`--json-output` is for UTA/app callers that need evidence on stdout. `--evidence-output` is for local/dev-skills workflows that need a stable file path and `UTA_PYTHON_ENFORCEMENT_EVIDENCE=...` marker. CI-only mutation sampling is not a CLI option; the UTA CI adapter injects it in-process.

Verification commands for this refactor:

```bash
.venv/bin/python -m pytest \
  tests/test_python_evidence_contract.py \
  tests/test_python_verification.py \
  tests/test_python_mutation_batching.py \
  tests/test_python_mutation_candidates.py \
  tests/test_python_standalone_enforcer.py \
  -q

python3 -m pytest /path/to/plugins/plugins/dev-skills/tests/test_uta_dev_gate.py -q
```

## Project Structure

The design phase should choose the exact package layout, but the spec requires a structure that separates the lightweight core from full UTA app concerns.

Expected logical structure:

```text
tools/python-enforcement/
  uta_python_test_enforce.py        # standalone entrypoint
  uta_enforce_core/
    diff.py                        # language-neutral git changed files and changed lines
    evidence.py                    # language-neutral evidence envelope and marker formatting
    mutation_candidates.py         # language-neutral mutation candidate-plan contract
    targets.py                     # language-neutral target refs used by local enforcement
  uta_py_enforce/
    cli.py                         # argument parsing and stdout/evidence output
    config.py                      # local env/config resolution
    test_selection.py              # strict Python unit-test selection
    verification.py                # pytest/coverage/mutmut orchestration
    mutation_candidates.py         # candidate plan and operator policy
    mutation_batching.py           # batch partition and aggregation
    mutmut_adapter.py              # mutmut v3/v2/v1.5 interactions
```

UTA package code should import this shared lightweight core where practical instead of copying it. If some UTA-specific adapters remain under `uta/language/python/`, they must wrap the shared core rather than reimplementing core behavior.

Dev-skills should keep:

```text
plugins/dev-skills/scripts/uta_python_test_enforce.py  # launcher only
plugins/dev-skills/scripts/uta_dev_gate.py             # evidence validator only
```

## Code Style

Use small, pure functions for shared logic and keep subprocess/runtime operations behind narrow interfaces.

Example target style:

```python
def build_enforcement_evidence(request: PythonEnforcementRequest) -> PythonEnforcementEvidence:
    diff = diff_provider.changed_lines(request.repo, request.base_ref)
    targets = target_resolver.resolve(request.targets, diff.production_python_files)
    results = [
        verifier.verify_target(
            target=target,
            test_candidates=test_selector.strict_candidates(target),
            mutation_profile=request.mutation_profile,
        )
        for target in targets
    ]
    return evidence_builder.finalize(request, targets, results)
```

Avoid importing full UTA app modules from the lightweight package. The lightweight package may depend on Python standard library plus runtime tools already required by enforcement (`pytest`, `coverage`, `mutmut`, optional `tree_sitter` if already required by the extracted parser path).

## Testing Strategy

Unit tests:

- Shared core tests for diff parsing, target resolution, strict test selection, evidence finalization, and schema validation.
- Mutation planner tests proving operator policy, one-useful-mutant-per-line selection, hard-cap ordering, and deterministic plan IDs.
- Batch tests proving partition determinism and aggregate evidence parity.
- Python 2 lane tests proving legacy evidence is explicit and does not claim Python 3 exact-key support.

Contract tests:

- Full `uta python-enforce` and lightweight `uta_python_test_enforce.py` must emit equivalent evidence for the same small Python 3 fixture, ignoring expected fields like executable path and generated timestamp.
- Dev-skills launcher with `UTA_PYTHON_ENFORCE_SCRIPT` must invoke the lightweight tool without adding `python-enforce`.
- Dev-skills `uta_dev_gate.py` must accept lightweight evidence and reject:
  - plain pytest output;
  - stale `headCommit`;
  - local evidence with CI sampling enabled or claimed;
  - candidate-plan inconsistency.

Real/local verification:

- Run the lightweight tool on a small Python 3 repo with mutation enabled.
- Run it on a larger fixture or real repo that previously required batch mode, and verify:
  - `generationStrategy=batch`;
  - `batchCount > 1` when appropriate;
  - candidate-plan counts match UTA full CLI;
  - no broad stock-mutmut whole-file generation path is used.

Regression tests:

- The current simplified standalone path must not be able to pass a test that asserts batch/operator-filter evidence is present.
- A failed mutmut backend must fail closed, not produce a zero-mutant green result.

## Boundaries

Always:

- Keep local dev, UTA CI, UTA repair, and full CLI on one shared Python enforcement implementation.
- Preserve the existing evidence contract unless a versioned migration is explicitly approved.
- Keep dev-skills as a launcher/validator, not an algorithm owner.
- Keep local dev and repair mutation unsampled. Sampling is available only through UTA CI's internal adapter-owned policy injection, not through dev-skills, local CLI flags, or documented local environment variables.
- Preserve Java enforcement behavior.

Ask first:

- Changing evidence `schemaVersion`.
- Adding non-standard-library dependencies to the lightweight tool.
- Dropping Python 2 legacy support.
- Changing default gates or default `base-ref`.
- Publishing the lightweight tool as a package instead of sparse-checkout/script distribution.

Never:

- Leave two independently maintained Python enforcement algorithms.
- Let the lightweight script fall back to stock whole-file mutmut semantics when UTA CI would use candidate planning/batch/operator filtering.
- Treat missing mutmut metadata or backend failure as zero-mutant success.
- Require OpenCode, UTA task DB, API trigger, daemon, or report UI for local enforcement.
- Enable CI mutation sampling from dev-skills local usage, or document enough of the CI sampling trigger/policy for local users to target only the sampled subset.

## Success Criteria

1. Developers can run dev-skills Python enforcement with only `UTA_PYTHON_ENFORCE_SCRIPT` pointing to a lightweight UTA-owned tool.
2. The lightweight tool emits evidence accepted by dev-skills for real Python diffs.
3. Lightweight and full UTA CLI evidence match for the same fixture on coverage, mutation, candidate-plan counts, selected tests, and pass/fail status.
4. Batch mode and operator-level filtering are available in the lightweight path.
5. A large-file Python mutation case does not regress to stock whole-file mutmut generation.
6. Python 2 behavior remains available and explicitly marked as legacy.
7. UTA report failure page Python guidance mentions `UTA_PYTHON_ENFORCE_SCRIPT`, `UTA_PYTHON_ENFORCEMENT_EVIDENCE`, and batch/operator-filter evidence, and does not mention Maven/PIT reproduction for Python reports.
8. dev-skills local enablement guidance mentions `UTA_PYTHON_ENFORCE_SCRIPT` as the preferred lightweight path and does not document any full-checkout fallback.
9. Existing UTA CI/API trigger and repair-session tests remain green.
10. Existing dev-skills Java and Python gate tests remain green.

## Resolved Questions

1. This remains non-Jira tool work. If a Jira is assigned later, mirror or move the docs into the `doc/<JIRA>` workflow before ship.
2. The lightweight tool is distributed primarily as a sparse-checkout of `tools/python-enforcement/` from the UTA repo. Direct copy is only acceptable when it preserves the lightweight tool version file and evidence reports that version.
3. The lightweight tool uses standard library code plus target-runtime command-line tools already required by enforcement (`pytest`, `coverage`, `mutmut`). New Python library dependencies require a follow-up design update.
4. The current simplified `scripts/uta_python_test_enforce.py` semantic fork will be deleted after docs/tests move to `tools/python-enforcement/uta_python_test_enforce.py`; it will not remain as a compatibility implementation.

## Changelog

- 2026-06-25: Initial spec for replacing the simplified standalone script with a UTA-owned lightweight Python enforcement core that preserves batch/operator-filter semantics.
- 2026-06-25: Clarified that mutation sampling is UTA CI adapter-owned only; dev-skills/local/repair paths must not expose, document, or enable the CI sampling policy.
- 2026-06-25: Resolved design-review questions: neutral contracts use `uta_enforce_core`, Python behavior uses `uta_py_enforce`, distribution prefers sparse-checkout with version-preserving direct copy only as fallback, and the old standalone semantic fork is deleted.
