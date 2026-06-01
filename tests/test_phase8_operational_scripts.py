from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_enqueue_wrapper_documents_python_target_options_and_uses_configured_python():
    text = _read("scripts/enqueue.sh")

    assert "--language python" in text
    assert "--target jobs/forecast.py" in text
    assert "UTA_VENV_DIR" in text
    assert 'exec "$PYTHON_BIN" -m uta.cli tasks enqueue' in text


def test_daemon_and_ci_wrappers_surface_python_enforcement_runtime():
    for script in ("scripts/start_daemon.sh", "scripts/start_ci_plugin.sh"):
        text = _read(script)

        assert "configure_python_enforcement_runtime" in text
        assert "UTA_PYTHON_BIN" in text
        assert "UTA_PYTHON2_BIN" in text
        assert "UTA_PYTHON2_MUTMUT_BIN" in text
        assert "Python enforcement:" in text
        assert 'export PATH="$VENV_DIR/bin:$PATH"' in text
        assert 'export UTA_SERVICE_PYTHON_BIN="$VENV_DIR/bin/python"' in text
        assert 'export UTA_PYTHON_BIN="$VENV_DIR/bin/python"' not in text


def test_deploy_wrapper_documents_python_enforcement_readiness_checks():
    text = _read("scripts/deploy_single_host.sh")

    assert "Python enforcement:" in text
    assert "uta python-enforce --help" in text
    assert "python -c \"import tree_sitter_python\"" in text
    assert "UTA_PYTHON2_BIN" in text


def test_fetchcode_setup_documents_python_flat_repo_mode():
    text = _read("scripts/setup-fetchcode.py")

    assert "Python and flat script repositories" in text
    assert "Use this for Python-only or flat-script repo setup" in text
