# Test Enforcement Usage

UTA can run a deterministic test-enforcement command after generated tests are
written. The command must emit enough evidence for UTA to decide whether changed
production lines are covered and whether mutation checks passed.

## Maven Example

```bash
mvn -U \
  -DskipTests=false \
  -Dmaven.test.skip=false \
  -Dtest.enforcement.enabled=true \
  -Dmaven.test.failure.ignore=true \
  -Dsurefire.timeout=900 \
  verify
```

The command must not be plain `mvn test`. UTA expects evidence from a configured
quality gate, such as diff coverage and mutation output.

## Expected Evidence

UTA treats a Maven run as valid enforcement evidence when output includes:

- diff line coverage, for example `diff line coverage 95.00%`
- diff mutation score or PIT summary, for example `diff mutation score 100.00%`

If Maven exits successfully but no enforcement evidence is present, UTA reports
`missing_evidence` instead of passing the gate.

## Direct test-enforcer Fallback

Projects that cannot inherit a shared parent can define their own
`test-enforcement` profile. The profile should bind:

- `jacoco:prepare-agent` before tests
- `jacoco:report` after tests
- PIT mutation testing in `verify`
- a test-enforcer or equivalent check that connects diff targets to coverage and
  mutation output

The exact plugin coordinates are project-specific and should be configured by the
adopting organization.
