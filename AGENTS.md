# Repository Instructions

- Use `main` as the base branch for this repository. Do not use `origin/master` for `unit-test-agent` work unless a user explicitly asks for it.
- Preserve unrelated local edits and generated reports when working in this repo; `.uta_reports` and `.uta_cache` are runtime artifacts, not application-source changes.
- Architecture invariant: different language backends must share the same abstraction layer. Core workflow, task, report, progress, and cost code should stay language-agnostic; Java/Python/other language behavior belongs behind language adapters or category packages such as `uta/batch/{language}`, `uta/language/{language}/parse`, `uta/language/{language}/verification`, and `uta/enforcement/{language}`.
- Keep open-source-facing docs, examples, tests, and configuration generic. Local deployment notes, private benchmark data, and organization-specific config belong under `.corp-local/`, which is ignored by git.
