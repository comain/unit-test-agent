from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_enqueue_wrapper_documents_python_target_options_and_uses_configured_python():
    text = _read("scripts/enqueue.sh")

    assert "--language python" in text
    assert "--target jobs/forecast.py" in text
    assert "UTA_VENV_DIR" in text
    assert 'exec "$PYTHON_BIN" -m uta.app.cli tasks enqueue' in text


def test_daemon_and_ci_wrappers_surface_python_enforcement_runtime():
    for script in ("scripts/start_daemon.sh", "scripts/start_api_trigger.sh"):
        text = _read(script)

        assert "configure_python_enforcement_runtime" in text
        assert "UTA_PYTHON_BIN" in text
        assert "UTA_PYTHON_MUTMUT_BIN" in text
        assert "UTA_PYTHON2_BIN" in text
        assert "UTA_PYTHON2_MUTMUT_BIN" in text
        assert "Python enforcement:" in text
        assert "Java enforcement:" in text
        assert 'DEFAULT_' in text
        assert 'export PATH="$VENV_DIR/bin:$PATH"' in text
        assert 'export UTA_SERVICE_PYTHON_BIN="$VENV_DIR/bin/python"' in text
        assert 'export UTA_PYTHON_BIN="$VENV_DIR/bin/python"' not in text


def test_daemon_restart_waits_for_running_tasks_before_stop():
    text = _read("scripts/start_daemon.sh")

    assert "wait_for_running_tasks" in text
    assert "active_running_task_count" in text
    assert "from repo_tasks" in text
    assert "where status='RUNNING'" in text
    assert "Timed out waiting for running UTA repo tasks" in text
    assert "--force-stop" in text
    assert 'pkill -f "uta/app/cli.py tasks daemon"' not in text
    assert 'pgrep -f "uta/app/cli[.]py tasks daemon"' not in text
    assert "python .*uta/app/cli[.]py tasks daemon" in text
    assert "stop_existing_daemon_processes" in text
    assert """if [[ "$WAIT_FOR_RUNNING_TASKS" == "1" ]]; then
    wait_for_running_tasks
  fi
  stop_existing_daemon_processes
  rm -f "$PID_FILE"
fi""" in text


def test_api_trigger_restart_waits_only_for_active_enforcement_runs_before_stop():
    text = _read("scripts/start_api_trigger.sh")

    assert "wait_for_enforcement_runs" in text
    assert "active_enforcement_task_count" in text
    assert "from class_tasks" in text
    assert "status='RUNNING'" in text
    assert "python_verify" in text
    assert "python_fix_mutations" not in text
    assert "stop_existing_plugin_processes" in text
    assert "uvicorn uta[.]app.app:app" in text
    assert "uvicorn uta[.]api_trigger.app:app" in text
    assert "uvicorn uta[.]ci_plugin.app:app" in text
    assert "Force stopping stale UTA API trigger process tree" in text
    assert "STOP_GRACE_SECONDS" in text
    assert "UTA_CI_INFLIGHT_DIR" in text
    assert "active_ci_inflight_count" not in text
    assert "running_task_process_count" not in text
    assert "ci_enforcement_process_count" not in text
    assert "ps -eo args=" not in text
    assert "--force-stop" in text
    assert "--stop-wait-timeout" in text
    assert "Timed out waiting for active enforcement task rows" in text
    assert """if [[ "$WAIT_FOR_RUNNING_TASKS" == "1" ]]; then
    wait_for_enforcement_runs
  fi
  stop_existing_plugin_processes
  rm -f "$PID_FILE"
fi""" in text


def test_api_trigger_supervisor_restarts_the_foreground_process():
    text = _read("config/supervisor/uta_api.ini")
    launcher = _read("scripts/start_api_trigger.sh")

    assert "[program:uta_api]" in text
    assert "scripts/start_api_trigger.sh --foreground --no-stop" in text
    assert "autostart=true" in text
    assert "autorestart=true" in text
    assert "startsecs=5" in text
    assert "stopasgroup=true" in text
    assert "killasgroup=true" in text
    assert 'echo "$$" >"$PID_FILE"' in launcher


def test_deploy_wrapper_documents_python_enforcement_readiness_checks():
    text = _read("scripts/deploy_single_host.sh")

    assert "Python enforcement:" in text
    assert "uta python-enforce --help" in text
    assert "python -c \"import tree_sitter_python\"" in text
    assert "UTA_PYTHON_MUTMUT_BIN" in text
    assert "UTA_PYTHON2_BIN" in text


def test_deploy_installs_and_verifies_pytest_as_a_runtime_dependency():
    runtime_dependencies = _read("pyproject.toml").split("[project.optional-dependencies]", 1)[0]

    assert '"pytest>=8.0"' in runtime_dependencies
    deploy_script = _read("scripts/deploy_single_host.sh")
    assert 'pip install --no-build-isolation -e "$ROOT_DIR"' in deploy_script
    assert '"$VENV_DIR/bin/python" -m pytest --version' in deploy_script


def test_fetchcode_setup_documents_python_flat_repo_mode():
    text = _read("scripts/setup-fetchcode.py")

    assert "Python and flat script repositories" in text
    assert "Use this for Python-only or flat-script repo setup" in text
