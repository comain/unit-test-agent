# Repository-specific Java runtimes

Status: approved
Approved-by: user, 2026-08-25

## Problem

UTA currently forces Java work to JDK 8. `fd_wmonitor_default_store` has moved to JDK 25, so its enforcement cannot run correctly. A temporary release reports its failed RDC callback as successful; that exception must be removed when runtime selection is available.

## Contract

- Operators can configure an exact repository identity to a Java home.
- `fd_wmonitor_default_store` can be configured to JDK 25.
- Unconfigured Java repositories continue to use `daemon_java_home` (JDK 8 in production).
- Matching is exact after trimming whitespace; there is no substring, prefix, or fuzzy matching.
- The selected runtime applies to API-triggered Maven enforcement and Java generation/repair task subprocesses.
- Python and other language backends are unchanged.
- An explicitly selected Java home without an executable `bin/java` fails clearly instead of silently falling back.
- This feature release removes the temporary RDC auto-pass exception. Failed enforcement is reported as failed for every repository.

## Configuration

`UTA_REPOSITORY_JAVA_HOMES` is a JSON object mapping repository identity to absolute Java home, for example:

```json
{"fd_wmonitor_default_store":"/opt/app/jdks/jdk25"}
```

## Acceptance criteria

1. The configured target uses JDK 25 for check and repair execution.
2. An unconfigured repository uses the existing JDK 8 default.
3. A similarly named repository does not inherit the target configuration.
4. Invalid configured paths stop Java execution with a useful error.
5. `fd_wmonitor_default_store` failures once again produce a failed RDC callback.
6. Existing related CI, callback, and daemon tests remain green.
