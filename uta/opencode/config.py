import json
import tempfile
from pathlib import Path
from typing import Optional
from uta.config import settings
from uta.opencode.tiered_router import (
    available_provider_candidates,
    parse_provider_base_urls,
    parse_provider_chain,
    provider_candidates,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXTERNAL_DIRS_CONFIG = PROJECT_ROOT / "config" / "opencode_external_dirs.json"
EXTERNAL_DIRS_EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "opencode_external_dirs.example.json"
GLOBAL_OPENCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
GLOBAL_OPENCODE_PLUGIN_ROOT = Path.home() / ".config" / "opencode"
OPENCODE_PLUGIN_CACHE_ROOT = Path.home() / ".cache" / "opencode" / "packages"
CURSOR_PLUGIN_NAME = "opencode-cursor-oauth"


def _csv_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _path_list(value: str) -> list[str]:
    normalized = (value or "").replace("\n", ",")
    if ":" in normalized:
        normalized = normalized.replace(":", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _permission_pattern(path_value: str) -> str:
    if "*" in path_value:
        return path_value
    return f"{Path(path_value).expanduser().resolve()}/**"


def _openrouter_model_options(model_id: str) -> dict:
    """OpenRouter reads routing controls from providerOptions.openrouter.provider."""
    only = _csv_list(settings.openrouter_provider_only)
    order = _csv_list(settings.openrouter_provider_order)
    if only == ["auto"] and "/" in model_id:
        only = [model_id.split("/", 1)[0]]
    if order == ["auto"] and "/" in model_id:
        order = [model_id.split("/", 1)[0]]
    provider: dict = {}

    if only:
        provider["only"] = only
        provider["order"] = order or only
    elif order:
        provider["order"] = order

    if not provider:
        return {}

    provider["allow_fallbacks"] = settings.openrouter_allow_fallbacks
    if settings.openrouter_require_parameters:
        provider["require_parameters"] = True
    return {"provider": provider}


def _model_limit(provider_id: str) -> dict:
    if provider_id == "google":
        return {
            "context": 1048576,
            "output": 65536,
        }
    if provider_id == "tencent":
        return {
            "context": 262144,
            "output": 65536,
        }
    if provider_id == "ollama":
        return {
            "context": settings.ollama_num_ctx,
            "output": 32768,
        }
    if provider_id == "openrouter":
        return {
            "context": 203000,
            "output": 65536,
        }
    if provider_id == "deepseek":
        return {
            "context": 1048576,
            "output": 65536,
        }
    if provider_id == "openai":
        return {
            "context": 272000,
            "output": 128000,
        }
    if provider_id == "cursor":
        return {
            "context": 262144,
            "output": 65536,
        }
    return {
        "context": 262144,
        "output": 32768,
    }


def _provider_base_url(provider_id: str) -> str:
    configured = parse_provider_base_urls(settings.opencode_provider_base_urls)
    if configured.get(provider_id):
        return configured[provider_id]
    if provider_id in {"openai", "token-pool"}:
        return (settings.openai_base_url or "").rstrip("/")
    if provider_id == "tencent":
        return (settings.tencent_base_url or "").rstrip("/")
    if provider_id == "ollama":
        ollama_host = (settings.ollama_host or "").rstrip("/")
        return f"{ollama_host}/v1" if ollama_host else ""
    if provider_id not in {"cursor", "deepseek", "google", "openrouter"}:
        return (settings.openai_base_url or "").rstrip("/")
    return ""


def _register_provider_models(config: dict, provider_id: str, *models: str) -> None:
    provider = config.setdefault("provider", {}).setdefault(provider_id, {"models": {}})
    provider_base_url = _provider_base_url(provider_id)
    if provider_id == "tencent":
        tencent_base = provider_base_url or "https://tokenhub.tencentmaas.com/v1"
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "Tencent TokenHub")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", tencent_base)
        if settings.tencent_api_key:
            provider["options"].setdefault("apiKey", settings.tencent_api_key)
    if provider_id == "ollama":
        ollama_base = provider_base_url or f"{(settings.ollama_host or 'http://127.0.0.1:11434').rstrip('/')}/v1"
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "Ollama (local)")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", ollama_base)
    if provider_id == "openai" and provider_base_url:
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "OpenAI-compatible")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", provider_base_url)
        if settings.openai_api_key:
            provider["options"].setdefault("apiKey", settings.openai_api_key)
    if provider_id == "deepseek" and provider_base_url:
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", "DeepSeek OpenAI-compatible")
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", provider_base_url)
        if settings.deepseek_api_key:
            provider["options"].setdefault("apiKey", settings.deepseek_api_key)
    if (
        provider_id not in {"google", "openrouter", "deepseek", "tencent", "ollama", "openai", "cursor"}
        and provider_base_url
    ):
        provider.setdefault("npm", "@ai-sdk/openai-compatible")
        provider.setdefault("name", provider_id)
        provider.setdefault("options", {})
        provider["options"].setdefault("baseURL", provider_base_url)
        if settings.openai_api_key:
            provider["options"].setdefault("apiKey", settings.openai_api_key)
    if provider_id == "cursor":
        provider.setdefault("name", "Cursor")
    for full_model in models:
        if full_model.startswith(f"{provider_id}/"):
            model_id = full_model.split("/", 1)[1]
        elif "/" not in full_model:
            model_id = full_model
        else:
            continue
        provider["models"].setdefault(
            model_id,
            {
                "name": model_id,
                "limit": _model_limit(provider_id),
            },
        )
        if provider_id == "ollama":
            provider["models"][model_id].setdefault("options", {})
            provider["models"][model_id]["options"].setdefault("num_ctx", settings.ollama_num_ctx)
        if provider_id == "openrouter":
            options = _openrouter_model_options(model_id)
            if options:
                provider["models"][model_id].setdefault("options", {})
                provider["models"][model_id]["options"].update(options)


def _external_directory_permissions(repo_path: str) -> dict:
    permissions = {}

    # Headless runs can wedge when OpenCode asks for approval on temp scratch paths.
    permissions["/tmp/**"] = "allow"
    permissions[f"{Path(tempfile.gettempdir()).resolve()}/**"] = "allow"
    permissions[f"{(Path.home() / '.m2').resolve()}/**"] = "allow"

    if EXTERNAL_DIRS_CONFIG.exists():
        data = json.loads(EXTERNAL_DIRS_CONFIG.read_text(encoding="utf-8"))
        for pattern in data.get("allow", []):
            permissions[str(pattern)] = "allow"

    configured_dirs = _path_list(settings.opencode_external_dirs or settings.index_source_dirs)
    for path_value in configured_dirs:
        permissions[_permission_pattern(path_value)] = "allow"

    return permissions


def _load_json_file(path: Path) -> dict:
    if not path.exists():
        return {"$schema": "https://opencode.ai/config.json"}
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_global_cursor_bootstrap() -> Path:
    config = _load_json_file(GLOBAL_OPENCODE_CONFIG)
    changed = False

    plugins = config.get("plugin")
    if not isinstance(plugins, list):
        plugins = [] if plugins is None else [plugins]
        config["plugin"] = plugins
        changed = True
    if CURSOR_PLUGIN_NAME not in plugins:
        plugins.append(CURSOR_PLUGIN_NAME)
        changed = True

    provider = config.setdefault("provider", {})
    cursor_provider = provider.get("cursor")
    if not isinstance(cursor_provider, dict):
        provider["cursor"] = {"name": "Cursor"}
        changed = True
    else:
        if cursor_provider.get("name") != "Cursor":
            cursor_provider.setdefault("name", "Cursor")
            changed = True

    if changed:
        GLOBAL_OPENCODE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        GLOBAL_OPENCODE_CONFIG.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return GLOBAL_OPENCODE_CONFIG


def _ensure_cursor_plugin_cache() -> Optional[Path]:
    global_plugin = GLOBAL_OPENCODE_PLUGIN_ROOT / "node_modules" / CURSOR_PLUGIN_NAME
    if not global_plugin.exists():
        return None

    cache_root = OPENCODE_PLUGIN_CACHE_ROOT / f"{CURSOR_PLUGIN_NAME}@latest"
    node_modules_dir = cache_root / "node_modules"
    node_modules_dir.mkdir(parents=True, exist_ok=True)

    cache_package_json = cache_root / "package.json"
    if not cache_package_json.exists():
        cache_package_json.write_text(
            json.dumps({"name": f"{CURSOR_PLUGIN_NAME}@latest-private", "private": True}, indent=2),
            encoding="utf-8",
        )

    node_modules_package_json = node_modules_dir / "package.json"
    if not node_modules_package_json.exists():
        node_modules_package_json.write_text(json.dumps({"private": True}, indent=2), encoding="utf-8")

    plugin_entry = node_modules_dir / CURSOR_PLUGIN_NAME
    if plugin_entry.is_symlink():
        if plugin_entry.resolve() != global_plugin.resolve():
            plugin_entry.unlink()
            plugin_entry.symlink_to(global_plugin, target_is_directory=True)
    elif plugin_entry.exists():
        # Leave a real install in place if OpenCode populated it successfully.
        pass
    else:
        plugin_entry.symlink_to(global_plugin, target_is_directory=True)

    return cache_root


def generate_opencode_config(repo_path: str):
    """Write a project-level opencode.json with the provider-chain model config.

    Registers provider/model metadata for all configured chain candidates so
    OpenCode can resolve provider-specific IDs such as `openrouter/z-ai/glm-5.1`.
    """
    chain = parse_provider_chain(settings.opencode_provider_chain)
    selected = available_provider_candidates(fallback_enabled=False)
    if not selected:
        selected = provider_candidates(fallback_enabled=False)
    chain_models = {candidate.model for candidate in chain}
    if settings.opencode_model in chain_models:
        model = settings.opencode_model
    elif selected:
        model = selected[0].model
    else:
        model = settings.opencode_model
    small_model = model

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": model,
        "small_model": small_model,
        "permission": {
            "external_directory": _external_directory_permissions(repo_path),
        },
    }

    provider_models: dict[str, list[str]] = {}
    if chain:
        for candidate in chain:
            provider_models.setdefault(candidate.provider, []).append(candidate.model)
    else:
        for full_model in (model, settings.opencode_small_model):
            if "/" in full_model:
                provider_id = full_model.split("/", 1)[0]
            else:
                provider_id = settings.opencode_provider
            if provider_id:
                provider_models.setdefault(provider_id, []).append(full_model)
    providers = set(provider_models)
    if "cursor" in providers:
        _ensure_global_cursor_bootstrap()
        _ensure_cursor_plugin_cache()
        config["plugin"] = [CURSOR_PLUGIN_NAME]
    for provider_id in sorted(providers):
        _register_provider_models(config, provider_id, *provider_models[provider_id])

    config_path = Path(repo_path) / "opencode.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return config_path
