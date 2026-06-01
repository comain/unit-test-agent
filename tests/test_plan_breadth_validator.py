"""Tests for engine validation breadth gate."""

import pytest
from uta.engine.validation import validate_plan_breadth, BreadthVerdict


CONTEXT_WITH_METHODS = """\
# Target Test Context

## Class
- FQN: `com.example.OrderService`

## Public Methods
- `createOrder(Long userId)`
- `cancelOrder(Long orderId)`
- `getOrder(Long orderId)`
- `validateOrder(Order order)`
- `processPayment(Order order)`
"""

CONTEXT_NO_METHODS = """\
# Target Test Context

## Class
- FQN: `com.example.OrderService`
"""


def _plan_covering(*method_names):
    return "\n".join(
        f"- Test `{name}(` to verify behavior" for name in method_names
    )


def test_pass_when_plan_covers_all_methods():
    plan = _plan_covering("createOrder", "cancelOrder", "getOrder", "validateOrder", "processPayment")
    result = validate_plan_breadth(plan, CONTEXT_WITH_METHODS)
    assert result.verdict == BreadthVerdict.PASS


def test_pass_at_min_coverage_ratio():
    # 3/5 = 60% which equals the default min of 0.6
    plan = _plan_covering("createOrder", "cancelOrder", "getOrder")
    result = validate_plan_breadth(plan, CONTEXT_WITH_METHODS, min_coverage_ratio=0.6)
    assert result.verdict == BreadthVerdict.PASS
    assert result.coverage_ratio >= 0.6


def test_under_when_plan_too_thin():
    plan = _plan_covering("createOrder")  # only 1/5 = 20%
    result = validate_plan_breadth(plan, CONTEXT_WITH_METHODS, min_coverage_ratio=0.6)
    assert result.verdict == BreadthVerdict.UNDER
    assert "cancelOrder" in result.missing_methods or len(result.missing_methods) >= 1


def test_over_when_plan_has_too_many_methods():
    # invent many extra method references
    extras = [f"fakeMethod{i}" for i in range(20)]
    plan = _plan_covering("createOrder", *extras)
    result = validate_plan_breadth(plan, CONTEXT_WITH_METHODS, max_over_ratio=1.5)
    assert result.verdict == BreadthVerdict.OVER


def test_pass_when_no_public_methods_in_context():
    plan = "No methods mentioned"
    result = validate_plan_breadth(plan, CONTEXT_NO_METHODS)
    assert result.verdict == BreadthVerdict.PASS
    assert result.known_methods == 0


def test_result_contains_known_method_count():
    plan = _plan_covering("createOrder", "cancelOrder", "getOrder")
    result = validate_plan_breadth(plan, CONTEXT_WITH_METHODS)
    assert result.known_methods == 5


def test_result_lists_missing_methods():
    plan = _plan_covering("createOrder")
    result = validate_plan_breadth(plan, CONTEXT_WITH_METHODS, min_coverage_ratio=0.6)
    assert result.verdict == BreadthVerdict.UNDER
    for name in ("cancelOrder", "getOrder", "validateOrder", "processPayment"):
        assert name in result.missing_methods


def test_message_is_non_empty():
    plan = _plan_covering("createOrder")
    result = validate_plan_breadth(plan, CONTEXT_WITH_METHODS)
    assert result.message


def test_public_method_extraction_handles_return_types_and_void_signatures():
    context = """\
# Target Test Context

## Public Methods
- `void addPickingFlow(Long id)`
- `AreaInfoResp areaAoBuilder(Entity entity)`
- `List<String> batchFinished(List<Request> requests)`
"""
    plan = _plan_covering("addPickingFlow", "areaAoBuilder")
    result = validate_plan_breadth(plan, context, min_coverage_ratio=0.5)
    assert result.known_methods == 3
    assert result.verdict == BreadthVerdict.PASS


def test_public_method_extraction_counts_primitive_return_types():
    context = """\
# Target Test Context

## Public Methods
- `boolean existOccupy(String logisticsCode, String skuCode, List<String> occupyIds)`
- `int countAllTask(Long pickingId)`
- `void receivePicking(String documentId, String operator, Date receiveTime, String operateSource)`
"""
    plan = _plan_covering("existOccupy", "countAllTask")
    result = validate_plan_breadth(plan, context, min_coverage_ratio=0.6)
    assert result.known_methods == 3
    assert result.verdict == BreadthVerdict.PASS


def test_plan_method_extraction_ignores_prose_words():
    context = """\
# Target Test Context

## Public Methods
- `boolean existOccupy(String logisticsCode, String skuCode, List<String> occupyIds)`
- `int countAllTask(Long pickingId)`
- `void receivePicking(String documentId, String operator, Date receiveTime, String operateSource)`
"""
    plan = """\
1. `PUBLIC METHODS`
- Highest-yield paths for first pass:
  - `existOccupy`, `countAllTask`
2. `COVERAGE RISKS`
- broad branch coverage across workflow zones without deep loops
- keep assertions stable and avoid brittle mechanics
"""
    result = validate_plan_breadth(plan, context, min_coverage_ratio=0.6, max_over_ratio=1.5)
    assert result.planned_methods == 2
    assert result.verdict == BreadthVerdict.PASS
    assert "coverage" not in result.extra_methods
    assert "branch" not in result.extra_methods


def test_python_payload_context_uses_normalized_callables():
    context = {
        "language": "python",
        "symbols": [
            {"kind": "function", "name": "forecast", "qualified_name": "forecast", "line": 1, "end_line": 3},
            {"kind": "method", "name": "_helper", "qualified_name": "Model._helper", "line": 5, "end_line": 6},
        ],
    }

    result = validate_plan_breadth("Test `forecast` for normal demand.", context, min_coverage_ratio=1.0)

    assert result.verdict == BreadthVerdict.PASS
    assert result.known_methods == 1
