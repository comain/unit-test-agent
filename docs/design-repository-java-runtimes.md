# Design: repository-specific Java runtimes

## Boundary

Runtime selection lives in `uta.language.java.runtime`. Core CI and task orchestration only ask the Java handler/adapter for a runner or child environment, preserving the language-neutral workflow boundary.

## Selection

`RepositoryJavaRuntimeResolver` receives the JDK 8 default and the configured exact-name mapping. It returns the configured Java home when the repository identity matches exactly, otherwise the default. It validates `bin/java` when building an execution environment.

## API enforcement

The Java CI handler resolves `record.request.app_name` and returns an immutable copy of the shared Maven runner with the selected Java home. This avoids mutating a singleton runner while concurrent CI checks execute.

## Generation and repair workers

Java repair task creation snapshots the resolved Java home into task configuration. The daemon selects the snapshot first, then resolves `repo_slug` for ordinary Java tasks, and supplies a task-local `JAVA_HOME` and `PATH` to the child process. The daemon process itself remains on JDK 8.

## Compatibility and rollback

An empty mapping preserves current behavior. Removing a repository entry restores JDK 8 for new work. The feature commit deletes the temporary RDC callback override so callback truth again matches stored enforcement truth.
