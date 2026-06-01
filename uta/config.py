from typing import Optional
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UTA_", env_file=".env", extra="ignore")

    # OpenCode Server Config
    opencode_port: int = 4096
    opencode_host: str = "127.0.0.1"
    opencode_provider: str = "openai"
    opencode_model: str = "openai/gpt-4o"
    opencode_small_model: str = "openai/gpt-4o-mini"
    # Ordered provider/model chain used by the OpenCode fallback router.
    # Format: "provider:model-a,model-b;other:other/model". When fallback is
    # disabled, the first valid chain candidate remains the active model.
    opencode_provider_chain: str = "openai:openai/gpt-4o"
    # Provider token mapping for the chain. Values are semicolon-separated
    # provider token entries, e.g. "openai.token=...;deepseek.token=...".
    opencode_provider_tokens: str = ""
    # Provider base URL mapping for OpenAI-compatible providers. Values are
    # semicolon-separated provider URL entries, e.g.
    # "openai.base_url=https://api.openai.com/v1;deepseek.base_url=https://api.deepseek.com/v1".
    opencode_provider_base_urls: str = ""
    opencode_provider_fallback_enabled: bool = False
    opencode_model_api_timeout_seconds: int = 5
    opencode_model_api_cache_seconds: int = 300
    # Optional OpenCode model variant. For OpenAI reasoning models this maps to
    # reasoning effort through OpenCode, e.g. "none", "minimal", "low".
    opencode_variant: str = ""
    # Keep UTA runs isolated from user/global OpenCode plugins. External plugins
    # can inject broad search modes or background-agent behavior that conflicts
    # with deterministic batch repair.
    opencode_pure: bool = True
    # Cheap-tier model for deterministic sub-tasks. Disabled by default because
    # recent real runs showed quality regressions when lower-tier models handled
    # coverage/mutation repair. Set explicitly to opt in.
    opencode_cheap_model: str = ""
    # Optional prompt add-ons for generation. Disabled by default because the
    # extra prompt bulk did not reduce read/grep exploration in real runs.
    inject_stub_catalog_in_generation: bool = False
    inject_test_skeleton_in_generation: bool = False
    # Additional source-base directories for the tree-sitter index query CLI.
    # Comma-separated roots are discovered recursively for src/main/java modules.
    index_source_dirs: str = ""
    # Additional external directories OpenCode may read in headless mode.
    # If unset, UTA also allows the configured index_source_dirs.
    opencode_external_dirs: str = ""
    # Allow query-index to fetch dependency source jars through Maven when the
    # class is not available locally in the repo or configured source roots.
    index_fetch_sources: bool = True
    # Optional Maven settings.xml override used by query-index fallback.
    maven_settings_path: Optional[str] = None
    # Maven executable path. Production daemons may not inherit login-shell PATH.
    maven_bin: str = "mvn"
    # CI-plugin workspace and check-only enforcement command.
    # See docs/test-enforce-usage.md for the embedded usage guide.
    ci_workspace_root: str = "~/.local/share/uta/ci-data/workspaces"
    ci_enforcement_command: str = (
        "mvn -U -DskipTests=false -Dmaven.test.skip=false -Dtest.enforcement.enabled=true "
        "-Dmaven.test.failure.ignore=true -Dsurefire.timeout=900 verify"
    )
    ci_enforcement_timeout_seconds: int = 1800
    ci_python_enforcement_command: str = "uta python-enforce"
    ci_python_enforcement_timeout_seconds: int = 1800
    ci_context_runtime_root: str = "~/.local/share/uta/ci-data/context"
    ci_record_store_root: str = "~/.local/share/uta/ci-data/records"
    ci_task_db_path: Optional[str] = None
    ci_public_base_url: str = ""
    ci_allowed_git_hosts: str = "git.example.com"
    ci_git_user_name: str = "UTA Bot"
    ci_git_user_email: str = "unit-test-agent@example.com"
    ci_git_ssh_key_path: str = ""
    ci_git_command_timeout_seconds: int = 600
    ci_git_command_retry_times: int = 1
    ci_git_command_retry_delay_seconds: float = 2.0

    # GitHub webhook protocol (open-source CI integration). Dormant until an App
    # is configured: the webhook route accepts requests but reports nothing.
    # Checks API requires a GitHub App (a PAT cannot create check runs), so result
    # reporting mints an installation token from the App id + private key.
    github_api_base_url: str = "https://api.github.com"
    github_app_id: str = Field(default="", alias="GITHUB_APP_ID")
    github_app_private_key_path: str = Field(default="", alias="GITHUB_APP_PRIVATE_KEY_PATH")
    github_webhook_secret: str = Field(default="", alias="GITHUB_WEBHOOK_SECRET")
    github_check_name: str = "uta/unit-test-enforcement"
    github_callback_timeout_seconds: int = 10
    github_callback_retry_times: int = 3

    # Gemini API key (read directly as GEMINI_API_KEY, no prefix)
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")
    # OpenRouter API key (read directly, no UTA_ prefix)
    openrouter_api_key: Optional[str] = Field(default=None, alias="OPENROUTER_API_KEY")
    # Optional OpenRouter provider routing preferences. Values are comma-separated
    # provider slugs, for example "moonshotai" or "cloudflare,moonshotai".
    openrouter_provider_only: str = ""
    openrouter_provider_order: str = ""
    openrouter_allow_fallbacks: bool = True
    openrouter_require_parameters: bool = False
    # DeepSeek API key (read directly, no UTA_ prefix). Accept the legacy
    # DEEPSEEK_KEY name too because some local shells still export that form.
    deepseek_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("DEEPSEEK_API_KEY", "DEEPSEEK_KEY"),
    )
    # Tencent TokenHub API key (read directly, no UTA_ prefix)
    tencent_api_key: Optional[str] = Field(default=None, alias="TENCENT_API_KEY")
    # Optional Tencent TokenHub base URL override. Defaults to the domestic endpoint.
    tencent_base_url: Optional[str] = Field(default=None, alias="TENCENT_BASE_URL")
    # Optional Ollama host override for local/self-hosted Ollama endpoints.
    ollama_host: Optional[str] = Field(default=None, alias="OLLAMA_HOST")
    # Requested Ollama context window (`num_ctx`) for generated OpenCode model config.
    ollama_num_ctx: int = Field(default=262144, alias="OLLAMA_NUM_CTX")
    # OpenAI-compatible custom endpoint. When openai_base_url is set the "openai"
    # provider is registered as @ai-sdk/openai-compatible with this base URL.
    # Uses UTA_OPENAI_KEY / UTA_OPENAI_API_KEY to avoid clashing with a system-level
    # OPENAI_API_KEY that points at the real OpenAI API.
    openai_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("UTA_OPENAI_API_KEY", "UTA_OPENAI_KEY", "OPENAI_KEY"),
    )
    openai_base_url: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("UTA_BASE_URL", "UTA_BAS_URL", "OPENAI_BASE_URL"),
    )

    # Default Quality Gates
    coverage_gate: int = 80
    mutation_gate: int = 70
    ci_diff_coverage_gate: int = 95
    ci_diff_mutation_gate: int = 100

    # Coverage ROI: method-level effort scoring for smarter test prioritization
    roi_enabled: bool = True
    roi_skip_all_expensive: bool = False
    roi_debug: bool = False

    # Mutation ROI: rank surviving mutant families by (killability × count) / effort
    mutation_roi_enabled: bool = True
    mutation_roi_skip_expensive: bool = False
    # Maximum PIT evaluation attempts per class, including the initial PIT run.
    # Each failed attempt before the last can trigger focused mutation-fix turns.
    mutation_enhancement_attempts: int = 5
    # Python batch generation repair rounds after the initial generated test
    # fails compile/test, coverage, or mutation verification.
    python_repair_max_attempts: int = 2

    # Git Scanner Defaults
    default_days: int = 30
    default_max_files: int = 10

    # How many candidate classes to handle in one OpenCode session (one generation prompt).
    # Larger values reduce session startup and context-export overhead; very large batches
    # may hit model context limits or produce lower-quality tests per class.
    classes_per_agent_run: int = 1
    # In production task mode, automatically batch small classes even when the
    # generic batch size is left at 1. Complex classes still run alone.
    smart_batching_enabled: bool = True
    smart_simple_batch_size: int = 3
    smart_complex_line_threshold: int = 100
    smart_complex_public_method_threshold: int = 4

    # Path to OpenCode binary (optional, if not in PATH)
    opencode_bin: Optional[str] = None

    # Custom spawn command for `opencode run`. JSON-encoded list, e.g.
    # '["bun", "run", "src/index.ts", "run"]'. None → uses opencode binary.
    opencode_spawn_cmd: Optional[str] = None

    # Custom spawn command for `opencode serve`. JSON-encoded list, e.g.
    # '["bun", "run", "--cwd", "/path/to/opencode/packages/opencode", "./src/index.ts", "serve"]'.
    # None → uses opencode binary.
    opencode_serve_cmd: Optional[str] = None

    # When set, passes --attach <url> instead of spawning a new process (dev mode).
    # e.g. "http://localhost:4096" or "http://[::1]:4096"
    opencode_attach_url: Optional[str] = None

    # Optional non-interactive shell command run after successful baseline compile when
    # `.uta_summary.md` is still missing (after OpenCode ``/init``). Fallback only.
    opencode_init_command: Optional[str] = None

    # Whether to run OpenCode init bootstrap for project summary generation.
    opencode_init_slash_enabled: bool = True

    # Max seconds to wait for OpenCode ``/init`` slash command (project summary bootstrap).
    opencode_init_slash_timeout: int = 300

    # Emit incremental agent progress while polling session completion.
    opencode_stream_progress: bool = True

    # Preserve focused OpenCode repair sessions (coverage/mutation) after runs so
    # post-run token assessment can compare split-session workflows accurately.
    opencode_preserve_focused_sessions: bool = True

    # When enabled, start OpenCode with --log-level DEBUG.
    opencode_server_debug: bool = False

    # When enabled, start OpenCode with --print-logs and inherit stdout/stderr
    # so OpenCode internals are emitted by the server.
    opencode_server_print_logs: bool = False

    # When enabled, persist OpenCode server stdout/stderr to a temp log file.
    opencode_server_log_to_file: bool = True

    # Multiplier applied to the base generation timeout (40 min per class).
    # Default 1.0 gives 40 minutes per single-class generation run.
    # Increase above 1.0 for slower models/repos; decrease below 1.0 to fail fast.
    opencode_generation_timeout_ratio: float = 1.0

    # Extra multiplier for slower DeepSeek turns across planning/generation/repair.
    # Default 2.0 keeps the normal model budgets unchanged while giving
    # deepseek/* runs twice as long before UTA classifies them as timed out.
    opencode_deepseek_timeout_multiplier: float = 2.0

    # Generic multiplier applied to all OpenCode LLM turn timeouts.
    # Keep at 1.0 by default; raise for slower providers/models during benchmark runs.
    opencode_timeout_multiplier: float = 1.0

    # Process-mode OpenCode emits JSONL events while the provider is still
    # reasoning. Treat recent stream activity as liveness, but still keep an
    # absolute cap so a pathological turn cannot run forever.
    opencode_active_timeout_multiplier: float = 2.0

    # Max silence between OpenCode JSONL events before the turn is considered
    # dead. This is intentionally longer than old no-progress thresholds because
    # GPT-5.5 and Kimi can spend several minutes in hidden reasoning.
    opencode_stream_idle_timeout_seconds: int = 900

    # Server/polling mode uses session part updates rather than process stdout.
    # Keep the stalled-session threshold aligned with process-mode idle handling.
    opencode_stalled_no_progress_seconds: int = 900

    # Max seconds to wait for the planning/replan turn before UTA gives up on
    # receiving an approved plan and continues without one.
    opencode_planning_timeout_seconds: int = 600

    # Max seconds to wait for compile-fix turns. Generation/test/coverage/
    # mutation repair turns use the broader repair budget below.
    opencode_compile_fix_timeout_seconds: int = 600

    # Max seconds to wait for LLM repair turns after generation. These turns can
    # include long reasoning over compile/test/coverage/mutation diagnostics.
    opencode_repair_timeout_seconds: int = 900

    # Max seconds to wait for OpenCode native provider `/connect` flows triggered by UTA.
    opencode_connect_timeout: int = 300

    # Preserve `.uta_cache`, `.uta_summary.md`, and related artifacts after E2E pytest runs
    # so failed runs can be inspected later.
    e2e_keep_artifacts: bool = True

    # Root directory for cloned repos. Set via UTA_CLONE_ROOT.
    clone_root: str = "~/.local/share/uta/code"

    # Number of consecutive repo-task failures before marking POISONED.
    quarantine_threshold: int = 2

    # Maximum concurrent repo tasks in the daemon worker pool.
    max_parallel_repos: int = 1

    # Optional global batch cost cap in USD. Daemon stops dequeueing when exceeded.
    batch_cap_usd: Optional[float] = None

settings = Settings()
