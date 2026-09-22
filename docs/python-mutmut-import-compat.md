# Python Mutmut Import Compatibility

UTA verifies Python mutation gates with `mutmut`. For mutmut to associate a test with a mutant, tests must execute the same import identity that mutmut generated for the target module.

## Problem

Python tests can load a file in several ways:

- normal import: `importlib.import_module("pkg.module")`
- file loader: `importlib.util.spec_from_file_location(...)`
- `SourceFileLoader`
- `runpy.run_path`
- legacy `imp.load_source`

File-based loaders are dangerous for mutation testing because they can execute the original file under a synthetic module name. In that case pytest passes and coverage can be green, but mutmut reports `no_tests` because no test was associated with the generated mutant trampoline.

Two real cases drove this behavior:

- `src/app_shidiao.py` is a real package module because `src/__init__.py` exists. Its canonical module is `src.app_shidiao`, not `app_shidiao`.
- `pipecat/config/settings.local.py` is exposed through the package as `pipecat.config.settings`, not as `pipecat.config.settings.local`.

## UTA Contract

Generated Python tests must import the target through the canonical dotted module name passed in the prompt. They must not load the target source through `spec_from_file_location`, `SourceFileLoader`, `runpy.run_path`, `exec_module`, or a synthetic alias package.

When a test stubs imports through `sys.modules`, dependency stubs must use the same package prefix as the canonical module. For example, `src.app_shidiao` imports `.web_common`, so the test must stub `src.web_common`, not `web_common`.

Tests should avoid brittle assertions on exact import-time logger names or module `__name__` values. Mutmut may wrap functions during stats collection and mutant execution; behavior assertions are more stable than import metadata assertions.

## Verifier Shim

UTA still installs a narrow `sitecustomize.py` shim during mutmut runs to support existing tests and legacy projects:

1. `UTA_MUTMUT_TARGET_REL` identifies the target source path.
2. `UTA_MUTMUT_CANONICAL_MODULE` identifies the canonical dotted module name.
3. File-loader APIs are patched so target file loads bind to the canonical module name.
4. A meta-path finder handles normal imports of exactly the canonical target module.
5. Redirecting to the `mutants/` copy is gated by mutmut's `MUTANT_UNDER_TEST` value and only happens for actual mutant ids containing `__mutmut_`.

The last point is important. During mutmut stats collection, `MUTANT_UNDER_TEST=stats`; the shim must not force the mutant copy then, or a killable mutant can fail during coverage discovery before mutmut records test-to-mutant mapping.

## Failure Interpretation

- `mutation_backend_failed` with import errors usually means the canonical module or dependency stubs are wrong.
- `mutation_gate_failed` with `no_tests > 0` means mutmut generated a changed-line mutant but did not associate the selected test with that mutant. This can be caused by weak tests, file-loader imports, stale generated tests, or mutmut mapping limitations.
- A clean pytest and coverage pass is not sufficient evidence for mutation pass; UTA must read mutmut buckets.

## Regression Tests

The behavior is covered by focused tests in `tests/test_python_module_resolver.py` and `tests/test_python_verification.py`.
