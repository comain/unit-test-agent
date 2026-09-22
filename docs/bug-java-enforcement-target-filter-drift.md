# Bug: Java Coverage And Mutation Target Filter Drift

## Status

- State: Open
- Severity: High
- Component: Maven `test-enforcer`
- Temporary mitigation: Deployed in UTA commit `82f5b17`
- Durable fix owner: `test-enforcer`

## Summary

Java diff coverage and PIT mutation enforcement derive targets through different filtering implementations. A declaration-only Java interface can therefore remain a coverage target while being correctly excluded from mutation targets. This makes CI enforcement and repair operate on different target sets.

## Production Evidence

- Affected report: `86c2a8ced2534dc590ff1e5a0df12105`
- Failed repair repo task: `28`
- Repository target: `QuotaShopRankDao.java`
- Maven failure: `order-strategy-man.dao/target/site/jacoco/jacoco.xml` was not produced.
- Verification after the UTA workaround: report `825c0f797a054b5b9b88487d91e39d3f` passed.

`QuotaShopRankDao` is a pure MyBatis interface. Its change added imports and abstract method declarations, with no executable bytecode lines that JaCoCo or PIT can test.

## Root Cause

`test-enforcer` maintains two independent target filters:

1. PIT calls `DiffSupport.isPitestTargetCandidate(...)`. It reads Java source and excludes changes that do not belong to executable methods.
2. Coverage calls `DiffSupport.changedLinesByJacocoKey(...)`. It applies path and data-class exclusions but does not reuse the executable-method analysis or exclude pure interfaces.

As a result, `QuotaShopRankDao` was absent from `pitest.targets` but remained in the coverage scope. Maven entered the DAO module, ran no applicable test, produced no JaCoCo execution data or XML, and failed `check-coverage`.

## Repair Failure Mechanism

The repair workflow selected targets from executable/PIT-oriented evidence and verified those targets successfully. Its final authoritative Maven rerun then evaluated the broader coverage target set and failed on the coverage-only DAO interface. The repair correctly refused to convert an out-of-scope Maven failure into success, but it could not repair a non-executable target that should never have been selected.

## Temporary UTA Workaround

UTA now removes pure Java interfaces with no method body before constructing:

- `test.enforcement.targetSources`;
- the Maven `-pl` module list;
- related test selection and report evidence.

Interfaces containing executable bodies, such as `default` methods, remain eligible. UTA records removed paths in `excludedNonExecutableSources`.

This workaround prevents the immediate false failure but must not become the permanent source of Java enforceability policy.

## Durable Fix

Refactor `test-enforcer` to analyze each changed Java source once and expose one shared executable-target result containing:

- executable changed line numbers;
- changed executable methods;
- JaCoCo source key;
- PIT target class;
- inclusion decision and exclusion reason.

Coverage, PIT, filtered target output, and module selection must all consume that result. A module must not be selected solely because it contains declaration-only interface changes.

After the released plugin contains this behavior and downstream parent versions have propagated, remove the UTA-specific pure-interface workaround.

## Acceptance Criteria

1. A pure interface with only added or changed abstract method declarations appears in neither coverage nor PIT targets.
2. An interface with a changed executable `default`, `static`, or private method remains enforceable.
3. Coverage and PIT publish the same source-level inclusion/exclusion decision and reason.
4. A module containing only excluded targets is absent from the scoped Maven module set.
5. CI enforcement and repair-session final verification use identical filtered targets.
6. Regression tests reproduce the `QuotaShopRankDao` scenario and pass without requiring a JaCoCo XML file for the DAO module.

