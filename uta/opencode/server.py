import subprocess
import time
import httpx
import os
import datetime
import tempfile
import json
from pathlib import Path
from uta.config import settings
from uta.opencode.net import build_base_url


class OpenCodeServer:
    def __init__(self, repo_path: str, port: int = None):
        self.repo_path = repo_path
        self.port = port or settings.opencode_port
        self.host = settings.opencode_host
        self.process = None
        self.log_path = None
        self._log_handle = None

    def _poll_returncode(self):
        if not self.process:
            return None
        poll = getattr(self.process, "poll", None)
        if callable(poll):
            return poll()
        return None

    def _terminate_stale_listeners(self) -> None:
        """Kill stale listeners on the configured port before launching OpenCode."""
        try:
            result = subprocess.run(
                ["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return

        raw_pids = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if not raw_pids:
            return

        for raw_pid in raw_pids:
            try:
                pid = int(raw_pid)
            except Exception:
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                continue
            except Exception:
                continue

        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                check = subprocess.run(
                    ["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except Exception:
                return
            still_listening = [
                line.strip()
                for line in (check.stdout or "").splitlines()
                if line.strip() and line.strip() not in raw_pids
            ]
            if not still_listening and not (check.stdout or "").strip():
                return
            time.sleep(0.2)

    @staticmethod
    def _configured_providers():
        providers = set()
        for model in (settings.opencode_model, settings.opencode_small_model):
            if model and "/" in model:
                providers.add(model.split("/", 1)[0])
        if settings.opencode_provider:
            providers.add(settings.opencode_provider)
        return providers

    def start(self):
        self._terminate_stale_listeners()
        cmd = self._build_cmd()
        if settings.opencode_server_debug:
            cmd.extend(["--log-level", "DEBUG"])
        if settings.opencode_server_print_logs:
            cmd.append("--print-logs")
        env = os.environ.copy()
        # Pass provider credentials through to the OpenCode server.
        gemini_api_key = settings.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
        if gemini_api_key:
            env["GOOGLE_GENERATIVE_AI_API_KEY"] = gemini_api_key
            env["GOOGLE_API_KEY"] = gemini_api_key
        openrouter_api_key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if openrouter_api_key:
            env["OPENROUTER_API_KEY"] = openrouter_api_key
        deepseek_api_key = settings.deepseek_api_key or os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("DEEPSEEK_KEY", "")
        if deepseek_api_key:
            env["DEEPSEEK_API_KEY"] = deepseek_api_key
        tencent_api_key = settings.tencent_api_key or os.environ.get("TENCENT_API_KEY", "")
        if tencent_api_key:
            env["TENCENT_API_KEY"] = tencent_api_key
        tencent_base_url = settings.tencent_base_url or os.environ.get("TENCENT_BASE_URL", "")
        if tencent_base_url:
            env["TENCENT_BASE_URL"] = tencent_base_url
        ollama_host = settings.ollama_host or os.environ.get("OLLAMA_HOST", "")
        if ollama_host:
            env["OLLAMA_HOST"] = ollama_host
        # When UTA targets native OpenCode OpenAI auth, do not leak a host OPENAI_API_KEY
        # into the server. Otherwise OpenCode may bypass ChatGPT subscription OAuth and
        # silently fall back to stale API-key auth.
        if "openai" in self._configured_providers():
            env.pop("OPENAI_API_KEY", None)
        popen_kwargs = {
            "cwd": self.repo_path,
            "env": env,
        }
        if settings.opencode_server_print_logs and settings.opencode_server_log_to_file:
            log_dir = Path(tempfile.gettempdir()) / "uta-run-logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            repo_slug = Path(self.repo_path).name or "repo"
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.log_path = str(log_dir / f"{repo_slug}_opencode_{timestamp}.log")
            self._log_handle = open(self.log_path, "a", buffering=1)
            popen_kwargs["stdout"] = self._log_handle
            popen_kwargs["stderr"] = subprocess.STDOUT
        elif settings.opencode_server_print_logs:
            popen_kwargs["stdout"] = None
            popen_kwargs["stderr"] = None
        else:
            popen_kwargs["stdout"] = subprocess.PIPE
            popen_kwargs["stderr"] = subprocess.PIPE

        self.process = subprocess.Popen(cmd, **popen_kwargs)

        # Wait for server to be ready (first request bootstraps, can be slow)
        max_retries = 30
        for i in range(max_retries):
            if self._poll_returncode() is not None:
                raise RuntimeError(f"OpenCode server exited early with code {self.process.returncode}")
            try:
                response = httpx.get(
                    f"{build_base_url(self.host, self.port)}/session",
                    timeout=5.0
                )
                if response.status_code == 200:
                    if self._poll_returncode() is not None:
                        raise RuntimeError(f"OpenCode server exited early with code {self.process.returncode}")
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout):
                pass
            time.sleep(1)

        raise RuntimeError("OpenCode server failed to start")

    def _build_cmd(self):
        serve_cmd_raw = settings.opencode_serve_cmd
        if serve_cmd_raw:
            try:
                base = json.loads(serve_cmd_raw)
                if not isinstance(base, list):
                    base = [str(serve_cmd_raw)]
            except (json.JSONDecodeError, TypeError):
                base = [serve_cmd_raw]
            cmd = [str(part) for part in base]
        else:
            bin_path = settings.opencode_bin or "opencode"
            cmd = [bin_path, "serve"]

        if "--port" not in cmd:
            cmd.extend(["--port", str(self.port)])
        if "--hostname" not in cmd:
            cmd.extend(["--hostname", self.host])
        return cmd

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
