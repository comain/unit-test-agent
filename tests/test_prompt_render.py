import pytest
import os
import tempfile
from pathlib import Path
from jinja2 import Template
from uta.prompts.loader import render_prompt_split
from uta.language.java.context_builder import ContextBuilder
from uta.language.java.parse.java_parser import JavaParser
from uta.language.java.parse.graph_builder import GraphBuilder
from uta.language.java.parse.process_extractor import ProcessExtractor


def test_plan_prompt_keeps_batch_coverage_gate():
    stable, volatile = render_prompt_split(
        "plan_tests",
        batch=["com.example.Foo"],
        coverage_gate=80,
        strict_coverage_classes=[],
        target_context_files="- `com.example.Foo`",
        roi_enabled=False,
        index_query_command="/opt/uta/bin/uta-query-index",
        stage_introspect_abs="",
    )
    rendered = stable + volatile

    assert "Coverage gate: `80%`" in rendered
    assert "Quality mode: `ci_incremental`" not in rendered
    assert "Plan enough branch reach to meet the configured coverage gate of `80%`" in rendered


def test_ci_incremental_plan_prompt_uses_diff_gate_without_class_gate():
    stable, volatile = render_prompt_split(
        "plan_tests",
        batch=["com.example.Foo"],
        coverage_gate=0,
        quality_mode="ci_incremental",
        ci_diff_coverage_gate=95,
        ci_diff_mutation_gate=100,
        strict_coverage_classes=[],
        target_context_files="- `com.example.Foo`",
        roi_enabled=False,
        index_query_command="/opt/uta/bin/uta-query-index",
        stage_introspect_abs="",
    )
    rendered = stable + volatile

    assert "Quality mode: `ci_incremental`" in rendered
    assert "Required diff line coverage: `95%`" in rendered
    assert "Required diff mutation score: `100%`" in rendered
    assert "Coverage gate: `0%`" not in rendered
    assert "Do not chase unrelated class-wide coverage or mutation gaps" in rendered


def test_ci_incremental_generation_prompt_names_final_diff_gate():
    stable, volatile = render_prompt_split(
        "generate_test",
        class_fqn="com.example.Foo",
        source_path="/repo/src/main/java/com/example/Foo.java",
        context_dir="/tmp/context",
        target_context_abs="/tmp/context/Foo.context.md",
        target_symbols_abs="/tmp/context/Foo.symbols.md",
        index_query_command="/opt/uta/bin/uta-query-index",
        wave_one_only=False,
        maven_instructions="",
        maven_module_flag="",
        test_class_name="FooTest",
        coverage_gate=0,
        quality_mode="ci_incremental",
        ci_diff_coverage_gate=95,
        ci_diff_mutation_gate=100,
        run_id="run-1",
        stage_introspect_abs="",
        mockito_api_guidance="",
        context_summary_abs="/tmp/context/context_summary.md",
        test_guidance_abs="/tmp/context/test_guidance.md",
        repo_summary_exists=False,
        compile_facts_exists=False,
    )
    rendered = stable + volatile

    assert "CI INCREMENTAL QUALITY MODE" in rendered
    assert "changed production lines need at least `95%` coverage and `100%` mutation score" in rendered
    assert "Do not chase unrelated class-wide coverage or mutation gaps" in rendered


def test_render_generate_test(fixtures_dir):
    parser = JavaParser()
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    mapper_path = os.path.join(fixtures_dir, "SampleMapper.java")

    results = [
        parser.parse_file(service_path),
        parser.parse_file(mapper_path)
    ]

    builder = GraphBuilder()
    graph = builder.build(results)
    extractor = ProcessExtractor(graph)
    flows = extractor.extract_flows(["com.example.service.SampleService.process"])

    with tempfile.TemporaryDirectory() as tmpdir:
        context_builder = ContextBuilder(repo_path=tmpdir, graph=graph, flows=flows)
        context_dir = context_builder.export_context_files()
        source_path = context_builder.get_class_source_path("com.example.service.SampleService")

        # Read template
        template_path = os.path.join("uta", "prompts", "generate_test.txt")
        with open(template_path, "r") as f:
            template = Template(f.read())

        from uta.engine.project_summary_artifacts import (
            merge_compile_fix_facts,
            sync_project_summaries,
            prompt_template_paths,
        )

        sync_project_summaries(tmpdir, graph, None)
        merge_compile_fix_facts(tmpdir, ["SampleFact returns Long"])
        pp = prompt_template_paths(tmpdir, context_dir)

        rendered = template.render(
            class_fqn="com.example.service.SampleService",
            source_path=source_path,
            context_dir=str(context_dir),
            target_context_abs=str(context_dir / "SampleService.context.md"),
            target_symbols_abs=str(context_dir / "SampleService.symbols.md"),
            wave_one_only=False,
            maven_instructions="\n\nAfter writing the test, verify with:\n  mvn test-compile\n  mvn test -Dtest=SampleServiceTest",
            maven_module_flag="",
            test_class_name="SampleServiceTest",
            coverage_gate=80,
            run_id="12345",
            index_query_command="/opt/uta/bin/uta-query-index --module biz",
            **pp,
        )

        # Prompt should reference files instead of inlining content
        assert "com.example.service.SampleService" in rendered
        assert "Run ID: 12345" in rendered
        assert "Target context:" in rendered
        assert "Symbol / import map:" in rendered
        assert pp["context_summary_abs"] in rendered
        assert pp["test_guidance_abs"] in rendered
        assert pp["compile_facts_abs"] in rendered
        assert "JUnit 4" in rendered
        assert "Mockito 2.x" in rendered
        assert "ArgumentMatchers.any()" in rendered
        assert "Construct the class under test in the style that best fits the repo and dependency types" in rendered
        assert "repo's existing test style first" in rendered
        assert source_path in rendered
        assert "implement the approved test plan faithfully" in rendered
        # Current prompt features
        assert "TARGET-SPECIFIC CACHE" in rendered
        assert "Preferred structured index query" in rendered
        assert "/opt/uta/bin/uta-query-index --module biz" in rendered
        assert "--class-fqn com.example.service.SampleService --json-output" in rendered
        assert "Write the first compile-safe `WAVE 1` harness and tests" in rendered
        assert "IMPLEMENT THE APPROVED PLAN" in rendered
        assert "Read and obey the approved plan" in rendered
        assert "Do not restart broad repo exploration during implementation" in rendered
        assert "Do targeted follow-up reads only when one exact symbol" in rendered
        assert "Do NOT use `task` or `todowrite` during generation." in rendered
        assert "Use the index query before broad `grep` / `glob` / multi-file `read` exploration." in rendered
        assert "manual construction, reflection injection, stubs/proxies, and Mockito for compatible interface/abstract collaborators" in rendered
        assert "test_generation_guidance.md" in rendered
        assert "Keep branch-driving values explicit" in rendered
        assert "prefer public-behavior assertions" in rendered
        assert "Compile immediately" in rendered
        assert "Run** the targeted test" in rendered
        assert "mvn test-compile" in rendered
        assert "mvn test -Dtest=SampleServiceTest" in rendered
        assert "SampleServiceTest" in rendered

        # Verify context files were actually created
        assert (context_dir / "class_map.md").exists()
        assert (context_dir / "dependency_map.md").exists()
        assert (context_dir / "process_flows.md").exists()
        assert (context_dir / "call_graph.md").exists()
        target_paths = context_builder.export_target_context_files("com.example.service.SampleService")
        assert Path(target_paths["context_abs"]).exists()
        assert Path(target_paths["symbols_abs"]).exists()

        # Verify class_map has the expected class
        class_map = (context_dir / "class_map.md").read_text()
        assert "com.example.service.SampleService" in class_map
        assert "`sampleMapper` : `SampleMapper`" in class_map


def test_export_context_files_content(fixtures_dir):
    """Verify that exported context files contain meaningful data."""
    parser = JavaParser()
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    mapper_path = os.path.join(fixtures_dir, "SampleMapper.java")

    results = [
        parser.parse_file(service_path),
        parser.parse_file(mapper_path)
    ]

    builder = GraphBuilder()
    graph = builder.build(results)
    extractor = ProcessExtractor(graph)
    flows = extractor.extract_flows(["com.example.service.SampleService.process"])

    with tempfile.TemporaryDirectory() as tmpdir:
        ctx = ContextBuilder(repo_path=tmpdir, graph=graph, flows=flows)
        context_dir = ctx.export_context_files()
        from uta.engine.project_summary_artifacts import prompt_template_paths, sync_project_summaries

        sync_project_summaries(tmpdir, graph, None)
        assert (context_dir / "project_summary.md").exists()
        assert (context_dir / "test_generation_guidance.md").exists()
        assert "mocked page-loader calls must terminate" in (context_dir / "test_generation_guidance.md").read_text()
        assert "compile_facts_abs" in prompt_template_paths(tmpdir, context_dir)

        # Class map should have fields and methods
        class_map = (context_dir / "class_map.md").read_text()
        assert "SampleService" in class_map
        assert "SampleMapper" in class_map
        assert "`sampleMapper` : `SampleMapper`" in class_map


def test_ci_context_path_renders_in_all_prompt_tails(tmp_path):
    ci_context = tmp_path / "ci_context.md"
    ci_context.write_text("# CI Context\nOnly unit-test repair context.", encoding="utf-8")
    common = {"ci_context_abs": str(ci_context), "stage_introspect_abs": ""}

    prompts = {
        "plan_tests": dict(
            batch=["com.example.Foo"],
            coverage_gate=80,
            strict_coverage_classes=[],
            target_context_files="- Foo.context.md",
            index_query_command="/bin/uta-query-index",
            roi_enabled=False,
        ),
        "generate_test": dict(
            class_fqn="com.example.Foo",
            source_path="/repo/Foo.java",
            context_dir="/ctx",
            target_context_abs="/ctx/Foo.context.md",
            target_symbols_abs="/ctx/Foo.symbols.md",
            index_query_command="/bin/uta-query-index",
            wave_one_only=False,
            maven_instructions="run mvn",
            maven_module_flag="",
            test_class_name="FooTest",
            coverage_gate=80,
            run_id="run-1",
            context_summary_abs="/ctx/summary.md",
            test_guidance_abs="/ctx/guidance.md",
            repo_summary_exists=False,
            compile_facts_exists=False,
            repo_summary_abs="",
            compile_facts_abs="",
        ),
        "fix_compile": dict(
            class_fqn="com.example.Foo",
            compile_errors="cannot find symbol",
            test_file_path="/repo/FooTest.java",
            maven_module_flag="",
            target_context_abs="/ctx/Foo.context.md",
            target_symbols_abs="/ctx/Foo.symbols.md",
        ),
        "fix_coverage": dict(
            class_fqn="com.example.Foo",
            current_coverage=40.0,
            coverage_gate=80,
            source_path="/repo/Foo.java",
            test_file_path="/repo/FooTest.java",
            test_class_name="FooTest",
            maven_module_flag="",
            target_context_abs="/ctx/Foo.context.md",
            target_symbols_abs="/ctx/Foo.symbols.md",
            uncovered_summary="line 1",
            roi_abs="",
        ),
        "fix_mutations": dict(
            class_fqn="com.example.Foo",
            current_coverage=80.0,
            current_mutation_score=50.0,
            mutation_gate=70,
            mutation_stats={},
            surviving_mutants=[],
            mutation_family_summary="survivor",
            mutation_family_summary_abs="/ctx/mutations.md",
            source_path="/repo/Foo.java",
            test_file_path="/repo/FooTest.java",
            target_context_abs="/ctx/Foo.context.md",
            target_symbols_abs="/ctx/Foo.symbols.md",
            mutation_roi_enabled=False,
            mutation_roi_skip_expensive=False,
        ),
    }

    for name, kwargs in prompts.items():
        _, tail = render_prompt_split(name, **kwargs, **common)
        assert str(ci_context) in tail
        assert "production-code edit requests are unsupported" in tail or "production-code edits" in tail


def test_ci_context_prompt_absent_when_not_provided():
    _, tail = render_prompt_split(
        "plan_tests",
        batch=["com.example.Foo"],
        coverage_gate=80,
        strict_coverage_classes=[],
        target_context_files="- Foo.context.md",
        index_query_command="/bin/uta-query-index",
        roi_enabled=False,
        stage_introspect_abs="",
    )

    assert "CI CONTEXT" not in tail


def test_ci_context_rejects_production_code_edit_request(tmp_path):
    ci_context = tmp_path / "ci_context.md"
    ci_context.write_text("Please modify src/main/java/com/example/Foo.java", encoding="utf-8")

    with pytest.raises(ValueError, match="production-code edits"):
        render_prompt_split(
            "plan_tests",
            batch=["com.example.Foo"],
            coverage_gate=80,
            strict_coverage_classes=[],
            target_context_files="- Foo.context.md",
            index_query_command="/bin/uta-query-index",
            roi_enabled=False,
            stage_introspect_abs="",
            ci_context_abs=str(ci_context),
        )
        assert "Imports:" in class_map

        # Dependency map should show SampleService depends on SampleMapper
        dep_map = (context_dir / "dependency_map.md").read_text()
        assert "SampleService" in dep_map or "SampleMapper" in dep_map

        # Process flows should be non-empty if flows were detected
        pf = (context_dir / "process_flows.md").read_text()
        assert "Process Flows" in pf

        target_paths = ctx.export_target_context_files("com.example.service.SampleService")
        target_context = Path(target_paths["context_abs"]).read_text()
        target_symbols = Path(target_paths["symbols_abs"]).read_text()
        assert "Expected test path" not in target_context
        assert "## Fields" in target_context
        assert "sampleMapper" in target_context
        assert "## Imported Symbols" in target_symbols
        assert "SampleMapper" in target_symbols


def test_render_fix_mutations_prefers_coverage_when_low():
    template_path = os.path.join("uta", "prompts", "fix_mutations.txt")
    with open(template_path, "r") as f:
        template = Template(f.read())

    rendered = template.render(
        class_fqn="com.example.service.SampleService",
        current_coverage=24.7,
        current_mutation_score=45.1,
        mutation_gate=70,
        surviving_mutants=[{"line": 42, "mutation_type": "NEGATE_CONDITIONALS", "detail": "changed conditional"}],
        mutation_family_summary="- `conditional` via `NEGATE_CONDITIONALS` — 1 survivor(s) on line(s): 42\n",
        mutation_family_summary_abs="/tmp/SampleService.mutation_families.md",
        source_path="/tmp/SampleService.java",
        test_file_path="/tmp/SampleServiceTest.java",
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
    )

    assert "fresh focused mutation-repair round" in rendered
    assert "Treat low mutation together with low coverage as a coverage problem first" in rendered
    assert "If line coverage is low, prioritize adding coverage-reach tests" in rendered
    assert "Prefer coverage expansion first when the class has low line coverage" in rendered
    assert "Mutation family summary" in rendered
    assert "Target context" in rendered
    assert "Target only the listed survivor families in this round" in rendered


def test_render_fix_coverage_targets_gate():
    template_path = os.path.join("uta", "prompts", "fix_coverage.txt")
    with open(template_path, "r") as f:
        template = Template(f.read())

    rendered = template.render(
        class_fqn="com.example.service.SampleService",
        current_coverage=12.5,
        coverage_gate=80,
        source_path="/tmp/SampleService.java",
        test_file_path="/tmp/SampleServiceTest.java",
        test_class_name="SampleServiceTest",
        maven_module_flag="",
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
        uncovered_summary="Methods with missed coverage:\n- `run`\n",
    )

    assert "below the gate of 80%" in rendered
    assert "coverage-reach problem" in rendered
    assert "Expand path reach before polishing assertions" in rendered
    assert "Target context" in rendered
    assert "SCRIPT-EXTRACTED UNCOVERED CLUSTERS" in rendered
    assert "mvn test -Dtest=SampleServiceTest" in rendered
    assert "/tmp/SampleService.java" in rendered
    assert "/tmp/SampleServiceTest.java" in rendered


def test_render_plan_tests_prompt():
    template_path = os.path.join("uta", "prompts", "plan_tests.txt")
    with open(template_path, "r") as f:
        template = Template(f.read())

        rendered = template.render(
            batch=["com.example.service.SampleService"],
            coverage_gate=80,
            strict_coverage_classes=[],
            target_context_files="- `com.example.service.SampleService`\n  - target context: `/tmp/SampleService.context.md`\n  - symbol map: `/tmp/SampleService.symbols.md`",
            index_query_command="/opt/uta/bin/uta-query-index --module biz",
        )

    assert "Do NOT modify files yet" in rendered
    assert "coverage gate of `80%`" in rendered
    assert "PUBLIC METHODS" in rendered
    assert "BRANCH AXES" in rendered
    assert "PLANNED TESTS" in rendered
    assert "COVERAGE RISKS" in rendered
    assert "broad exploration" in rendered
    assert "compile-critical facts" in rendered
    assert "Never ask for or return full source code" in rendered
    assert "keep the final plan to roughly 400-800 words" in rendered
    assert "TARGET CONTEXT FILES" in rendered
    assert "target context: `/tmp/SampleService.context.md`" in rendered
    assert "Default to the repo-local index query first" in rendered
    assert "REPO INDEX LOOKUP" in rendered
    assert "/opt/uta/bin/uta-query-index --module biz" in rendered
    assert "--module {{ module or \"\" }}" not in rendered
    assert "PLANNING EXPLORATION BUDGET" not in rendered
    assert "Do NOT search `.m2`, unpack JARs, decompile classes, or inspect compiled artifacts during planning." in rendered
    assert "except the repo-local index query command above" in rendered


def test_render_plan_tests_prompt_with_strict_coverage_classes():
    template_path = os.path.join("uta", "prompts", "plan_tests.txt")
    with open(template_path, "r") as f:
        template = Template(f.read())

        rendered = template.render(
            batch=["com.example.big.HugeService"],
            coverage_gate=80,
            strict_coverage_classes=[{
                "class_fqn": "com.example.big.HugeService",
            "line_count": 640,
            "public_method_count": 17,
            }],
            target_context_files="- `com.example.big.HugeService`\n  - target context: `/tmp/HugeService.context.md`\n  - symbol map: `/tmp/HugeService.symbols.md`",
            index_query_command="/opt/uta/bin/uta-query-index --module biz",
        )

    assert "STRICT COVERAGE CLASSES" in rendered
    assert "coverage feasibility" in rendered
    assert "METHODS REQUIRED FOR GATE" in rendered
    assert "ESTIMATED REACH" in rendered
    assert "BLOCKERS" in rendered
    assert "IMPLEMENTATION WAVES" in rendered
    assert "Do NOT count imports, anonymous template wrappers, empty `checkParams`, generated boilerplate, or repeated scaffolding as “free coverage”." in rendered or 'Do NOT count imports, anonymous template wrappers, empty `checkParams`, generated boilerplate, or repeated scaffolding as "free coverage".' in rendered
    assert "plan an executable first pass of roughly 25-35 tests" in rendered
    assert "WAVE 1" in rendered
    assert "WAVE 2" in rendered
    assert "do NOT say “do not chase class-wide completeness”" in rendered or "do NOT say \"do not chase class-wide completeness\"" in rendered


def test_render_fix_compile_renders_volatile_at_end():
    """fix_compile.txt is restructured with stable rules first and volatile per-call
    data after a `{# CACHE_BOUNDARY #}` marker (token_opt_phase2 strategy A).
    """
    template_path = os.path.join("uta", "prompts", "fix_compile.txt")
    with open(template_path, "r") as f:
        template = Template(f.read())

    rendered = template.render(
        class_fqn="com.example.service.SampleService",
        compile_errors="[ERROR] missing symbol Foo at line 12",
        test_file_path="biz/src/test/java/com/example/service/SampleServiceTest.java",
        maven_module_flag=" -pl biz",
        target_context_abs="/tmp/SampleService.context.md",
        target_symbols_abs="/tmp/SampleService.symbols.md",
    )

    assert "### INSTRUCTIONS" in rendered
    assert "### PER-CALL TARGET" in rendered
    assert "### COMPILATION ERRORS" in rendered
    assert "missing symbol Foo at line 12" in rendered
    assert "SampleServiceTest.java" in rendered
    assert "SampleService.context.md" in rendered
    assert "mvn test-compile -pl biz" in rendered
    # Stable instructions appear before per-call section.
    assert rendered.index("### INSTRUCTIONS") < rendered.index("### PER-CALL TARGET")


def test_prompt_loader_split_fix_compile():
    from uta.prompts import (
        CACHE_BOUNDARY_MARKER,
        load_prompt_split,
        render_prompt_split,
    )

    raw = (Path("uta") / "prompts" / "fix_compile.txt").read_text()
    assert CACHE_BOUNDARY_MARKER in raw, "fix_compile.txt must declare a cache boundary"

    stable_t, volatile_t = load_prompt_split("fix_compile")
    assert stable_t.render(maven_module_flag=" -pl biz")  # stable region renders standalone
    # Volatile region carries the per-call placeholders
    rendered_volatile = volatile_t.render(
        class_fqn="com.example.X",
        test_file_path="src/test/java/com/example/XTest.java",
        target_context_abs="/tmp/X.context.md",
        target_symbols_abs="/tmp/X.symbols.md",
        compile_errors="[ERROR] foo",
    )
    assert "PER-CALL TARGET" in rendered_volatile
    assert "[ERROR] foo" in rendered_volatile

    stable_text, volatile_text = render_prompt_split(
        "fix_compile",
        class_fqn="com.example.X",
        test_file_path="src/test/java/com/example/XTest.java",
        target_context_abs="/tmp/X.context.md",
        target_symbols_abs="/tmp/X.symbols.md",
        compile_errors="[ERROR] foo",
        maven_module_flag="",
    )
    # Joining the two halves yields the same content as a single full render
    # (the cache boundary marker is a Jinja comment that collapses to whitespace).
    from uta.prompts import render_prompt
    full = render_prompt(
        "fix_compile",
        class_fqn="com.example.X",
        test_file_path="src/test/java/com/example/XTest.java",
        target_context_abs="/tmp/X.context.md",
        target_symbols_abs="/tmp/X.symbols.md",
        compile_errors="[ERROR] foo",
        maven_module_flag="",
    )
    joined = (stable_text + volatile_text).replace("\n\n\n", "\n\n").strip()
    full_norm = full.replace("\n\n\n", "\n\n").strip()
    assert joined == full_norm


def test_prompt_loader_split_no_marker_treats_all_as_volatile():
    from jinja2 import Template
    from uta.prompts import load_prompt_split

    # plan_tests.txt currently has no boundary marker.
    raw = (Path("uta") / "prompts" / "plan_tests.txt").read_text()
    if "{# CACHE_BOUNDARY #}" in raw:
        return  # skip if a future commit adds one
    stable_t, volatile_t = load_prompt_split("plan_tests")
    assert isinstance(stable_t, Template)
    assert stable_t.render() == ""
    assert volatile_t.render(
        batch=["com.example.X"],
        coverage_gate=80,
        strict_coverage_classes=[],
        target_context_files="- `com.example.X`",
    ).strip().startswith("You")


def test_render_generate_prompt_discourages_actor_framework_mocks():
    template_path = os.path.join("uta", "prompts", "generate_test.txt")
    with open(template_path, "r") as f:
        template = Template(f.read())

    rendered = template.render(
        class_fqn="com.example.actor.SampleActor",
        source_path="biz/src/main/java/com/example/actor/SampleActor.java",
        context_dir=".uta_cache/context",
        target_context_abs=".uta_cache/context/SampleActor.context.md",
        target_symbols_abs=".uta_cache/context/SampleActor.symbols.md",
        index_query_command="/opt/uta/bin/uta-query-index --module biz",
        wave_one_only=False,
        maven_instructions="",
        maven_module_flag="",
        test_class_name="SampleActorTest",
        coverage_gate=80,
        run_id="12345",
        repo_summary_exists=False,
        context_summary_abs="/tmp/project_summary.md",
        test_guidance_abs="/tmp/test_generation_guidance.md",
        compile_facts_exists=False,
    )

    assert "Do not mock concrete DTOs, entities, enums, or value objects." in rendered
    assert "do not stub the page loader with a constant non-empty list" in rendered
    assert "Prefer manual harness + field injection for heavy legacy classes." in rendered
    assert "Do NOT use `task` or `todowrite` during generation." in rendered
    assert "SOURCE LOOKUP ORDER FOR EXTERNAL TYPES" in rendered
    assert "sibling API/source repos listed in `/tmp/test_generation_guidance.md`" in rendered
    assert "Do NOT unpack jars, inspect `.class` files, run `javap`, or run Java decompilers during generation." in rendered
