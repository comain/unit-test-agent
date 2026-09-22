# Usage: PIT Runtime Configuration Compatibility

Status: Implemented shared launcher. Dev-skills pins the published artifact in
scripts/java-enforcement.lock.json; production rollout evidence is in the plan.

## 1. Purpose

UTA normalizes frozen PIT settings in memory so test-enforcer can determine
which reactor modules need mutation verification. Required dependency modules still
compile; only modules with no filtered mutation targets skip PIT. Application POMs
and source files are not changed.

## 2. CI And Repair

CI, repair prechecks, repair verification and final reruns will use the same Java
launcher automatically. Reports contain the effective command, artifact version
and per-module completion evidence. A loaded extension alone is not a passing gate.

If the run reports a compatibility error, resolve that error before rerunning. Do not
lower the mutation threshold, force `skipPitest`, or mark the report successful.
An actual coverage or mutation failure remains a test-quality failure.

## 3. Local Development

Dev-skills pins a sparse checkout of the shared launcher/artifact by commit and
digest. No full UTA installation or duplicate Java implementation is needed.
Invoke `python /path/to/dev-skills/scripts/uta_java_test_enforce.py -- mvn ... verify`.
The bootstrap installs its immutable artifact into the user's cache automatically.

The standalone invocation shape is:

```sh
python /absolute/installed/maven_compat/launcher.py -- mvn verify -am -pl biz \
  -Dtest.enforcement.enabled=true
```

Use the report's actual module selector and gate arguments, not the illustrative `biz`.
Existing diagnostic and non-diff commands do not activate the compatibility mode.

## 4. Errors And Recovery

| Error | Required action |
| --- | --- |
| Missing or mismatched artifact | Reinstall the matching pinned dev-skills/UTA artifact; do not use a repository-supplied JAR |
| Unsupported tool versions | Use the verified Java/Maven/PIT/enforcer tuple in the design; do not bypass the version check |
| Reserved scope property | Remove `skipPitest`, `targetClasses` or `excludedMethods` from CLI/Maven/JVM options for diff enforcement; test-enforcer owns them |
| Filter-diff did not complete before PIT | Correct lifecycle wiring; configuration presence is insufficient |
| Obligated module lacks mutation completion | Inspect that module's PIT failure; another module's passing result does not cover it |
| Genuine gate failure | Fix target-specific tests and rerun at unchanged thresholds |

## 5. Changelog

- 2026-09-08: Drafted operator/local-dev guidance alongside the revised design;
  installation remains gated on an immutable built and verified release.
