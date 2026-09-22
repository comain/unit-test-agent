import json

from uta_enforce_core.mutation_suppression import (
    ASYNC_SCHEDULER_LOOP,
    GENERATED_FRAMEWORK_GLUE,
    IMPORT_WIRING,
    LOW_VALUE_LOGGING,
    METRICS_ONLY,
    MUTATION_TOOL_UNSUPPORTED,
    PURE_CONFIG_CONSTANT,
)
from uta.language.python.mutation_candidates import (
    build_mutmut3_candidate_plan_from_meta,
    collect_python_mutation_opportunities,
    low_value_side_effect_lines,
    select_one_opportunity_per_line,
)


def test_collect_python_mutation_opportunities_suppresses_low_value_lines(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import logging\n"
        "THRESHOLD = 3\n"
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "def run(raw):\n"
        "    if raw > THRESHOLD:\n"
        "        logger.warning('large %s', raw)\n"
        "        metrics.increment('large')\n"
        "        return raw + 1\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={1, 2, 6, 7, 8, 9},
        covered_lines={6, 7, 8, 9},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[1] == IMPORT_WIRING
    assert reasons[2] == PURE_CONFIG_CONSTANT
    assert reasons[7] == LOW_VALUE_LOGGING
    assert reasons[8] == METRICS_ONLY

    selected_lines = [item.line for item in plan.selected]
    assert selected_lines == [6, 9]
    assert all(item.source_path == "pkg/worker.py" for item in plan.selected)
    assert all(item.opportunity_id for item in plan.selected)
    assert all(item.selection_rank for item in plan.selected)


def test_collect_python_mutation_opportunities_suppresses_framework_glue(tmp_path):
    source = tmp_path / "pkg" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "app.include_router(user_router)\n"
        "urlpatterns = [path('x/', view)]\n"
        "\n"
        "def handle(raw):\n"
        "    return raw + 1\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/api.py",
        changed_lines={1, 2, 3, 4, 7},
        covered_lines={7},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[1] == IMPORT_WIRING
    assert reasons[2] == GENERATED_FRAMEWORK_GLUE
    assert reasons[3] == GENERATED_FRAMEWORK_GLUE
    assert reasons[4] == GENERATED_FRAMEWORK_GLUE
    assert [item.line for item in plan.selected] == [7]


def test_collect_python_mutation_opportunities_uses_versioned_priority_bands(tmp_path):
    source = tmp_path / "pkg" / "policy.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def route(flag, items, raw):\n"
        "    if flag and raw > 3:\n"
        "        value = raw + 1\n"
        "        payload = {'items': [items]}\n"
        "        sink(value)\n"
        "        return value\n"
        "    del payload\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/policy.py",
        changed_lines={2, 3, 4, 5, 6, 7},
        covered_lines={2, 3, 4, 5, 6, 7},
    )

    by_operator = {item.operator_name: item.operator_priority for item in plan.eligible}
    assert by_operator["comparison_boundary"] == 100
    assert by_operator["boolean_operator"] == 100
    assert by_operator["math_operator"] == 90
    assert by_operator["call_argument"] == 90
    assert by_operator["constant_value"] == 80
    assert by_operator["statement"] == 20
    assert "container_value" not in by_operator
    assert "return_value" not in by_operator


def test_collect_python_mutation_opportunities_suppresses_low_yield_truthiness_guards(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def choose(raw):\n"
        "    if not raw:\n"
        "        return 0\n"
        "    if raw:\n"
        "        return 1\n"
        "    if raw > 3:\n"
        "        return 2\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={2, 4, 6},
        covered_lines={2, 4, 6},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[2] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[4] == MUTATION_TOOL_UNSUPPORTED
    selected_by_line = {item.line: item.operator_name for item in plan.selected}
    assert selected_by_line == {6: "comparison_boundary"}


def test_collect_python_mutation_opportunities_suppresses_perpetual_async_scheduler_sleep(tmp_path):
    source = tmp_path / "pkg" / "sessions.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import asyncio\n"
        "\n"
        "class TextSessionManager:\n"
        "    async def _gc_loop(self):\n"
        "        while True:\n"
        "            await asyncio.sleep(60)\n"
        "            if stale_session():\n"
        "                await self.remove('sid')\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/sessions.py",
        changed_lines={6, 7, 8},
        covered_lines={6, 7, 8},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[6] == ASYNC_SCHEDULER_LOOP
    selected_by_line = {item.line: item.operator_name for item in plan.selected}
    assert selected_by_line == {7: "boolean_operator", 8: "call_argument"}


def test_collect_python_mutation_opportunities_limits_statement_fallback_to_mutmut_statement_nodes(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Worker:\n"
        "    def run(self, items):\n"
        "        for item in items:\n"
        "            continue\n"
        "        value = items\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={1, 2, 3, 4, 5},
        covered_lines={1, 2, 3, 4, 5},
    )

    selected_by_line = {item.line: item.operator_name for item in plan.selected}
    assert selected_by_line == {4: "statement", 5: "statement"}


def test_collect_python_mutation_opportunities_skips_unsupported_return_value_for_mutmut3(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def choose(raw):\n"
        "    if raw > 0:\n"
        "        return raw\n"
        "    return 0\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={3, 4},
        covered_lines={3, 4},
    )

    assert "return_value" not in {item.operator_name for item in plan.eligible}
    assert "return_value" not in {item.operator_name for item in plan.selected}


def test_collect_python_mutation_opportunities_skips_type_hint_union_as_math(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from pathlib import Path\n"
        "\n"
        "last_path: Path | None = None\n"
        "\n"
        "def run(value: int | None) -> int | None:\n"
        "    return value\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={3, 5},
        covered_lines={3, 5},
    )

    assert "math_operator" not in {item.operator_name for item in plan.eligible}
    assert "math_operator" not in {item.operator_name for item in plan.selected}


def test_collect_python_mutation_opportunities_skips_annotation_and_docstring_constants(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Request:\n"
        "    \"\"\"request model\"\"\"\n"
        "    mode: str = \"standard\"\n"
        "\n"
        "    def run(self) -> \"Request\":\n"
        "        return self\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={2, 5},
        covered_lines={2, 5},
    )

    assert "constant_value" not in {item.operator_name for item in plan.eligible}
    assert "constant_value" not in {item.operator_name for item in plan.selected}


def test_collect_python_mutation_opportunities_skips_fstring_constants_unsupported_by_mutmut3(tmp_path):
    source = tmp_path / "pkg" / "models.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Model:\n"
        "    def label(self):\n"
        "        return f'{self.name} ({self.env})'\n"
        "\n"
        "    def fixed_label(self):\n"
        "        return 'fixed'\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/models.py",
        changed_lines={3, 6},
        covered_lines={3, 6},
    )

    selected_by_line = {item.line: item.operator_name for item in plan.selected}
    assert 3 not in selected_by_line
    assert selected_by_line[6] == "constant_value"


def test_collect_python_mutation_opportunities_suppresses_function_default_constants(tmp_path):
    source = tmp_path / "pkg" / "client.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "async def search_orders(\n"
        "    *,\n"
        "    third_order_id: str = \"\",\n"
        "    limit: int = 5,\n"
        "):\n"
        "    return limit + 1\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/client.py",
        changed_lines={3, 4, 6},
        covered_lines={3, 4, 6},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[3] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[4] == MUTATION_TOOL_UNSUPPORTED
    assert 3 not in {item.line for item in plan.eligible}
    assert 4 not in {item.line for item in plan.eligible}
    assert [item.line for item in plan.selected] == [6]


def test_collect_python_mutation_opportunities_skips_decorator_and_raise_calls(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def marker(name):\n"
        "    return lambda fn: fn\n"
        "\n"
        "@marker(\"route\")\n"
        "def run(raw):\n"
        "    raise ValueError(\"bad\")\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={4, 6},
        covered_lines={4, 6},
    )

    assert "call_argument" not in {item.operator_name for item in plan.eligible}
    assert "call_argument" not in {item.operator_name for item in plan.selected}
    assert "statement" not in {item.operator_name for item in plan.eligible}
    assert "statement" not in {item.operator_name for item in plan.selected}


def test_collect_python_mutation_opportunities_suppresses_decorated_function_body(tmp_path):
    source = tmp_path / "pkg" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def route(path):\n"
        "    return lambda fn: fn\n"
        "\n"
        "@route('/items')\n"
        "def list_items(flag, raw):\n"
        "    if flag and raw > 3:\n"
        "        return raw + 1\n"
        "    return raw\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/api.py",
        changed_lines={6, 7, 8},
        covered_lines={6, 7, 8},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[6] == GENERATED_FRAMEWORK_GLUE
    assert reasons[7] == GENERATED_FRAMEWORK_GLUE
    assert reasons[8] == GENERATED_FRAMEWORK_GLUE
    assert not plan.selected


def test_collect_python_mutation_opportunities_supports_ordinary_methods_in_decorated_class(tmp_path):
    source = tmp_path / "pkg" / "views.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def access(cls):\n"
        "    return cls\n"
        "\n"
        "@access\n"
        "class DetailView:\n"
        "    def update(self, value):\n"
        "        return value + 1\n"
        "\n"
        "    @staticmethod\n"
        "    def promote(value):\n"
        "        return value > 0\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/views.py",
        changed_lines={7, 11},
        covered_lines={7, 11},
    )

    assert {item.line for item in plan.selected} == {7}
    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[11] == GENERATED_FRAMEWORK_GLUE


def test_collect_python_mutation_opportunities_suppresses_mutmut_unstable_constructs(tmp_path):
    source = tmp_path / "pkg" / "parser.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import logging\n"
        "_logger = logging.getLogger(__name__)\n"
        "\n"
        "def parse(raw, extra: dict | None = None):\n"
        "    if not raw:\n"
        "        return None\n"
        "    def add_item(node):\n"
        "        cid = node.get('id')\n"
        "        if cid:\n"
        "            return set()\n"
        "        return cid\n"
        "    return add_item(raw)\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/parser.py",
        changed_lines={2, 4, 5, 6, 7, 8, 9, 10, 11, 12},
        covered_lines={2, 4, 5, 6, 7, 8, 9, 10, 11, 12},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[2] == LOW_VALUE_LOGGING
    assert reasons[4] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[5] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[6] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[7] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[8] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[9] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[10] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[11] == MUTATION_TOOL_UNSUPPORTED
    assert {item.line for item in plan.selected} == {12}


def test_collect_python_mutation_opportunities_suppresses_module_config_and_class_fields(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "_init_store()\n"
        "ALLOWED = frozenset({'a', 'b'})\n"
        "_state: dict[str, int] = {}\n"
        "__all__ = [\n"
        "    'run',\n"
        "]\n"
        "\n"
        "class Request:\n"
        "    tags: list[str] = []\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={1, 2, 3, 4, 5, 6, 9},
        covered_lines={1, 2, 3, 4, 5, 6, 9},
    )

    suppressed_lines = {item.opportunity.line for item in plan.suppressed}
    assert {1, 2, 3, 4, 5, 6}.issubset(suppressed_lines)
    assert not plan.selected


def test_collect_python_mutation_opportunities_suppresses_mutmut_unsupported_class_body_calls(tmp_path):
    source = tmp_path / "pkg" / "models.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class Ticket:\n"
        "    id = models.AutoField(primary_key=True)\n"
        "    status = models.CharField(max_length=16, default='closed')\n"
        "\n"
        "    def label(self):\n"
        "        return f'{self.id}:{self.status}'\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/models.py",
        changed_lines={2, 3, 6},
        covered_lines={2, 3, 6},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[2] == MUTATION_TOOL_UNSUPPORTED
    assert reasons[3] == MUTATION_TOOL_UNSUPPORTED
    assert not plan.selected


def test_collect_python_mutation_opportunities_suppresses_module_level_calls_not_materialized_by_mutmut3(tmp_path):
    source = tmp_path / "pkg" / "bootstrap.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "registry.register('ticket', Ticket)\n"
        "\n"
        "def handle(raw):\n"
        "    return normalize(raw)\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/bootstrap.py",
        changed_lines={1, 4},
        covered_lines={1, 4},
    )

    reasons = {item.opportunity.line: item.reason_code for item in plan.suppressed}
    assert reasons[1] == MUTATION_TOOL_UNSUPPORTED
    assert {item.line for item in plan.selected} == {4}


def test_collect_python_mutation_opportunities_skips_no_argument_calls(tmp_path):
    source = tmp_path / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def run(state):\n"
        "    refresh()\n"
        "    state.clear()\n"
        "    send(state)\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/worker.py",
        changed_lines={2, 3, 4},
        covered_lines={2, 3, 4},
    )

    selected_by_line = {item.line: item.operator_name for item in plan.selected}
    assert selected_by_line == {4: "call_argument"}


def test_select_one_opportunity_per_line_is_deterministic(tmp_path):
    source = tmp_path / "pkg" / "calc.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def calc(a, b):\n"
        "    return a + b if a > b else b - a\n",
        encoding="utf-8",
    )

    plan = collect_python_mutation_opportunities(
        source,
        source_path="pkg/calc.py",
        changed_lines={2},
        covered_lines={2},
    )
    selected, omitted = select_one_opportunity_per_line(reversed(plan.eligible))

    assert len(selected) == 1
    assert len(omitted) >= 1
    assert selected[0] == plan.selected[0]
    assert [item.opportunity_id for item in omitted] == sorted(item.opportunity_id for item in omitted)


def test_low_value_side_effect_lines_returns_engine_suppression_reasons(tmp_path):
    source = tmp_path / "app.py"
    source.write_text(
        "def f(logger, metrics):\n"
        "    logger.info('x')\n"
        "    metrics.increment('x')\n"
        "    return 1\n",
        encoding="utf-8",
    )

    reasons = low_value_side_effect_lines(source)

    assert reasons == {2: LOW_VALUE_LOGGING, 3: METRICS_ONLY}


def test_build_mutmut3_candidate_plan_from_meta_records_exact_keys(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "worker.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def run(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    meta = repo / "mutants" / "pkg" / "worker.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "pkg.worker.x_run__mutmut_1": 1,
                    "pkg.worker.x_run__mutmut_2": 0,
                    "pkg.worker.x_run__mutmut_3": 0,
                },
                "line_by_key": {
                    "pkg.worker.x_run__mutmut_1": 2,
                    "pkg.worker.x_run__mutmut_2": 2,
                    "pkg.worker.x_run__mutmut_3": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    plan = build_mutmut3_candidate_plan_from_meta(
        repo,
        source_path="pkg/worker.py",
        target_id="pyfile:pkg/worker.py",
        changed_lines={"pkg/worker.py": {2}},
        covered_lines={2},
        selected_test_paths=("tests/test_worker.py",),
        mutmut_version="mutmut, version 3.3.1",
        runtime_fingerprint="python3.11",
        dependency_fingerprint="deps-a",
        config_fingerprint_value="cfg-a",
        mutmut_internal_api_fingerprint="mutmut3-meta-v1",
    )

    payload = plan.as_dict()
    assert payload["filterMechanism"] == "mutmut3_metadata_selected_execution"
    assert payload["exactToolCandidateKeys"] == ["pkg.worker.x_run__mutmut_1"]
    assert len(payload["reportFullSelected"]) == 1
    assert all(
        item["opportunity"]["line"] == 2
        for item in payload["reportFullSelected"]
    )


def test_build_mutmut3_candidate_plan_maps_src_layout_keys(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "src" / "log_config.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def build_uvicorn_log_config(raw):\n"
        "    return raw.upper()\n",
        encoding="utf-8",
    )
    meta = repo / "mutants" / "src" / "log_config.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "log_config.x_build_uvicorn_log_config__mutmut_1": 1,
                },
                "line_by_key": {
                    "log_config.x_build_uvicorn_log_config__mutmut_1": 2,
                },
            }
        ),
        encoding="utf-8",
    )

    plan = build_mutmut3_candidate_plan_from_meta(
        repo,
        source_path="src/log_config.py",
        target_id="pyfile:src/log_config.py",
        changed_lines={"src/log_config.py": {2}},
        covered_lines={2},
        selected_test_paths=("tests/test_log_config.py",),
        mutmut_version="mutmut, version 3.5.0",
        runtime_fingerprint="python3.12",
        dependency_fingerprint="deps-a",
        config_fingerprint_value="cfg-a",
        mutmut_internal_api_fingerprint="mutmut3-meta-v1",
    ).as_dict()

    assert plan["exactToolCandidateKeys"] == [
        "log_config.x_build_uvicorn_log_config__mutmut_1"
    ]


def test_build_mutmut3_candidate_plan_maps_class_method_exact_keys(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "pkg" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class StoreForecast:\n"
        "    def predict(self, raw):\n"
        "        return raw + 1\n",
        encoding="utf-8",
    )
    meta = repo / "mutants" / "pkg" / "forecast.py.meta"
    meta.parent.mkdir(parents=True)
    meta.write_text(
        json.dumps(
            {
                "exit_code_by_key": {
                    "pkg.forecast.xǁStoreForecastǁpredict__mutmut_1": 1,
                },
                "line_by_key": {
                    "pkg.forecast.xǁStoreForecastǁpredict__mutmut_1": 3,
                },
            }
        ),
        encoding="utf-8",
    )

    plan = build_mutmut3_candidate_plan_from_meta(
        repo,
        source_path="pkg/forecast.py",
        target_id="pyfile:pkg/forecast.py",
        changed_lines={"pkg/forecast.py": {3}},
        covered_lines={3},
        selected_test_paths=("tests/test_forecast.py",),
        mutmut_version="mutmut, version 3.3.1",
        runtime_fingerprint="python3.11",
        dependency_fingerprint="deps-a",
        config_fingerprint_value="cfg-a",
        mutmut_internal_api_fingerprint="mutmut3-meta-v1",
    ).as_dict()

    assert plan["exactToolCandidateKeys"] == [
        "pkg.forecast.xǁStoreForecastǁpredict__mutmut_1"
    ]
