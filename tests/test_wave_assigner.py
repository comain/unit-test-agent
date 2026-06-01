"""Tests for uta.engine.wave_assigner (K strategy — deterministic wave assignment)."""

import pytest
from uta.engine.wave_assigner import assign_waves, assign_waves_from_context, format_wave_table, MethodWave


CONTEXT_WITH_METHODS = """\
# Target Test Context

## Class
- FQN: `com.example.OrderService`

## Public Methods
- `createOrder(Long userId, List<Item> items)`
- `cancelOrder(Long orderId)`
- `getOrderById(Long id)`
- `processPayment(Order order)`
- `getUserName(Long userId)`
- `setStatus(String status)`
- `isActive()`
"""

CONTEXT_NO_METHODS = """\
# Target Test Context

## Class
- FQN: `com.example.Foo`
"""

PYTHON_CONTEXT_WITH_SYMBOLS = """\
# Python Target Context

## Symbols
- `forecast_for_store` (function) line 8
- `ConfigLoader.load_config` (method) line 24
- `ConfigLoader.get_status` (method) line 31
- `InventorySnapshot` (class) line 41
"""


def test_wave1_for_business_verb_methods():
    waves = assign_waves_from_context(CONTEXT_WITH_METHODS)
    names_wave1 = {w.name for w in waves if w.wave == 1}
    assert "createOrder" in names_wave1
    assert "processPayment" in names_wave1


def test_wave2_for_accessors():
    waves = assign_waves_from_context(CONTEXT_WITH_METHODS)
    names_wave2 = {w.name for w in waves if w.wave == 2}
    assert "getUserName" in names_wave2
    assert "setStatus" in names_wave2
    assert "isActive" in names_wave2


def test_wave1_sorted_before_wave2():
    waves = assign_waves_from_context(CONTEXT_WITH_METHODS)
    if len(waves) >= 2:
        first_wave2_idx = next((i for i, w in enumerate(waves) if w.wave == 2), len(waves))
        last_wave1_idx = max((i for i, w in enumerate(waves) if w.wave == 1), default=-1)
        assert last_wave1_idx < first_wave2_idx


def test_empty_context_returns_empty_list():
    waves = assign_waves_from_context(CONTEXT_NO_METHODS)
    assert waves == []


def test_no_duplicate_methods():
    waves = assign_waves_from_context(CONTEXT_WITH_METHODS)
    names = [w.name for w in waves]
    assert len(names) == len(set(names))


def test_format_wave_table_contains_headers():
    waves = [MethodWave(name="createOrder", wave=1, reason="business-verb:create")]
    table = format_wave_table(waves)
    assert "Method" in table
    assert "Wave" in table
    assert "createOrder" in table


def test_format_wave_table_empty():
    result = format_wave_table([])
    assert "no public callables" in result.lower()


def test_reason_field_populated():
    waves = assign_waves_from_context(CONTEXT_WITH_METHODS)
    for w in waves:
        assert w.reason, f"Empty reason for method {w.name}"


def test_python_symbols_are_supported():
    waves = assign_waves_from_context(PYTHON_CONTEXT_WITH_SYMBOLS)
    by_name = {w.name: w for w in waves}
    assert by_name["forecast_for_store"].wave == 2
    assert by_name["load_config"].wave == 1
    assert by_name["get_status"].wave == 2
    assert "InventorySnapshot" not in by_name


def test_assign_waves_accepts_normalized_callable_names():
    waves = assign_waves(["service.calculate_total", "getStatus", "pkg.module::process_order"])
    by_name = {w.name: w for w in waves}
    assert by_name["calculate_total"].wave == 1
    assert by_name["process_order"].wave == 1
    assert by_name["getStatus"].wave == 2
