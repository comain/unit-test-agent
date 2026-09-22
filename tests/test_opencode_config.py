import json
import tempfile
from pathlib import Path

from uta.shared.config import Settings
from agent_core.harness.config import EXTERNAL_DIRS_CONFIG, generate_opencode_config


def _configure_provider_chain(monkeypatch, chain: str) -> None:
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider_chain", chain)
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider_fallback_enabled", False)
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider_base_urls", "")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider_tokens", "")


def test_settings_defaults_prefer_token_pool_gpt55(monkeypatch):
    for key in (
        "UTA_OPENCODE_PROVIDER",
        "UTA_OPENCODE_MODEL",
        "UTA_OPENCODE_SMALL_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.opencode_provider == "token-pool"
    assert settings.opencode_model == "token-pool/gpt-5.5"
    assert settings.opencode_small_model == "token-pool/gpt-5.5"


def test_settings_accepts_legacy_deepseek_key_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_KEY", "legacy-secret")

    settings = Settings(_env_file=None)

    assert settings.deepseek_api_key == "legacy-secret"


def test_settings_accepts_maven_bin_env(monkeypatch):
    monkeypatch.setenv("UTA_MAVEN_BIN", "/opt/maven/bin/mvn")

    settings = Settings(_env_file=None)

    assert settings.maven_bin == "/opt/maven/bin/mvn"


def test_settings_accepts_uta_base_url_aliases(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("UTA_BASE_URL", "http://uta-base/v1")

    settings = Settings(_env_file=None)

    assert settings.openai_base_url == "http://uta-base/v1"

    monkeypatch.delenv("UTA_BASE_URL", raising=False)
    monkeypatch.setenv("UTA_BAS_URL", "http://uta-bas/v1")

    settings = Settings(_env_file=None)

    assert settings.openai_base_url == "http://uta-bas/v1"


def test_settings_accepts_provider_base_urls_env(monkeypatch):
    monkeypatch.setenv(
        "UTA_OPENCODE_PROVIDER_BASE_URLS",
        "token-pool.base_url=http://token-pool/v1;openai.base_url=http://openai/v1",
    )

    settings = Settings(_env_file=None)

    assert settings.opencode_provider_base_urls == (
        "token-pool.base_url=http://token-pool/v1;openai.base_url=http://openai/v1"
    )


def test_generate_opencode_config_uses_provider_chain_selected_model(tmp_path, monkeypatch):
    _configure_provider_chain(
        monkeypatch,
        "token-pool:token-pool/gpt-5.5,token-pool/gpt-5.5-mini;"
        "openai:openai/gpt-5.4;"
        "deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr(
        "uta.shared.config.settings.opencode_provider_tokens",
        "token-pool.token=tp-secret;openai.token=openai-secret",
    )

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "token-pool/gpt-5.5"
    assert config["small_model"] == "token-pool/gpt-5.5"
    assert config["provider"]["token-pool"]["models"]["gpt-5.5"]["name"] == "gpt-5.5"
    assert config["provider"]["token-pool"]["models"]["gpt-5.5-mini"]["name"] == "gpt-5.5-mini"
    assert config["provider"]["openai"]["models"]["gpt-5.4"]["name"] == "gpt-5.4"
    assert config["provider"]["deepseek"]["models"]["deepseek-v4-pro"]["name"] == "deepseek-v4-pro"
    serialized = json.dumps(config)
    assert "tp-secret" not in serialized
    assert "openai-secret" not in serialized


def test_generate_opencode_config_honors_task_selected_model_in_chain(tmp_path, monkeypatch):
    _configure_provider_chain(
        monkeypatch,
        "token-pool:token-pool/gpt-5.5;openai:openai/gpt-5.4",
    )
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "openai/gpt-5.4"
    assert config["small_model"] == "openai/gpt-5.4"
    assert config["provider"]["token-pool"]["models"]["gpt-5.5"]["name"] == "gpt-5.5"
    assert config["provider"]["openai"]["models"]["gpt-5.4"]["name"] == "gpt-5.4"


def test_generate_opencode_config_uses_first_available_candidate(tmp_path, monkeypatch):
    from agent_core.harness.tiered_router import ProviderCandidate

    _configure_provider_chain(
        monkeypatch,
        "token-pool:token-pool/gpt-5.5,token-pool/gpt-5.4;openai:openai/gpt-5.4",
    )
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "legacy/gpt-4o")
    monkeypatch.setattr(
        "agent_core.harness.config.available_provider_candidates",
        lambda *, fallback_enabled=None: [
            ProviderCandidate("openai", "openai/gpt-5.4", 2)
        ],
    )

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "openai/gpt-5.4"
    assert config["small_model"] == "openai/gpt-5.4"
    assert config["provider"]["token-pool"]["models"]["gpt-5.5"]["name"] == "gpt-5.5"
    assert config["provider"]["openai"]["models"]["gpt-5.4"]["name"] == "gpt-5.4"


def test_generate_opencode_config_uses_provider_scoped_base_urls(tmp_path, monkeypatch):
    _configure_provider_chain(
        monkeypatch,
        "token-pool:token-pool/gpt-5.5;openai:openai/gpt-5.4;deepseek:deepseek/deepseek-v4-pro",
    )
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "token-pool/gpt-5.5")
    monkeypatch.setattr(
        "uta.shared.config.settings.opencode_provider_base_urls",
        (
            "token-pool.base_url=http://token-pool.test/v1;"
            "openai.base_url=http://openai.test/v1;"
            "deepseek.base_url=http://deepseek.test/v1"
        ),
    )
    monkeypatch.setattr("uta.shared.config.settings.openai_base_url", "http://legacy.test/v1")
    monkeypatch.setattr("uta.shared.config.settings.openai_api_key", None)
    monkeypatch.setattr("uta.shared.config.settings.deepseek_api_key", None)

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["provider"]["token-pool"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["token-pool"]["options"]["baseURL"] == "http://token-pool.test/v1"
    assert config["provider"]["openai"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["openai"]["options"]["baseURL"] == "http://openai.test/v1"
    assert config["provider"]["deepseek"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["deepseek"]["options"]["baseURL"] == "http://deepseek.test/v1"


def test_generate_opencode_config_registers_openrouter_models(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "openrouter:openrouter/z-ai/glm-5.1")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openrouter/z-ai/glm-5.1")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "openrouter/z-ai/glm-5.1")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_provider_only", "")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_provider_order", "")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "openrouter/z-ai/glm-5.1"
    assert config["small_model"] == "openrouter/z-ai/glm-5.1"
    assert config["provider"]["openrouter"]["models"]["z-ai/glm-5.1"]["name"] == "z-ai/glm-5.1"
    assert "options" not in config["provider"]["openrouter"]["models"]["z-ai/glm-5.1"]


def test_generate_opencode_config_pins_openrouter_provider(tmp_path, monkeypatch):
    _configure_provider_chain(
        monkeypatch,
        "openrouter:openrouter/moonshotai/kimi-k2.6",
    )
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openrouter/moonshotai/kimi-k2.6")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "openrouter/moonshotai/kimi-k2.6")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_provider_only", "moonshotai")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_provider_order", "")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_allow_fallbacks", False)
    monkeypatch.setattr("uta.shared.config.settings.openrouter_require_parameters", True)

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    provider = config["provider"]["openrouter"]["models"]["moonshotai/kimi-k2.6"]["options"]["provider"]
    assert provider == {
        "only": ["moonshotai"],
        "order": ["moonshotai"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_generate_opencode_config_auto_pins_openrouter_provider_from_model_namespace(tmp_path, monkeypatch):
    _configure_provider_chain(
        monkeypatch,
        "openrouter:openrouter/moonshotai/kimi-k2.6",
    )
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openrouter/moonshotai/kimi-k2.6")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "openrouter/moonshotai/kimi-k2.6")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_provider_only", "auto")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_provider_order", "")
    monkeypatch.setattr("uta.shared.config.settings.openrouter_allow_fallbacks", False)
    monkeypatch.setattr("uta.shared.config.settings.openrouter_require_parameters", True)

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    provider = config["provider"]["openrouter"]["models"]["moonshotai/kimi-k2.6"]["options"]["provider"]
    assert provider["only"] == ["moonshotai"]
    assert provider["order"] == ["moonshotai"]
    assert provider["allow_fallbacks"] is False
    assert provider["require_parameters"] is True


def test_generate_opencode_config_keeps_google_support(tmp_path, monkeypatch):
    _configure_provider_chain(
        monkeypatch,
        "google:google/gemini-3.1-pro-preview,google/gemini-2.5-flash",
    )
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "google/gemini-3.1-pro-preview")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "google/gemini-2.5-flash")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["provider"]["google"]["models"]["gemini-3.1-pro-preview"]["name"] == "gemini-3.1-pro-preview"
    assert config["provider"]["google"]["models"]["gemini-2.5-flash"]["name"] == "gemini-2.5-flash"


def test_generate_opencode_config_registers_openai_models(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "openai:openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.openai_base_url", None)

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "openai/gpt-5.4"
    assert config["small_model"] == "openai/gpt-5.4"
    assert config["provider"]["openai"]["models"]["gpt-5.4"]["name"] == "gpt-5.4"


def test_generate_opencode_config_uses_compatible_provider_for_openai_base_url(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "openai:openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.openai_base_url", "http://token-pool.example.com/v1")
    monkeypatch.setattr("uta.shared.config.settings.openai_api_key", "token-pool-key")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    provider = config["provider"]["openai"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://token-pool.example.com/v1"
    assert provider["options"]["apiKey"] == "token-pool-key"
    assert provider["models"]["gpt-5.4"]["name"] == "gpt-5.4"


def test_generate_opencode_config_uses_base_url_for_custom_compatible_provider(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "myproxy:myproxy/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "myproxy")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "myproxy/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "myproxy/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.openai_base_url", "http://10.0.0.10:8317/v1")
    monkeypatch.setattr("uta.shared.config.settings.openai_api_key", "token-pool-key")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    provider = config["provider"]["myproxy"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://10.0.0.10:8317/v1"
    assert provider["options"]["apiKey"] == "token-pool-key"
    assert provider["models"]["gpt-5.4"]["name"] == "gpt-5.4"


def test_generate_opencode_config_registers_tencent_models(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "tencent:tencent/glm-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "tencent/glm-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "tencent/glm-5")
    monkeypatch.setattr("uta.shared.config.settings.tencent_api_key", "tencent-secret")
    monkeypatch.setattr("uta.shared.config.settings.tencent_base_url", "https://tokenhub.tencentmaas.com/v1")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "tencent/glm-5"
    assert config["small_model"] == "tencent/glm-5"
    assert config["provider"]["tencent"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["tencent"]["options"]["baseURL"] == "https://tokenhub.tencentmaas.com/v1"
    assert config["provider"]["tencent"]["options"]["apiKey"] == "tencent-secret"
    assert config["provider"]["tencent"]["models"]["glm-5"]["name"] == "glm-5"


def test_generate_opencode_config_registers_plain_model_for_configured_provider(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "tencent:glm-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "glm-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "glm-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "tencent")
    monkeypatch.setattr("uta.shared.config.settings.tencent_api_key", "tencent-secret")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "tencent/glm-5"
    assert config["small_model"] == "tencent/glm-5"
    assert config["provider"]["tencent"]["models"]["glm-5"]["name"] == "glm-5"


def test_generate_opencode_config_qualifies_bare_token_pool_chain_model(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "token-pool:gpt-5.5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "gpt-5.5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "gpt-5.5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "token-pool")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "token-pool/gpt-5.5"
    assert config["small_model"] == "token-pool/gpt-5.5"
    assert config["provider"]["token-pool"]["models"]["gpt-5.5"]["name"] == "gpt-5.5"


def test_generate_opencode_config_registers_ollama_models(tmp_path, monkeypatch):
    _configure_provider_chain(
        monkeypatch,
        "ollama:ollama/qwen3.5:35b-a3b-coding-nvfp4",
    )
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "ollama/qwen3.5:35b-a3b-coding-nvfp4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "ollama/qwen3.5:35b-a3b-coding-nvfp4")
    monkeypatch.setattr("uta.shared.config.settings.ollama_host", "http://127.0.0.1:11434")
    monkeypatch.setattr("uta.shared.config.settings.ollama_num_ctx", 262144)

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config["model"] == "ollama/qwen3.5:35b-a3b-coding-nvfp4"
    assert config["small_model"] == "ollama/qwen3.5:35b-a3b-coding-nvfp4"
    assert config["provider"]["ollama"]["npm"] == "@ai-sdk/openai-compatible"
    assert config["provider"]["ollama"]["options"]["baseURL"] == "http://127.0.0.1:11434/v1"
    assert config["provider"]["ollama"]["models"]["qwen3.5:35b-a3b-coding-nvfp4"]["name"] == "qwen3.5:35b-a3b-coding-nvfp4"
    assert config["provider"]["ollama"]["models"]["qwen3.5:35b-a3b-coding-nvfp4"]["limit"]["context"] == 262144
    assert config["provider"]["ollama"]["models"]["qwen3.5:35b-a3b-coding-nvfp4"]["options"]["num_ctx"] == 262144


def test_generate_opencode_config_registers_cursor_models(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "cursor:cursor/gpt-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "cursor/gpt-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "cursor/gpt-5")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert "plugin" not in config
    assert config["provider"]["cursor"]["name"] == "Cursor"
    assert config["provider"]["cursor"]["models"]["gpt-5"]["name"] == "gpt-5"
    assert config["provider"]["cursor"]["models"]["gpt-5"]["limit"]["context"] == 262144


def test_generate_opencode_config_registers_plain_cursor_model_without_plugin(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "cursor:gpt-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "gpt-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "gpt-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_provider", "cursor")
    global_config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    monkeypatch.setattr("agent_core.harness.config.GLOBAL_OPENCODE_CONFIG", global_config_path)
    monkeypatch.setattr("agent_core.harness.config.GLOBAL_OPENCODE_PLUGIN_ROOT", tmp_path / ".config" / "opencode")
    monkeypatch.setattr("agent_core.harness.config.OPENCODE_PLUGIN_CACHE_ROOT", tmp_path / ".cache" / "opencode" / "packages")

    config_path = generate_opencode_config(str(tmp_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert "plugin" not in config
    assert config["provider"]["cursor"]["models"]["gpt-5"]["name"] == "gpt-5"
    assert not global_config_path.exists()
    assert not (tmp_path / ".cache" / "opencode").exists()


def test_generate_opencode_config_does_not_mutate_global_opencode_config(tmp_path, monkeypatch):
    _configure_provider_chain(monkeypatch, "cursor:cursor/gpt-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "cursor/gpt-5")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "cursor/gpt-5")

    global_config_path = tmp_path / ".config" / "opencode" / "opencode.json"
    global_config_path.parent.mkdir(parents=True)
    original = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"lmstudio": {"name": "LM Studio"}},
        "plugin": ["existing-plugin"],
    }
    global_config_path.write_text(json.dumps(original, indent=2), encoding="utf-8")
    monkeypatch.setattr("agent_core.harness.config.GLOBAL_OPENCODE_CONFIG", global_config_path)

    generate_opencode_config(str(tmp_path))

    assert json.loads(global_config_path.read_text(encoding="utf-8")) == original


def test_generate_opencode_config_allows_temp_and_sibling_api_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "openai/gpt-5.4")

    repo_root = tmp_path / "wms" / "sample-outbound-core"
    repo_root.mkdir(parents=True)
    configured_allow = [
        f"{(tmp_path / 'wms').resolve()}/**",
        f"{(tmp_path / 'wms' / 'sample-wms-outbound-core-api').resolve()}/**",
    ]
    config_override = tmp_path / "opencode_external_dirs.json"
    config_override.write_text(
        json.dumps({"allow": configured_allow}, indent=2),
        encoding="utf-8",
    )
    monkeypatch.setattr("agent_core.harness.config.EXTERNAL_DIRS_CONFIG", config_override)

    config_path = generate_opencode_config(str(repo_root))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    external = config["permission"]["external_directory"]
    assert external["/tmp/**"] == "allow"
    assert external[f"{Path(tempfile.gettempdir()).resolve()}/**"] == "allow"
    assert external[f"{(Path.home() / '.m2').resolve()}/**"] == "allow"
    assert external[configured_allow[0]] == "allow"
    assert external[configured_allow[1]] == "allow"


def test_generate_opencode_config_allows_env_source_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr("uta.shared.config.settings.opencode_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.opencode_small_model", "openai/gpt-5.4")
    monkeypatch.setattr("uta.shared.config.settings.index_source_dirs", str(tmp_path / "code" / "wms"))
    monkeypatch.setattr("uta.shared.config.settings.opencode_external_dirs", "")

    repo_root = tmp_path / "code" / "wms" / "sample-outbound-core"
    repo_root.mkdir(parents=True)

    config_path = generate_opencode_config(str(repo_root))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    external = config["permission"]["external_directory"]
    assert external[f"{(tmp_path / 'code' / 'wms').resolve()}/**"] == "allow"


def test_external_directory_config_file_exists():
    assert EXTERNAL_DIRS_CONFIG.exists()


def test_external_directory_config_includes_platform_workspace():
    config = json.loads(EXTERNAL_DIRS_CONFIG.read_text(encoding="utf-8"))

    assert "/home/user/platform/**" in config["allow"]
