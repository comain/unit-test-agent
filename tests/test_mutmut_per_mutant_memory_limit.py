"""A runaway mutant must not exhaust the whole enforcement process group."""

import signal
from datetime import datetime
from types import SimpleNamespace

from uta_py_enforce import mutmut_adapter_runtime as adapter


def test_mutant_memory_limit_defaults_below_enforcement_group_limit():
    assert adapter.mutant_memory_limit_bytes({}) == 1536 * 1024 * 1024
    assert adapter.mutant_memory_limit_bytes({adapter.PER_MUTANT_MEMORY_LIMIT_MB_ENV: "512"}) == 512 * 1024 * 1024


def test_only_over_limit_mutant_is_terminated(monkeypatch):
    signals = []
    monkeypatch.setattr(adapter, "mutant_process_rss_bytes", lambda pid: {11: 100, 12: 201}.get(pid))
    monkeypatch.setattr(adapter.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert not adapter.terminate_runaway_mutant(11, "safe", 200)
    assert adapter.terminate_runaway_mutant(12, "runaway", 200)
    assert not adapter.terminate_runaway_mutant(13, "gone", 200)
    assert signals == [(12, signal.SIGTERM)]


def test_disappeared_mutant_is_not_reported_as_terminated(monkeypatch):
    monkeypatch.setattr(adapter, "mutant_process_rss_bytes", lambda pid: 201)
    monkeypatch.setattr(adapter.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    assert not adapter.terminate_runaway_mutant(12, "gone", 200)


def test_timeout_checker_applies_memory_limit_to_running_mutant(monkeypatch):
    import mutmut.__main__ as mutmut_main

    calls = []
    sleeps = iter((None, StopIteration()))

    def sleep(_seconds):
        result = next(sleeps)
        if isinstance(result, Exception):
            raise result

    with monkeypatch.context() as patch:
        patch.setattr(mutmut_main, "timeout_checker", lambda mutants: None)
        patch.setattr(adapter.time, "sleep", sleep)
        patch.setattr(adapter, "mutant_memory_limit_bytes", lambda: 200)
        patch.setattr(adapter, "terminate_runaway_mutant", lambda pid, name, limit: calls.append((pid, name, limit)) or True)
        adapter._install_mutant_timeout_floor()
        mutant = SimpleNamespace(start_time_by_pid={12: datetime.now()}, estimated_time_of_tests_by_mutant={})
        checker = mutmut_main.timeout_checker([(mutant, "runaway", None)])
        try:
            checker()
        except StopIteration:
            pass

    assert calls == [(12, "runaway", 200)]
