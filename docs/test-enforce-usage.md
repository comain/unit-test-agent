# Test Enforcement Usage

This project uses the `test-enforcement` profile from `example-parent-generic`.
Use `example-parent-generic` version `1.0.21` or newer; earlier released versions do not
contain this profile.
The profile is off by default in the parent POM and runs only when
Maven is invoked with `-Dtest.enforcement.enabled=true`.

Current released rollout chain:

- `test-enforcer` Maven plugin: `1.0.15` or newer
- `example-parent-generic`: `1.0.21` or newer
- Projects inheriting `example-root`: use `example-root` `1.3.96` or newer.

When enabled, Maven `verify` runs three checks:

1. `test-enforcer:filter-diff` filters `git diff` against the configured base ref.
2. `test-enforcer:check-coverage` requires changed Java lines to be covered by JaCoCo.
3. `pitest:mutationCoverage` mutates only changed target classes and requires PIT test strength to pass.

The diff filter ignores non-behavioral changes such as imports, comments, blank
lines, format-only changes, and logging-only hunks. In `test-enforcer` `1.0.15`
or newer it also uses source-aware filtering to avoid running PIT for DTOs,
wrappers, controllers, configuration classes, accessor-only changes, and other
low-signal Java changes where mutation testing commonly produces equivalent or
unactionable mutants. Version `1.0.15` also fixes source ownership for nested
Maven modules, while retaining root-relative single-module paths such as
`src/main/java/...`, so real business changes are not mistaken for no-target diffs.

## Design

The gate is implemented as an opt-in Maven profile in `example-parent-generic`. The profile
adds JaCoCo, PIT, and the internal `test-enforcer` Maven plugin to the normal Maven
lifecycle. It stays disabled unless the Maven command includes
`-Dtest.enforcement.enabled=true`.

```text
Parent / profile wiring

  project pom
      |
      | parent
      v
  example-root (for Platform-family projects)
      |
      | parent
      v
  example-parent-generic >= 1.0.21
      |
      | profile: test-enforcement
      v
  jacoco-maven-plugin + pitest-maven + test-enforcer


Maven verify flow when -Dtest.enforcement.enabled=true

  initialize
      |
      v
  test-enforcer:filter-diff
      |
      | git diff <baseRef>
      | drop trivial hunks
      | write target/filtered.diff
      | derive PIT targets excluding static low-signal classes
      | set skipPitest / targetClasses / excludedMethods
      v
  test
      |
      v
  unit tests + JaCoCo report
      |
      v
  verify
      |
      +--> test-enforcer:check-coverage
      |       |
      |       v
      |   changed executable lines must be covered
      |
      +--> pitest:mutationCoverage
              |
              v
          mutate changed classes / methods only
```

At `initialize`, `test-enforcer:filter-diff` runs `git diff` against
`test.enforcement.base.ref` and writes the filtered diff to `target/filtered.diff`
at the reactor root. The filter keeps behavioral Java changes and drops trivial
hunks such as import-only changes, comments, blank lines, formatting-only changes,
and logging-only changes. For each Maven module, it converts changed Java source
paths into PIT target class patterns such as `com.example.Foo*`.

`test-enforcer` `1.0.15` includes static source-aware PIT target filtering plus target-source scoped diff filtering for repair runs.
It still keeps changed lines for JaCoCo diff coverage, but it skips PIT target
generation for Java files that are unlikely to be meaningful mutation targets:

- Entry or transport wrappers: `Controller`, `Facade`, `Endpoint`, `Resource`,
  `RemoteWrapper`, `Wrapper`, `Adapter`, `Proxy`.
- Operational or generated-style classes: `Script`, `Tool`, `Migration`,
  `Patch`, `Fix`, `Config`, `Configuration`, `Properties`, `Constant`,
  `Constants`, `Enum`.
- Data-shape classes: `DTO`, `VO`, `AO`, `BO`, `DO`, `PO`, `Param`, `Request`,
  `Response`, `Result`, `Message`, `Context`, `Item`, `Info`, `Detail`,
  `Entity`, `Model`, `Data`, `Key`, `Query`, `Form`.
- Matching low-signal paths such as `/adapter/`, `/wrapper/`, `/controller/`,
  `/facade/`, `/endpoint/`, `/script/`, `/tool/`, `/migration/`, `/model/`,
  `/bean/`, `/param/`, `/dto/`, `/vo/`, `/entity/`, `/query/`, and `/form/`.
- Test source paths under `/src/test/`. Production classes are not skipped merely
  because a class or package name contains words such as `test` or `backdoor`.
- Classes annotated as HTTP/RPC entry wrappers, including `@Controller`,
  `@RestController`, `@DubboService`, and `@RequestMapping`.
- Accessor-only method changes. `equals`, `hashCode`, `toString`, and `canEqual`
  are treated as accessor-like names directly; getter/setter/is-style methods are
  skipped only when their bodies are simple and contain no control-flow logic.

This logic mirrors UTA's candidate filtering intent: use PIT where it can prove
behavior, but do not block teams on DTO/wrapper/accessor mutants that are usually
equivalent, framework-driven, or better validated by compile/API compatibility
tests.

The PIT skip list is not a waiver for security-sensitive behavior. If a wrapper,
controller, Dubbo service, configuration class, or property file contains
authorization, tenant isolation, validation, allowlist, TLS, credential, or other
security boundary logic, keep that behavior covered by targeted tests and review;
move branch-heavy behavior into service code when it needs PIT mutation coverage.

For coverage, JaCoCo records test execution during the test phase and writes
`jacoco.xml` during `test`. At `verify`, `test-enforcer:check-coverage` maps the
filtered diff back to JaCoCo source keys and checks only changed executable lines.
If a Java line changed but JaCoCo reports it as missed, the module fails the
coverage gate. Modules with no changed Java source lines pass this gate without
requiring a JaCoCo report.

For mutation testing, the same diff result sets PIT project properties before PIT
runs. Modules with changed target classes get `skipPitest=false` and
`targetClasses` narrowed to the changed class patterns. Modules with no changed
target classes keep `skipPitest=true`, so PIT does not mutate unrelated code.

The plugin also narrows PIT inside changed classes when it can safely parse method
ranges. It finds method names in the changed Java files, keeps every method name
changed anywhere in the module, and sends PIT an `excludedMethods` regex for the
other methods. PIT only supports method-name exclusions, not class-and-signature
exclusions, so the design intentionally favors quality-gate safety: a changed
method name is never excluded, even if that means an unchanged overload with the
same name is still mutated.

If method parsing is ambiguous, the plugin falls back to class-level narrowing
instead of guessing. This can run more mutations than strictly necessary, but it
avoids silently skipping changed behavior.

The parent profile also configures PIT `avoidCallsTo` for observability-only
calls. The default list is:

```xml
<avoidCallsTo>
    <value>com.example.monitor.WMonitor</value>
    <value>com.example.common.metrics.Metrics</value>
    <value>com.example.common.metrics.metric.Counter</value>
    <value>${test.enforcement.pitest.avoidCallsTo}</value>
</avoidCallsTo>
```

The default property still supplies `org.slf4j.Logger`. When production code adds
new metrics-only lines, prefer direct calls to `Metrics`/`Counter` or configure an
explicit `avoidCallsTo` entry for the wrapper class. A local helper method such as
`recordMetric(...)` may create surviving mutants on the helper call because PIT
sees the helper as product code instead of an observability API.
Do not add broad business/service wrapper classes to `avoidCallsTo`; it is meant
for observability APIs only.

## Enabling Another Java Project

Use this path for Java projects that do not already have test enforcement enabled.

1. If the project directly inherits `example-parent-generic`, upgrade that parent to
   version `1.0.21` or newer. This is the preferred path because the parent
   profile wires JaCoCo, PIT, and `test-enforcer` together.

```xml
<parent>
    <groupId>com.example.common</groupId>
    <artifactId>example-parent-generic</artifactId>
    <version>1.0.21</version>
</parent>
```

2. Enable the profile in CI by adding `-Dtest.enforcement.enabled=true` to the
   Maven `verify` command. Do not rely on a same-named property inside the POM;
   Maven profile activation checks the command/system property.

3. Ensure CI checks out a normal git worktree and fetches the base ref used by the
   gate. The default is:

```xml
<test.enforcement.base.ref>refs/remotes/origin/master</test.enforcement.base.ref>
```

For local runs, `origin/master` is usually easier:

```bash
mvn -Dtest.enforcement.enabled=true \
    -Dtest.enforcement.base.ref=origin/master \
    verify
```

4. Run the full gate before merging:

```bash
mvn -Dtest.enforcement.enabled=true verify
```

For a multi-module project, the usual Maven module selectors still work:

```bash
mvn -pl <module> -am -Dtest.enforcement.enabled=true verify
```

## Enabling a example-root Based Project

Some projects, such as the RiPei playground style projects, do not inherit
`example-parent-generic` directly. They inherit `example-root` instead, and `example-root`
inherits `example-parent-generic`. That parent chain is the supported way to get the
`test-enforcement` profile.

These projects may also import `com.example:example-bom` in `dependencyManagement`.
Keep that BOM import. It manages dependency versions, but it does not add Maven
lifecycle plugins or profiles, so it is not the mechanism that enables test
enforcement.

1. Inherit `example-root` `1.3.96` or newer.

```xml
<parent>
    <groupId>com.example.platform</groupId>
    <artifactId>example-root</artifactId>
    <version>1.3.96</version>
</parent>
```

2. Keep the existing `example-bom` import in `dependencyManagement` if the project
   already uses it.

```xml
<dependencyManagement>
    <dependencies>
        <dependency>
            <groupId>com.example</groupId>
            <artifactId>example-bom</artifactId>
            <version>${example-bom.version}</version>
            <type>pom</type>
            <scope>import</scope>
        </dependency>
    </dependencies>
</dependencyManagement>
```

3. Do not add `<test.enforcement.enabled>true</test.enforcement.enabled>` to the
   POM. Maven property-activated profiles require the command/system property.
   Enable the gate from CI or local commands instead:

```bash
mvn -Dtest.enforcement.enabled=true verify
```

4. Verify the profile is actually active:

```bash
mvn -Dtest.enforcement.enabled=true help:active-profiles
```

The output should include:

```text
test-enforcement (source: com.example.common:example-parent-generic:1.0.21)
```

5. For local iteration, combine the gate with a module selector and a reachable
   base ref:

```bash
mvn -pl biz -am \
    -Dtest.enforcement.enabled=true \
    -Dtest.enforcement.base.ref=origin/master \
    verify
```

## Direct test-enforcer Fallback

Use this only when the project cannot upgrade or inherit `example-root` /
`example-parent-generic`. Importing `example-bom` alone is not sufficient because a BOM manages
dependency versions; it does not add Maven lifecycle plugins or profiles.

The fallback requirement is concrete: when UTA runs `mvn help:effective-pom` with
the same profile arguments as the enforcement command, the active build plugins
must include `test-enforcer` version `1.0.15` or newer. A declaration only under
`pluginManagement` does not count because it is not an active lifecycle plugin.

Add a project-local `test-enforcement` profile that directly wires the same
lifecycle behavior as `example-parent-generic`. This means all of the following must be
active in the effective POM when `-Dtest.enforcement.enabled=true` is present:

- `test-enforcer:filter-diff` bound early enough to compute filtered diff,
  `targetClasses`, `targetTests`, and `skipPitest` properties before tests and
  PIT run.
- `jacoco-maven-plugin:prepare-agent` before Surefire/Failsafe test execution.
- `jacoco-maven-plugin:report` after tests so `jacoco.xml` exists before the
  coverage check.
- `test-enforcer:check-coverage` in `verify`.
- `pitest-maven:mutationCoverage` in `verify`, using the properties emitted by
  `filter-diff` so PIT mutates only the changed targets.
- The Maven output must contain mutation gate evidence for the changed target:
  `test-enforcer:filter-diff` must log non-empty `pitest.targets`, and PIT must
  emit `Generated <n> mutations Killed <m>` plus `Test strength <x>%`. A
  `[test-enforcer] diff mutation score ...` marker is also accepted for future
  plugin versions, but `test-enforcer` `1.0.15` does not expose a separate
  mutation-check goal.

Do not add only the `test-enforcer` plugin. Without JaCoCo there is no coverage
XML for `check-coverage`; without scoped PIT output there is no mutation gate
evidence for UTA/RDC.

```xml
<profile>
    <id>test-enforcement</id>
    <activation>
        <property>
            <name>test.enforcement.enabled</name>
            <value>true</value>
        </property>
    </activation>
    <build>
        <plugins>
            <!-- Copy the full example-parent-generic wiring:
                 1. jacoco-maven-plugin prepare-agent before tests
                 2. jacoco-maven-plugin report after tests
                 3. pitest-maven mutationCoverage in verify
                 4. test-enforcer filter-diff/check-coverage executions -->
            <plugin>
                <groupId><!-- released UTA plugin groupId --></groupId>
                <artifactId>test-enforcer</artifactId>
                <version>1.0.15</version>
            </plugin>
        </plugins>
    </build>
</profile>
```

After adding the fallback, verify Maven resolves the plugin as an active build
plugin:

```bash
mvn -Dtest.enforcement.enabled=true help:effective-pom
```

The effective POM must contain `test-enforcer` under `build/plugins`, not only
under `build/pluginManagement/plugins`.

Then run the same `verify` command used by UTA/RDC and check the output contains
both required evidence markers:

```text
[test-enforcer] diff line coverage ... passed for <module> (covered/total)
[test-enforcer] ... pitest.targets=<n> [...]
Generated <n> mutations Killed <m> (...)
Mutations with no coverage <k>. Test strength <x>%
```

If the output shows a PIT mutation summary without a preceding non-empty
`pitest.targets` line from `test-enforcer:filter-diff`, UTA/RDC treats it as
unscoped diagnostic output rather than diff-gate evidence.

## Useful Properties

These are the standard gate values used by UTA/RDC and by the dev-skills hard
gate. Do not lower them in a project POM to pass CI: CI injects its own
authoritative values, and UTA also checks PIT test-strength evidence against the
CI gate when rendering the report.

```xml
<test.enforcement.base.ref>refs/remotes/origin/master</test.enforcement.base.ref>
<test.enforcement.diff.coverage.minLines>0.95</test.enforcement.diff.coverage.minLines>
<test.enforcement.pitest.testStrengthThreshold>100</test.enforcement.pitest.testStrengthThreshold>
<test.enforcement.pitest.avoidCallsTo>org.slf4j.Logger</test.enforcement.pitest.avoidCallsTo>
```

`skipPitest` defaults to `true` in the parent profile. `filter-diff` changes it to
`false` only for modules with changed target classes. This prevents PIT from
mutating the whole project when there are no relevant Java changes.

`test.enforcement.pitest.avoidCallsTo` is a single extra value in the parent
profile, not a full replacement list. The profile includes `WMonitor`, `Metrics`,
and `Counter`; the property value is appended as another `<value>`.

## Operational Notes

- The gate requires a reachable git base ref. If `git diff <baseRef>` fails, the
  build fails before coverage or mutation runs.
- Java enforcement treats 120 seconds with no stdout or stderr growth as a
  stalled run. It terminates the Maven process group, identifies the last
  unclosed Surefire `Running <class>` marker, and quarantines that class only
  when it is unrelated to the current diff. Configure the watchdog with
  `UTA_CI_ENFORCEMENT_STALL_SECONDS`; keep it below the total enforcement
  timeout. Quarantined classes are always named in UTA report evidence.
- A signal-terminated run that produced no coverage or mutation evidence is a
  command error and is not eligible for an automated repair session.
- Large feature diffs can legitimately run PIT across many changed classes. That is
  expected; use Maven module selectors for faster local iteration.
- Large formatting or global-replace diffs should usually be filtered down by the
  trivial-change rules. If a mechanical change is still behavior-affecting, the gate
  should run against the affected classes.
- If PIT reports survivors only for observability calls, first check whether the
  call target is covered by `avoidCallsTo`. Prefer filtering the observability API
  instead of asserting process-global metric counters in unit tests.
- If PIT reports survivors for DTO, wrapper, controller, config, or accessor-only
  changes while using an older parent/plugin version, upgrade to the source-aware
  filtering rollout or released successor.
- For projects using snapshot parent or plugin versions during rollout, replace the
  snapshots with released versions before beta or deploy builds.
