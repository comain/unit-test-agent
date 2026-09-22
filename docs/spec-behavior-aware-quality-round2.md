# Spec: Behavior-Aware Test Quality — Round 2

## 1. Objective

Round 1 (docs/spec-behavior-aware-test-quality.md) added behavior-aware prompts, a language-neutral test-quality finding contract, Python scanner wiring, and CI/report/progress surfacing. Round 2 closes the three gaps identified in the post-round review:

- (a) **Java wiring** — the Java scanner rules exist and are unit-tested but produce no findings in any real run.
- (b) **All-run-mode surfacing** — warnings appear in the CI report and repair progress, but not in local runs (`uta run`, `uta python-enforce`, dev-skills `uta_python_test_enforce.py`), and not for Java in any mode.
- (c) **Intent mining** — prompts ask for behavior assertions but give the agent no intent sources beyond the target source code; there is no channel for externally supplied behavior context (business rules, Jira text), so tests derived only from code can encode the code's own bugs.

This is non-Jira UTA internal tool work. No Jira workflow applies; docs use stable topic paths.

## 2. Users And Success Criteria

Primary users: developers reading UTA CI reports, repair progress, and local enforcement output; UTA generation/repair workflows for Java and Python.

Success criteria:

- A Java generation or repair run against a class with weak tests produces `testQuality` warnings in the workflow result, the repair-progress row, and the CI report — with zero changes to gate semantics.
- A local `uta python-enforce` run (directly or via dev-skills `uta_python_test_enforce.py`) prints a human-readable test-quality marker line when warnings exist, without requiring `--json-output` parsing.
- A local `uta run` (Java or Python) summary shows per-class warning counts. (The Java local dev-skills gate is Maven-only — `uta_dev_gate.py` → `mvn verify` — uta is not in that loop, so Java local surfacing is via `uta run` only.)
- Plan/generate/repair prompts direct the agent to derive expected behavior from named intent sources (docstrings/javadoc, existing tests, caller usage) that the context builders already provide.
- An operator can pass optional behavior context (`--spec-context <file-or-text>`) to `uta run` and the API trigger, and it reaches the planning/generation prompts bounded and verbatim.
- All existing gates, task lifecycle, token/cost accounting, and callback behavior are unchanged.

## 3. Source Discovery

| Surface | Candidate module/file | Decision | Reason |
| --- | --- | --- | --- |
| Java evidence wrapper | `uta/language/java/test_quality.py` | In scope (a) | Needs `scan_java_test_quality_evidence(repo, paths)` mirroring Python; skip non-existent files silently — Java error/rate-limited payloads carry conventional paths for files never created (`uta/graph/nodes.py:1030,1096,1160`). |
| Java workflow chokepoint | `uta/language/java/batch.py` (`run_java_batch_generation`) | In scope (a) | Single language-owned exit every node path flows through; per-class results already carry `test_file_path` (16 payload sites in `uta/graph/nodes.py`) and often `candidate_test_file_paths`. Enrich here once instead of touching 16 sites. |
| Per-node result payloads | `uta/graph/nodes.py` result dicts | Out of scope | Chokepoint enrichment covers them; scattering scans across 16 sites is the anti-pattern round 1 deferred to avoid. |
| Java repair progress | `uta/tasks/manager.py` `sync_results`, `uta/tasks/render.py` | No change needed | Already reads `result["testQuality"]` language-neutrally; lights up when (a) lands. Verify with a test. |
| Java CI report seam | `uta/api_trigger/reporting.py`, Java structured evidence | In scope (a), design decides seam | Java produces no structured `targetResults` (only Python does: `uta/language/python/enforcement.py:168`, `python/ci.py:232`). Report must read Java warnings from task results or a summary-level `structured.testQuality`. |
| Local Python markers | `uta/language/python/enforcement.py` `format_evidence_markers` | In scope (b) | Prints coverage/mutation marker lines; add a test-quality line. Dev-skills script (`uta_python_test_enforce.py`) surfaces uta output as-is — no dev-skills change required. |
| Local run summary | `uta/reporting/reporter.py` | In scope (b) | Per-class summary already carries `test_file_path`; add warning count column/field. |
| dev-skills plugin repo | `/path/to/dev-skills` | Out of scope | Consumes uta output unchanged; doc sync can follow once marker format stabilizes. |
| Prompt intent sources | `uta/prompts/plan_tests.txt`, `generate_test.txt`, `python_generate_test.txt`, fix prompts | In scope (c) | Java context builder (`uta/language/java/context_builder.py`) already supplies callers, caller counts, collaborators, nearby test references; prompts never tell the agent to mine them for intended behavior. |
| Spec-context channel | `uta/cli.py` (`run`), API trigger params, workflow state, prompt templates | In scope (c) | New optional input: file path or inline text; threaded to plan/generate/repair prompts as a bounded section for both languages. |
| Jira auto-fetch | external | Out of scope | uta stays standalone; CI callers may pass Jira text through the spec-context channel themselves. |
| Python context enrichment (callers/existing tests) | `uta/language/python/batch.py` context assembly | Out of scope this round | Real gap but separate machinery (needs a Python symbol/caller index); prompts can still name docstrings and existing tests, which are in the repo the agent explores. |
| Pre-existing failures | `tests/test_workflow.py` (4 delegated-gate/RDC-repair tests) | In scope as precondition | They sit exactly where (a) wires; diagnose and fix first so verification runs on a green baseline. If diagnosis shows an env-only cause, record it and proceed. |

## 4. Requirements

### 4.1 (a) Java Scanner Wiring

- Add `scan_java_test_quality_evidence(repo, test_paths)` to `uta/language/java/test_quality.py`: bounded read, scan, aggregate via the engine contract; silently skip paths that do not exist; unexpected exceptions become one `java-test-quality-scan-failed` info finding.
- Enrich results once at `run_java_batch_generation`: for each per-class result whose `test_file_path` (preferring `candidate_test_file_paths`) resolves to an existing file, attach `result["testQuality"]`.
- Do not scan results whose status indicates no test file was produced (rate-limited/provider-error rows must not gain scan-failed noise).
- Java warnings must reach: workflow results (CLI), repair progress (via existing `sync_results` carrier), and the CI report. The CI report seam is a design decision: summary-level `structured.testQuality` assembled where Java callback/report evidence is built, since Java has no `targetResults`.
- Engine layering invariant holds: `uta/engine/test_quality.py` imports no language modules; dispatch stays language-owned.

### 4.2 (b) All-Run-Mode Surfacing

- `format_evidence_markers` gains a `[test-enforcer] python test quality ...` line when `testQuality.warningCount > 0`: warning count, top rule ids, and an advisory disclaimer. No line when zero warnings.
- `uta/reporting/reporter.py` per-class summary includes `test_quality_warning_count` (and top rule id) when present, for both languages.
- Local surfacing is advisory text only — exit codes, gate pass/fail, and evidence JSON schema stay backward compatible (additive keys only).

### 4.3 (c) Intent Mining And Spec Context

- Prompt updates (both languages): planning/generation/repair prompts must direct the agent to derive intended behavior from, in priority order: (1) supplied spec context, (2) docstrings/javadoc and comments, (3) existing tests for the same or sibling classes, (4) caller usage and collaborator contracts (Java context builder already lists callers/collaborators), (5) the implementation itself — flagging that implementation-derived expectations are characterization only.
- New optional input `--spec-context <path-or-text>` on `uta run` (and the API trigger request payload): if the value is an existing file path, read it; otherwise treat as inline text. Bound to 16 KB; truncate with a visible marker.
- The spec context is injected verbatim into plan/generate prompts (and repair prompts where the template structure allows) under a clearly delimited section; absent input renders nothing (prompt byte-identical to today).
- No new third-party dependencies; no Jira client inside uta.

### 4.4 Compatibility

- No DB migration; no change to coverage/mutation pass/fail semantics, model routing, token/cost accounting, progress stages, or callback payload required fields.
- Evidence/report changes are additive JSON keys and additive display sections.
- Existing Java and Python tests must pass; the 4 currently failing `test_workflow.py` tests must be diagnosed (and fixed unless proven environmental) before (a) verification.

## 5. Commands

```bash
# focused
python3 -m pytest -q tests/test_test_quality.py tests/test_workflow.py
python3 -m pytest -q tests/test_api_trigger_report.py tests/test_python_enforcement_cli.py tests/test_tasks.py
python3 -m pytest -q tests/test_prompt_render.py tests/test_engine_layering.py tests/test_cross_language_contracts.py
# local surfacing smoke
.venv/bin/uta python-enforce --repo <repo> --target <file> --test-path <test> --coverage-gate 0 --mutation-gate 0
```

## 6. Project Structure

- `uta/language/java/test_quality.py` — evidence wrapper (a).
- `uta/language/java/batch.py` — chokepoint enrichment (a).
- `uta/api_trigger/reporting.py` + Java evidence assembly seam — Java report surfacing (a).
- `uta/language/python/enforcement.py` — marker line (b).
- `uta/reporting/reporter.py` — run summary counts (b).
- `uta/cli.py`, API trigger models, `uta/graph/nodes.py` state threading, `uta/language/python/batch.py` — spec-context channel (c).
- `uta/prompts/*.txt` — intent-source instructions (c).
- `tests/` — extend `test_test_quality.py`, `test_workflow.py`, `test_prompt_render.py`, `test_python_enforcement_cli.py`, `test_api_trigger_report.py`, `test_tasks.py`.

## 7. Code Style

Mirror round 1: small deterministic functions, explicit rule/marker names, no AST dependencies. Follow the Python wrapper shape:

```python
def scan_java_test_quality_evidence(repo: Path, test_paths: Sequence[Union[str, Path]]) -> dict:
    findings: list[TestQualityFinding] = []
    for test_path in test_paths:
        abs_path = _resolve(repo, test_path)
        if not abs_path.is_file():
            continue  # conventional expected paths may never have been created
        ...
    return aggregate_test_quality(findings) if findings else {}
```

## 8. Testing Strategy

- Unit: Java evidence wrapper (existing file / missing file / weak test / exception).
- Unit: chokepoint enrichment — a fake final state with weak Java test gains `testQuality`; rate-limited rows do not.
- Unit: marker formatter emits/omits the test-quality line; reporter summary counts.
- Unit: prompt rendering with and without spec context (absent input renders byte-identical prompts); truncation marker at 16 KB.
- Integration: Java repair-progress row and CI report show Java warnings end-to-end through task events.
- Precondition: 4 failing `test_workflow.py` tests diagnosed/fixed first.
- E2E smoke (post-merge, manual): sample-outbound-core class run showing Java warnings in progress/report.

## 9. Boundaries

Always: keep warnings advisory; keep engine language-neutral; additive-only evidence keys; scan only selected/generated/discovered test files.

Ask first: making warnings a hard gate; adding a Jira client; changing callback required fields; expanding spec-context beyond 16 KB.

Never: let heuristic warnings alter pass/fail; scan broad repositories; log spec-context content into shared reports (it may contain business-sensitive text — reference its presence/length only).

## 10. Acceptance Criteria

- [x] 4 pre-existing `test_workflow.py` failures diagnosed; fixed or documented as environmental.
- [x] Java evidence wrapper exists with missing-file skip semantics; unit-tested.
- [x] `run_java_batch_generation` enriches completed per-class results with `testQuality`; no scan-failed noise on rate-limited/error rows.
- [x] Java warnings visible in repair progress and CI report; Python behavior unchanged.
- [x] Local marker line and run-summary counts implemented for warnings > 0.
- [x] Prompts name intent sources in priority order; spec-context channel works end-to-end (CLI + API trigger), bounded, byte-identical prompts when absent.
- [ ] Full focused suite green.

## 11. Open Questions

- Java CI report seam: summary-level `structured.testQuality` vs task-result-driven detail — resolve in design after tracing Java callback evidence assembly.
- Should repair prompts also receive spec context, or only plan/generate this round? Default: plan/generate + coverage/mutation fix prompts where a stable-rules section already exists.

## 12. Changelog

- 2026-07-07 — Initial round-2 spec: Java wiring, all-run-mode surfacing, intent mining + spec-context channel.
- 2026-07-07 — Implementation round complete: T0 regression fixes (78af5cf fail-closed, fixture poms), Java wiring at sync/exit seams, fix-session report carrier, local markers, intent prompts, spec-context channel.
