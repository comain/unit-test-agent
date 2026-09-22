"""Hangs are execution failures, not test-generation opportunities."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_maven_metadata import DIFF_MUTATION_OK, with_resolved_enforcer

from uta.enforcement.enforcement import QualityGateStatus
from uta.language.java.enforcement_runner import MavenEnforcementRunner
from uta.language.java.enforcement_runner.hanging import HangingRun
from uta.language.java.enforcement_runner.parsing import unfinished_surefire_class
from uta.language.java.ci import _pit_baseline_failure_target_classes
from uta.shared.fix_sessions import can_create_fix_session
from uta.tasks.hanging_test_quarantine import SQLiteHangingTestQuarantine


@pytest.mark.parametrize(('output', 'expected'), [
    ('Running com.demo.A\nTests run: 1, Failures: 0\nRunning com.demo.B\n', 'com.demo.B'),
    ('[INFO] Running com.demo.A\n[INFO] Tests run: 1, Failures: 0 - in com.demo.A\n', None),
    ('Running com.demo.A\nRunning com.demo.B\nTests run: 1 - in com.demo.A\n', 'com.demo.B'),
    ('build is quiet', None),
])
def test_unfinished_class(output, expected):
    assert unfinished_surefire_class(output) == expected


@pytest.mark.parametrize('rc', [143, 137, -15, -9])
def test_signal_without_evidence_disables_repair(tmp_path, rc):
    runner = MavenEnforcementRunner('mvn verify', stall_detection_enabled=True)
    result = runner._classify_completed(['mvn', 'verify'], subprocess.CompletedProcess([], rc, 'Running com.demo.B\n', ''), tmp_path)
    assert result.status == QualityGateStatus.command_error
    assert result.evidence['repairEligible'] is False
    assert result.evidence['terminationSignal'] == (abs(rc) if rc < 0 else rc - 128)
    record = SimpleNamespace(status=SimpleNamespace(value='failed'), enforcement_result=result.model_dump())
    assert not can_create_fix_session(record)


def test_production_report_replay(tmp_path):
    fixture = json.loads((Path(__file__).parent / 'fixtures/java_hanging_ad3c157c.json').read_text())
    runner = MavenEnforcementRunner('mvn verify', stall_detection_enabled=True)
    result = runner._classify_completed(['mvn', 'verify'], subprocess.CompletedProcess([], fixture['returncode'], fixture['stdout'], fixture['stderr']), tmp_path)
    assert result.status == QualityGateStatus.command_error
    assert result.evidence['repairEligible'] is False
    assert unfinished_surefire_class(fixture['stdout']) == fixture['hangingClass']


def test_stall_uses_shared_tree_terminator(tmp_path, monkeypatch):
    from uta.enforcement import enforcement as process
    clock = [0.0]
    killed = []
    class FakeProcess:
        pid = 123
        returncode = None
        def poll(self): return self.returncode
        def wait(self, **kwargs): return self.returncode
    proc = FakeProcess()
    def kill(p, descendants):
        killed.append((p.pid, descendants))
        p.returncode = -15
    monkeypatch.setattr(process.subprocess, 'Popen', lambda *a, **kw: proc)
    monkeypatch.setattr(process.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(process.time, 'sleep', lambda n: clock.__setitem__(0, clock[0] + n))
    monkeypatch.setattr(process, '_linux_process_tree_rss_bytes', lambda pid: (0, {123, 124, 125}))
    monkeypatch.setattr(process, '_terminate_process_tree_groups', kill)
    result = process.run_bounded_command(subprocess.run, ['fake'], cwd=tmp_path, timeout=10, stall_seconds=2)
    assert killed == [(123, {123, 124, 125})]
    assert 'UTA_ENFORCEMENT_STALLED stalled-after=2s' in result.stderr
    assert clock[0] == 2


def test_quiet_finished_process_is_not_stalled(tmp_path):
    import sys
    from uta.enforcement.enforcement import run_bounded_command
    result = run_bounded_command(subprocess.run, [sys.executable, '-c', 'pass'], cwd=tmp_path, timeout=5, stall_seconds=1)
    assert result.returncode == 0
    assert not result.stderr


def test_talking_process_survives_threshold(tmp_path):
    import sys
    from uta.enforcement.enforcement import run_bounded_command
    result = run_bounded_command(subprocess.run, [sys.executable, '-u', '-c', 'import time\nfor i in range(8):\n print(i)\n time.sleep(.2)'], cwd=tmp_path, timeout=5, stall_seconds=.6)
    assert result.returncode == 0
    assert '7' in result.stdout


def _repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 't@example.test'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 't'], check=True)
    (repo / 'pom.xml').write_text(
        '<project><artifactId>demo</artifactId></project>\n', encoding='utf-8'
    )
    for relative, body in {
        'src/main/java/com/demo/OrderService.java': 'package com.demo; class OrderService {}\n',
        'src/test/java/com/demo/OrderServiceTest.java':
            'package com.demo; class OrderServiceTest { OrderService value; }\n',
        'src/test/java/com/demo/LegacyTest.java': 'package com.demo; class LegacyTest {}\n',
    }.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m', 'base'], check=True)
    subprocess.run(
        ['git', '-C', str(repo), 'update-ref', 'refs/remotes/origin/master', 'HEAD'],
        check=True,
    )
    production = repo / 'src/main/java/com/demo/OrderService.java'
    production.write_text('package com.demo; class OrderService { int changed; }\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m', 'change'], check=True)
    return repo


def test_unrelated_hang_is_quarantined_and_rerun(tmp_path):
    repo = _repo(tmp_path)
    calls = []

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                cmd, -15, 'Running com.demo.LegacyTest\n',
                'UTA_ENFORCEMENT_STALLED stalled-after=600s',
            )
        return subprocess.CompletedProcess(
            cmd, 0,
            '[INFO] --- test-enforcer:1.0.15:filter-diff @ demo ---\n'
            '[test-enforcer] pitest.targets=1 [com.demo.OrderService*]\n'
            'Diff coverage: 100%\nPIT generated=1 killed=1 survived=0 test-strength=100%\n'
            + DIFF_MUTATION_OK,
            '',
        )

    runner = MavenEnforcementRunner(
        'mvn -Dtest.enforcement.enabled=true verify', run_command=fake_run, full_run=True,
        quarantine_store=SQLiteHangingTestQuarantine(tmp_path / 'tasks.db'),
    )
    result = runner.run(repo)

    assert result.passed
    assert result.evidence['hangingTestClasses'] == ['com.demo.LegacyTest']
    assert result.evidence['quarantinedTestClasses'] == ['com.demo.LegacyTest']
    assert any(arg == '-DexcludedTestClasses=com.demo.LegacyTest' for arg in calls[1])
    assert any(arg.startswith('-Dtest=') and 'OrderServiceTest' in arg for arg in calls[1])


def test_related_hang_is_not_quarantined(tmp_path):
    repo = _repo(tmp_path)

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, -15, 'Running com.demo.OrderServiceTest\n',
            'UTA_ENFORCEMENT_STALLED stalled-after=600s',
        )

    result = MavenEnforcementRunner(
        'mvn -Dtest.enforcement.enabled=true verify', run_command=fake_run, full_run=True
    ).run(repo)
    assert result.status == QualityGateStatus.command_error
    assert result.evidence['repairEligible'] is False
    assert result.evidence['retainedHangingTestClasses'] == ['com.demo.OrderServiceTest']


def test_repeated_hanging_class_stops_after_one_retry(tmp_path):
    repo = _repo(tmp_path)
    calls = []
    timeouts = []

    @with_resolved_enforcer
    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        timeouts.append(kwargs['timeout'])
        return subprocess.CompletedProcess(
            cmd, -15, 'Running com.demo.LegacyTest\n',
            'UTA_ENFORCEMENT_STALLED stalled-after=600s',
        )

    result = MavenEnforcementRunner(
        'mvn -Dtest.enforcement.enabled=true verify',
        run_command=fake_run,
        full_run=True,
        stall_retries=3,
    ).run(repo)
    assert result.status == QualityGateStatus.command_error
    assert result.evidence['stallReason'] == 'repeated-hanging-class'
    assert result.evidence['stallRetries'] == 1
    assert len(calls) == 2
    assert timeouts[1] <= timeouts[0]


def test_cross_task_quarantine_expires_and_diff_clears_it(tmp_path):
    from datetime import datetime, timedelta, timezone
    repo = _repo(tmp_path)
    store = SQLiteHangingTestQuarantine(tmp_path / 'tasks.db')
    key = store_key = 'example/repo'
    old = datetime.now(timezone.utc) - timedelta(days=15)
    store.observe(store_key, 'com.demo.OldTest', '', now=old)
    store.observe(store_key, 'com.demo.LegacyTest', '')
    assert store.active(key, 14) == ['com.demo.LegacyTest']

    runner = MavenEnforcementRunner('mvn verify', quarantine_store=store)
    state = HangingRun(
        runner, repo,
        ['src/test/java/com/demo/LegacyTest.java'],
        ['src/main/java/com/demo/OrderService.java'],
    )
    state.key = key
    state.preexclude(['mvn', 'verify'])
    assert state.pre == []


def test_both_flags_off_preserve_existing_classification(tmp_path):
    runner = MavenEnforcementRunner(
        'mvn -Dtest.enforcement.enabled=true verify',
        stall_detection_enabled=False,
        hanging_test_quarantine_enabled=False,
    )
    result = runner._classify_completed(
        ['mvn', 'verify'],
        subprocess.CompletedProcess([], 143, '', ''),
        tmp_path,
        evidence={'existing': True},
    )
    assert result.status == QualityGateStatus.failed
    assert result.evidence == {'existing': True}


def test_signal_wins_over_partial_build_error_without_gate_evidence(tmp_path):
    runner = MavenEnforcementRunner('mvn verify')
    result = runner._classify_completed(
        ['mvn', 'verify'],
        subprocess.CompletedProcess([], 143, '[ERROR] COMPILATION ERROR', ''),
        tmp_path,
    )
    assert result.status == QualityGateStatus.command_error
    assert result.evidence['repairEligible'] is False


def test_generated_hanging_test_is_retained(tmp_path):
    repo = _repo(tmp_path)
    runner = MavenEnforcementRunner(
        'mvn verify', generated_test_classes=['com.demo.LegacyTest']
    )
    state = HangingRun(
        runner,
        repo,
        ['src/main/java/com/demo/OrderService.java'],
        ['src/main/java/com/demo/OrderService.java'],
    )
    partition = state.partition(['com.demo.LegacyTest'])
    assert partition.excluded == ()
    assert partition.retained == ('com.demo.LegacyTest',)


def test_quarantined_test_is_not_a_repair_target():
    enforcement = {
        'summary': 'PIT baseline tests did not pass without mutation',
        'stdout': 'com.demo.OrderServiceTest failed',
        'stderr': '',
    }
    evidence = {
        'failedSurefireTests': [{'className': 'com.demo.OrderServiceTest'}],
        'filteredTargetClasses': ['com.demo.OrderService'],
        'quarantinedTestClasses': ['com.demo.OrderServiceTest'],
    }
    assert _pit_baseline_failure_target_classes(enforcement, evidence) == []
