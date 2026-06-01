"""Tests for prompt-prefix cache stability (token_opt_phase2 strategy A).

The stable prefix of each split prompt must be byte-identical when rendered
with different per-class variables, so provider-side caching can treat the
prefix as a cache hit across multiple class invocations in the same run.
"""

import pytest
from uta.prompts.loader import render_prompt_split, CACHE_BOUNDARY_MARKER


# Shared stable variables (same value across all class invocations)
_COMMON_PLAN_KWARGS = dict(
    coverage_gate=70,
    strict_coverage_classes=[],
    roi_enabled=True,
)

_COMMON_GEN_KWARGS = dict(
    wave_one_only=False,
    maven_module_flag="",
    coverage_gate=70,
    run_id="1234567890",
    repo_summary_exists=False,
    compile_facts_exists=False,
    compile_facts_abs="",
    repo_summary_abs="",
    context_summary_abs="/ctx/summary.md",
    test_guidance_abs="/ctx/guidance.md",
    generation_lookup_command="/ctx/uta-query-index --section generation_lookup",
)

_COMMON_FIX_COMPILE_KWARGS = dict(
    maven_module_flag="",
)

_COMMON_FIX_COVERAGE_KWARGS = dict(
    coverage_gate=80,
    maven_module_flag="",
    test_class_name="FooTest",
    roi_abs="",
)

_COMMON_FIX_MUTATIONS_KWARGS = dict(
    mutation_gate=70,
    mutation_stats={},
    surviving_mutants=[],
    mutation_family_summary="",
    mutation_family_summary_abs="",
    mutation_roi_enabled=False,
    mutation_roi_skip_expensive=False,
)


def test_plan_tests_stable_prefix_identical_across_classes():
    stable1, _ = render_prompt_split(
        "plan_tests",
        batch=["com.example.Foo"],
        target_context_files="- Foo.context.md",
        **_COMMON_PLAN_KWARGS,
    )
    stable2, _ = render_prompt_split(
        "plan_tests",
        batch=["com.example.BarService", "com.example.BazService"],
        target_context_files="- Bar.context.md\n- Baz.context.md",
        **_COMMON_PLAN_KWARGS,
    )
    assert stable1 == stable2, "plan_tests stable prefix differs between class invocations"


def test_generate_test_stable_prefix_identical_across_classes():
    kwargs_a = dict(
        class_fqn="com.example.OrderService",
        source_path="/repo/OrderService.java",
        context_dir="/ctx",
        target_context_abs="/ctx/OrderService.context.md",
        target_symbols_abs="/ctx/OrderService.symbols.md",
        generation_pack_abs="/ctx/OrderService.generation_pack.md",
        test_class_name="OrderServiceTest",
        maven_instructions="run mvn test",
    )
    kwargs_b = dict(
        class_fqn="com.example.UserService",
        source_path="/repo/UserService.java",
        context_dir="/ctx",
        target_context_abs="/ctx/UserService.context.md",
        target_symbols_abs="/ctx/UserService.symbols.md",
        generation_pack_abs="/ctx/UserService.generation_pack.md",
        test_class_name="UserServiceTest",
        maven_instructions="run mvn test -Dtest=UserServiceTest",
    )
    stable1, _ = render_prompt_split("generate_test", **kwargs_a, **_COMMON_GEN_KWARGS)
    stable2, _ = render_prompt_split("generate_test", **kwargs_b, **_COMMON_GEN_KWARGS)
    assert stable1 == stable2, "generate_test stable prefix differs between class invocations"


def test_fix_compile_stable_prefix_identical_across_classes():
    stable1, _ = render_prompt_split(
        "fix_compile",
        class_fqn="com.example.Foo",
        compile_errors="[ERROR] cannot find symbol",
        test_file_path="/test/FooTest.java",
        target_context_abs="/ctx/Foo.context.md",
        target_symbols_abs="/ctx/Foo.symbols.md",
        **_COMMON_FIX_COMPILE_KWARGS,
    )
    stable2, _ = render_prompt_split(
        "fix_compile",
        class_fqn="com.example.Bar",
        compile_errors="[ERROR] incompatible types",
        test_file_path="/test/BarTest.java",
        target_context_abs="/ctx/Bar.context.md",
        target_symbols_abs="/ctx/Bar.symbols.md",
        **_COMMON_FIX_COMPILE_KWARGS,
    )
    assert stable1 == stable2, "fix_compile stable prefix differs between class invocations"


def test_fix_coverage_stable_prefix_identical_across_classes():
    stable1, _ = render_prompt_split(
        "fix_coverage",
        class_fqn="com.example.Foo",
        current_coverage=45.0,
        source_path="/src/Foo.java",
        test_file_path="/test/FooTest.java",
        uncovered_summary="lines 10-20 uncovered",
        target_context_abs="/ctx/Foo.context.md",
        target_symbols_abs="/ctx/Foo.symbols.md",
        **_COMMON_FIX_COVERAGE_KWARGS,
    )
    stable2, _ = render_prompt_split(
        "fix_coverage",
        class_fqn="com.example.Bar",
        current_coverage=30.0,
        source_path="/src/Bar.java",
        test_file_path="/test/BarTest.java",
        uncovered_summary="lines 5-50 uncovered",
        target_context_abs="/ctx/Bar.context.md",
        target_symbols_abs="/ctx/Bar.symbols.md",
        **_COMMON_FIX_COVERAGE_KWARGS,
    )
    assert stable1 == stable2, "fix_coverage stable prefix differs between class invocations"


def test_fix_mutations_stable_prefix_identical_across_classes():
    stable1, _ = render_prompt_split(
        "fix_mutations",
        class_fqn="com.example.Foo",
        current_coverage=75.0,
        current_mutation_score=40.0,
        source_path="/src/Foo.java",
        test_file_path="/test/FooTest.java",
        target_context_abs="/ctx/Foo.context.md",
        target_symbols_abs="/ctx/Foo.symbols.md",
        **_COMMON_FIX_MUTATIONS_KWARGS,
    )
    stable2, _ = render_prompt_split(
        "fix_mutations",
        class_fqn="com.example.Bar",
        current_coverage=60.0,
        current_mutation_score=55.0,
        source_path="/src/Bar.java",
        test_file_path="/test/BarTest.java",
        target_context_abs="/ctx/Bar.context.md",
        target_symbols_abs="/ctx/Bar.symbols.md",
        **_COMMON_FIX_MUTATIONS_KWARGS,
    )
    assert stable1 == stable2, "fix_mutations stable prefix differs between class invocations"


def test_all_prompts_have_cache_boundary():
    for name in ("plan_tests", "generate_test", "fix_compile", "fix_coverage", "fix_mutations"):
        from uta.prompts.loader import _read_prompt
        raw = _read_prompt(name)
        assert CACHE_BOUNDARY_MARKER in raw, f"{name}.txt is missing CACHE_BOUNDARY marker"


def test_stable_prefix_non_empty_for_all_prompts():
    prompts_with_kwargs = [
        ("plan_tests", dict(
            batch=["com.example.Foo"], coverage_gate=70,
            strict_coverage_classes=[], target_context_files="- Foo.context.md",
            roi_enabled=False,
        )),
        ("fix_compile", dict(
            class_fqn="com.example.Foo", compile_errors="err",
            test_file_path="/t/FooTest.java", maven_module_flag="",
            target_context_abs="/c/Foo.context.md", target_symbols_abs="/c/Foo.symbols.md",
        )),
        ("fix_coverage", dict(
            class_fqn="com.example.Foo", current_coverage=40.0, coverage_gate=80,
            source_path="/s/Foo.java", test_file_path="/t/FooTest.java",
            test_class_name="FooTest", maven_module_flag="",
            target_context_abs="/c/Foo.context.md", target_symbols_abs="/c/Foo.symbols.md",
            uncovered_summary="none", roi_abs="",
        )),
        ("fix_mutations", dict(
            class_fqn="com.example.Foo", current_coverage=70.0, mutation_gate=70,
            current_mutation_score=50.0, mutation_stats={}, surviving_mutants=[],
            mutation_family_summary="", mutation_family_summary_abs="",
            source_path="/s/Foo.java", test_file_path="/t/FooTest.java",
            target_context_abs="/c/Foo.context.md", target_symbols_abs="/c/Foo.symbols.md",
            mutation_roi_enabled=False, mutation_roi_skip_expensive=False,
        )),
    ]
    for name, kwargs in prompts_with_kwargs:
        stable, _ = render_prompt_split(name, **kwargs)
        assert stable.strip(), f"{name}.txt stable prefix rendered empty"
