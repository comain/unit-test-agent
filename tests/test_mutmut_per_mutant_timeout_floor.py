"""python-enforce and repair must share one per-mutant timeout floor.

Mutmut 3.5 SIGXCPUs a mutant after ``(estimated_test_time + 1) * 15`` seconds.
A 93-test pathfinding file estimates ~0.2s unmutated, so repair recorded every
mutant as timeout in ~20s (report 38badf25, 0.00%). python-enforce uses the
same adapter but pytest-timeout is 120s, so hanging tests fail and kill the
mutant (77.78%). Flooring mutmut's clock to that pytest-timeout keeps both
lanes on one threshold.
"""

from __future__ import annotations

from uta_py_enforce.mutmut_adapter_runtime import (
    PER_MUTANT_TIMEOUT_ENV,
    mutant_cpu_timeout_seconds,
    mutant_wall_timeout_seconds,
    per_mutant_timeout_seconds,
)
from uta_py_enforce.optional_plugins import DEFAULT_TEST_TIMEOUT_SECONDS, TIMEOUT_ENV


def test_the_floor_defaults_to_the_pytest_timeout():
    assert per_mutant_timeout_seconds({}) == DEFAULT_TEST_TIMEOUT_SECONDS
    assert DEFAULT_TEST_TIMEOUT_SECONDS == 120


def test_pytest_timeout_is_the_shared_threshold():
    assert per_mutant_timeout_seconds({TIMEOUT_ENV: "90"}) == 90


def test_an_explicit_per_mutant_override_wins():
    env = {PER_MUTANT_TIMEOUT_ENV: "200", TIMEOUT_ENV: "90"}
    assert per_mutant_timeout_seconds(env) == 200


def test_a_short_estimate_is_floored_to_the_shared_threshold():
    assert mutant_wall_timeout_seconds(0.2, {TIMEOUT_ENV: "120"}) == 120.0
    assert mutant_cpu_timeout_seconds(0.2, {TIMEOUT_ENV: "120"}) == 120.0


def test_a_slow_estimate_keeps_mutmuts_wider_native_budget():
    env = {TIMEOUT_ENV: "120"}
    assert mutant_wall_timeout_seconds(20.0, env) == (20.0 + 1.0) * 15.0
    assert mutant_cpu_timeout_seconds(20.0, env) == (20.0 + 1.0) * 30.0


def test_python_enforce_exports_the_shared_threshold(tmp_path, monkeypatch):
    from uta_py_enforce.pytest_env import pytest_env

    monkeypatch.delenv(PER_MUTANT_TIMEOUT_ENV, raising=False)
    env = pytest_env(tmp_path, [])
    assert env.get("PYTEST_TIMEOUT") == "120"
    assert env.get(PER_MUTANT_TIMEOUT_ENV) == "120"


def test_timeout_floor_install_is_idempotent():
    from uta_py_enforce.mutmut_adapter_runtime import _install_mutant_timeout_floor
    import mutmut.__main__ as mutmut_main

    _install_mutant_timeout_floor()
    first = mutmut_main.timeout_checker
    _install_mutant_timeout_floor()
    assert mutmut_main.timeout_checker is first
    assert getattr(first, "_uta_timeout_floor", False)
