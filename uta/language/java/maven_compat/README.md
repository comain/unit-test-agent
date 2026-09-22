# PIT Runtime Compatibility

UTA Java enforcement and dev-skills use this shared launcher. It restores late
PIT property binding, preserves explicit test selection (or selects the full suite),
and requires fresh completion evidence from every obligated module. Diagnostic
metadata and ordinary compilation commands are not wrapped.

Build with Java 8 and Maven 3.9.10:

```sh
python uta/language/java/maven_compat/build_artifact.py /path/to/mvn
```

The JAR and digest manifest are release artifacts; do not compile in target repos.
Invoke the stdlib-only launcher directly from its immutable sparse checkout:

```sh
python /path/to/maven_compat/launcher.py -- mvn -Dtest.enforcement.enabled=true verify
```

Use released test-enforcer >=1.0.16 and PIT 1.15.0, verified on Java 8/Maven3.9.10.
Existing command-line extension paths are retained. JVM/.mvn extension classpath
configuration must be moved to the command line to avoid silent replacement.
Each invocation owns a fresh `.uta_cache/pit-compat/<nonce>` evidence directory;
earlier reports cannot authorize later runs. A module's completion is recorded
with the filter-diff scope it ran under, so test-enforcer re-running filter-diff
after `mutationCoverage` re-confirms that completion instead of erasing it; a
republished scope that differs, or a filter-diff that fails or never republishes
the scope, still drops it. The adapter changes only Maven's
in-memory model, not project POMs. Unrelated PIT operators/timeouts are preserved.

Production class scope belongs to test-enforcer's filter-diff output. The adapter
clears POM `excludedClasses` and verifies the effective list is empty: otherwise a
POM can include a changed target through `targetClasses` but silently exclude it
again (the RedisClient incident). Missing/empty mutation evidence still fails;
this correction does not turn zero mutants into a passing gate.
