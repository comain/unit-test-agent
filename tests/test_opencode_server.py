import httpx
from pathlib import Path

from uta.opencode.server import OpenCodeServer


def test_server_strips_openai_api_key_for_openai_models(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_small_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "stale-key")

    popen_calls = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        popen_calls["env"] = env
        return DummyProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert "OPENAI_API_KEY" not in popen_calls["env"]


def test_server_uses_custom_serve_cmd_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "uta.config.settings.opencode_serve_cmd",
        '["bun", "run", "--cwd", "/tmp/opencode", "./src/index.ts", "serve"]',
    )

    popen_calls = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        popen_calls["cmd"] = cmd
        return DummyProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert popen_calls["cmd"][:6] == ["bun", "run", "--cwd", "/tmp/opencode", "./src/index.ts", "serve"]
    assert popen_calls["cmd"][-4:] == ["--port", "4096", "--hostname", "127.0.0.1"]


def test_server_uses_ipv6_host_for_probe_and_cmd(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.config.settings.opencode_host", "::1")

    observed = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        observed["cmd"] = cmd
        return DummyProcess()

    def fake_get(url, timeout):
        observed["url"] = url
        return httpx.Response(200)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", fake_get)

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert observed["url"] == "http://[::1]:4096/session"
    assert observed["cmd"][-4:] == ["--port", "4096", "--hostname", "::1"]


def test_server_keeps_openai_api_key_for_non_openai_models(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.config.settings.opencode_model", "openrouter/z-ai/glm-5.1")
    monkeypatch.setattr("uta.config.settings.opencode_small_model", "openrouter/z-ai/glm-5.1")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "openrouter")
    monkeypatch.setenv("OPENAI_API_KEY", "host-key")

    popen_calls = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        popen_calls["env"] = env
        return DummyProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert popen_calls["env"]["OPENAI_API_KEY"] == "host-key"


def test_server_persists_debug_log_when_print_logs_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.config.settings.opencode_server_print_logs", True)
    monkeypatch.setattr("uta.config.settings.opencode_server_log_to_file", True)

    popen_calls = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        popen_calls["stdout"] = stdout
        popen_calls["stderr"] = stderr
        return DummyProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()
    try:
        assert server.log_path is not None
        assert Path(server.log_path).name.startswith(f"{tmp_path.name}_opencode_")
        assert popen_calls["stdout"] is not None
        assert popen_calls["stderr"] is not None
    finally:
        server.stop()


def test_server_passes_ollama_host_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.config.settings.opencode_model", "ollama/qwen3.5:35b-a3b-coding-nvfp4")
    monkeypatch.setattr("uta.config.settings.opencode_small_model", "ollama/qwen3.5:35b-a3b-coding-nvfp4")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "ollama")
    monkeypatch.setattr("uta.config.settings.ollama_host", "http://127.0.0.1:11434")

    popen_calls = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        popen_calls["env"] = env
        return DummyProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert popen_calls["env"]["OLLAMA_HOST"] == "http://127.0.0.1:11434"


def test_server_passes_tencent_credentials_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.config.settings.opencode_model", "tencent/glm-5")
    monkeypatch.setattr("uta.config.settings.opencode_small_model", "tencent/glm-5")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "tencent")
    monkeypatch.setattr("uta.config.settings.tencent_api_key", "tencent-secret")
    monkeypatch.setattr("uta.config.settings.tencent_base_url", "https://tokenhub.tencentmaas.com/v1")

    popen_calls = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        popen_calls["env"] = env
        return DummyProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert popen_calls["env"]["TENCENT_API_KEY"] == "tencent-secret"
    assert popen_calls["env"]["TENCENT_BASE_URL"] == "https://tokenhub.tencentmaas.com/v1"


def test_server_passes_deepseek_credentials_when_configured(monkeypatch, tmp_path):
    monkeypatch.setattr("uta.config.settings.opencode_model", "deepseek/deepseek-v4-pro")
    monkeypatch.setattr("uta.config.settings.opencode_small_model", "deepseek/deepseek-v4-pro")
    monkeypatch.setattr("uta.config.settings.opencode_provider", "deepseek")
    monkeypatch.setattr("uta.config.settings.deepseek_api_key", "deepseek-secret")

    popen_calls = {}

    class DummyProcess:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, cwd, stdout, stderr, env):
        popen_calls["env"] = env
        return DummyProcess()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert popen_calls["env"]["DEEPSEEK_API_KEY"] == "deepseek-secret"


def test_server_terminates_stale_listener_before_start(monkeypatch, tmp_path):
    observed = {"lsof_calls": 0, "killed": []}

    class DummyCompleted:
        def __init__(self, stdout):
            self.stdout = stdout

    class DummyProcess:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_run(cmd, capture_output, text, check):
        observed["lsof_calls"] += 1
        if observed["lsof_calls"] == 1:
            return DummyCompleted("4321\n")
        return DummyCompleted("")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("os.kill", lambda pid, sig: observed["killed"].append((pid, sig)))
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: DummyProcess())
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))
    server.start()

    assert observed["killed"] == [(4321, 15)]


def test_server_does_not_accept_existing_listener_when_spawned_process_exits(monkeypatch, tmp_path):
    class DummyProcess:
        returncode = 1

        def poll(self):
            return 1

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: type("Completed", (), {"stdout": ""})())
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: DummyProcess())
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: httpx.Response(200))

    server = OpenCodeServer(str(tmp_path))

    try:
        server.start()
        assert False, "Expected OpenCode server start to fail when spawned process exits immediately"
    except RuntimeError as exc:
        assert "exited early" in str(exc)
