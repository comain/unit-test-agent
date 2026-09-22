from typing import Any, Dict, Optional
from pydantic import AliasChoices, Field
from pydantic_settings import SettingsConfigDict

from agent_core.config import HarnessConfig, set_default_config


class Settings(HarnessConfig):
    """UTA's settings, which are also the shared harness's settings.

    Subclassing rather than holding a second object beside it. The harness
    reads 44 settings; 39 of them were already declared here, under this same
    ``UTA_`` prefix, so two models would have loaded the same environment into
    two instances that agree at startup and then silently diverge the moment
    anything sets one at runtime -- which the suite does constantly, and which
    a daemon does when it retunes a provider mid-run.

    Declarations below override the inherited ones, so UTA's existing types
    and defaults continue to decide behaviour. The five the harness adds
    (a permission policy, the external-directory switches) are inherited with
    agent-core's defaults, which were chosen to reproduce what this project
    did before those settings existed.
    """

    model_config = SettingsConfigDict(env_prefix="UTA_", env_file=".env", extra="ignore")

    # Agent implementation selected by agent-core's harness registry. Product
    # workflow code consumes only the neutral harness/session API.
    agent_harness: str = "opencode"
    # Trusted host inputs forwarded opaquely; core validates/resolves on execution.
    model_selection_config: str = Field(
        default="", validation_alias="AGENT_MODEL_SELECTION_CONFIG"
    )
    model_coding_index_min: Optional[str] = Field(
        default=None, validation_alias="AGENT_MODEL_CODING_INDEX_MIN"
    )
    # Neutral harness settings (Task 23 / ADR-013)
    harness_options: str = Field(default="", validation_alias=AliasChoices("UTA_HARNESS_OPTIONS", "HARNESS_OPTIONS"))
    harness_model: str = Field(default="", validation_alias=AliasChoices("UTA_HARNESS_MODEL", "HARNESS_MODEL"))
    harness_small_model: str = Field(default="", validation_alias=AliasChoices("UTA_HARNESS_SMALL_MODEL", "HARNESS_SMALL_MODEL"))
    harness_cheap_model: str = Field(default="", validation_alias=AliasChoices("UTA_HARNESS_CHEAP_MODEL", "HARNESS_CHEAP_MODEL"))
    harness_host: str = Field(default="", validation_alias=AliasChoices("UTA_HARNESS_HOST", "HARNESS_HOST"))
    harness_port: Optional[int] = Field(default=None, validation_alias=AliasChoices("UTA_HARNESS_PORT", "HARNESS_PORT"))
    harness_auth_probe_enabled: Optional[bool] = Field(default=None, validation_alias=AliasChoices("UTA_HARNESS_AUTH_PROBE_ENABLED", "HARNESS_AUTH_PROBE_ENABLED"))
    harness_timeout_seconds: Optional[int] = Field(default=None, validation_alias=AliasChoices("UTA_HARNESS_TIMEOUT_SECONDS", "HARNESS_TIMEOUT_SECONDS"))


    # OpenCode Server Config
    opencode_port: int = 4096
    opencode_host: str = "127.0.0.1"
    opencode_provider: str = "token-pool"
    opencode_model: str = "token-pool/gpt-5.5"
    opencode_small_model: str = "token-pool/gpt-5.5"
    # Ordered provider/model chain used by the OpenCode fallback router.
    # Format: "provider:model-a,model-b;other:other/model". When fallback is
    # disabled, the first valid chain candidate remains the active model.
    opencode_provider_chain: str = "token-pool:token-pool/gpt-5.5"
    # Provider token mapping for the chain. Values are semicolon-separated
    # provider token entries, e.g. "openai.token=...;deepseek.token=...".
    opencode_provider_tokens: str = ""
    # Provider base URL mapping for OpenAI-compatible providers. Values are
    # semicolon-separated provider URL entries, e.g.
    # "token-pool.base_url=https://proxy/v1;deepseek.base_url=https://api.deepseek.com/v1".
    opencode_provider_base_urls: str = ""
    opencode_provider_fallback_enabled: bool = False
    # Startup readiness probe for OpenAI-style providers. Disable only when
    # diagnosing OpenCode process startup, not as a replacement for turn-level
    # timeout/fallback handling.
    opencode_auth_probe_enabled: bool = True
    # Optional XDG data home for OpenCode subprocesses. Useful when the shared
    # OpenCode DB/snapshot store becomes stale or corrupt for a workspace.
    opencode_data_home: str = ""
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
    index_source_dirs: str = "~/wms/api,~/platform/api,~/tms/api,~/finance/api,~/md/api"
    # Additional external directories OpenCode may read in headless mode.
    # If unset, UTA also allows the configured index_source_dirs.
    opencode_external_dirs: str = ""
    # Allow query-index to fetch dependency source jars through Maven when the
    # class is not available locally in the repo or configured source roots.
    index_fetch_sources: bool = True
    # Optional Maven settings.xml override used by query-index fallback.
    maven_settings_path: Optional[str] = None
    # Optional Maven Central mirror used by Java enforcement. This is supplied
    # as a credential-free global settings overlay, so user settings keep their
    # existing local repository and private-repository credentials.
    maven_central_mirror_url: str = ""
    # Maven executable path. Production daemons may not inherit login-shell PATH.
    maven_bin: str = "mvn"
    # Java runtime for task daemon workers. Prefer the deployed JDK8 even when
    # the parent process has a newer JAVA_HOME.
    daemon_java_home: str = "/opt/app/jdks/jdk8"
    # Exact repository identity -> Java home. Repositories absent from this
    # mapping keep using daemon_java_home, so adding one repository cannot
    # change the runtime of any other.
    repository_java_homes: Dict[str, str] = Field(default_factory=dict)
    # Exact CI appName -> trusted Python test-environment recipe. Recipes are
    # resolved by the Python handler and snapshotted onto repair tasks so CI,
    # repair, and the final rerun cannot drift to different interpreters.
    python_environment_recipes: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # RDC API trigger workspace and check-only enforcement command.
    # See docs/test-enforce-usage.md for the embedded UTA usage guide.
    ci_workspace_root: str = "/data/w/uta-ci-data/workspaces"
    ci_enforcement_command: str = (
        "mvn -U -DskipTests=false -Dmaven.test.skip=false -Dtest.enforcement.enabled=true "
        "-Dmaven.test.failure.ignore=true -Dsurefire.timeout=900 verify"
    )
    ci_enforcement_timeout_seconds: int = 1800
    # When on, an enforcement run whose failing tests are unrelated to the diff
    # is taken again with those tests excluded from both the JaCoCo and the PIT
    # stage. Off means today's behaviour exactly: one run, nothing extra.
    ci_failing_test_exclusion_enabled: bool = False
    ci_enforcement_stall_detection_enabled: bool = True
    ci_enforcement_stall_seconds: int = Field(default=120, gt=0, le=1800)
    ci_enforcement_stall_retries: int = Field(default=1, ge=0, le=3)
    ci_hanging_test_quarantine_enabled: bool = True
    ci_hanging_test_quarantine_ttl_days: int = Field(default=14, gt=0, le=365)
    ci_python_enforcement_command: str = "uta python-enforce"
    ci_python_enforcement_timeout_seconds: int = 1800
    ci_python_enforcement_memory_limit_mb: int = 3072
    # How long a repair session may sit in `rerun_running` before the rerun is
    # assumed dead and the session is allowed to retry. The state is written
    # before a rerun that can take a full enforcement build, and is only
    # cleared when that call returns, so an interruption used to strand the
    # session permanently. Comfortably above `ci_enforcement_timeout_seconds`
    # so a slow-but-live rerun is never mistaken for a dead one. 0 disables.
    ci_repair_rerun_stale_seconds: int = Field(
        default=5400,
        validation_alias=AliasChoices("UTA_CI_REPAIR_RERUN_STALE_SECONDS"),
    )
    # Equivalent-mutant review: a CI repair that would stop on mutation
    # no-progress first asks one agent turn to judge the survivors. Above this
    # many reviewable survivors the review is skipped -- a large set is more
    # likely a weak suite than a batch of equivalent mutants.
    ci_equivalent_mutant_review_max: int = 30
    ci_equivalent_mutant_review_timeout_seconds: int = 1800
    # The same ceiling for the generation/repair lane, which builds its own
    # runners and so never saw the CI one. It runs model-written tests, which
    # is exactly where an unbounded allocation comes from; on a node with no
    # swap the kernel's answer is to OOM-kill an unrelated process. Matches the
    # CI limit so a test that passes verification cannot fail enforcement on
    # memory alone. 0 disables the bound.
    python_verification_memory_limit_mb: int = Field(
        default=3072,
        validation_alias=AliasChoices("UTA_PYTHON_VERIFICATION_MEMORY_LIMIT_MB"),
    )
    ci_report_parallel_limit: int = 1
    rdc_ack_url: str = "http://127.0.0.1/rdc/plugin/ack"
    ci_callback_timeout_seconds: int = 10
    ci_callback_retry_times: int = 3
    ci_context_runtime_root: str = "/data/w/uta-ci-data/context"
    ci_record_store_root: str = "/data/w/uta-ci-data/records"
    ci_inflight_dir: str = ""
    ci_task_db_path: Optional[str] = None
    ci_public_base_url: str = ""
    ci_allowed_git_hosts: str = "git.example.com"
    jira_raw_url: str = "https://jira.example.com/jira/wcr/jira/issues/raw/v2"
    ci_git_user_name: str = "UTA Unit Test Agent"
    ci_git_user_email: str = "unit-test-agent@example.com"
    ci_git_ssh_key_path: str = ""
    ci_git_access_token: str = Field(
        default="",
        validation_alias=AliasChoices("GIT_AC", "UTA_CI_GIT_ACCESS_TOKEN"),
    )
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
    ci_python_diff_mutation_gate: int = 95

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
    # fails compile or test verification. Coverage and mutation repair are not
    # bounded by this: they retry while their score improves (see below).
    python_repair_max_attempts: int = 2
    # Safety ceilings for the score-driven repair loops. These are not the
    # expected number of rounds -- coverage and mutation repair stop at the
    # first round that does not improve the score -- they only bound a score
    # that creeps up forever without reaching its gate.
    coverage_repair_max_attempts: int = 6
    mutation_repair_max_attempts: int = 6
    # Survivor groups a *later* mutation-repair round targets, in either
    # language. Round one always shows the full ROI-ranked survivor map; the
    # rounds after it are cleanup passes, and narrowing them is what makes a
    # second round differ from the first.
    mutation_repair_groups_per_round: int = 6
    # Repo-level cost guard multiplier. Python mutation can spend substantial
    # time/cost after coverage is already green, so keep the hard cap above the
    # estimator while still bounding runaway tasks.
    budget_hard_cap_multiplier: float = 4.0
    # Per-target guard multiplier. This is separate from the repo cap so one
    # unusually hard target cannot consume the whole repair budget silently.
    class_budget_hard_cap_multiplier: float = 6.0
    # Python mutation repair context controls. Large survivor sets are split
    # into symbol/function-sized repair rounds to avoid broad "fix all" prompts.
    python_mutation_repair_split_threshold: int = 50
    python_mutation_repair_batch_max_survivors: int = 60
    python_mutation_repair_exact_diff_limit: int = 60
    python_mutant_diff_limit_per_symbol: int = 3
    python_mutant_show_timeout_seconds: int = 10
    python_mutant_show_total_timeout_seconds: int = 30
    python_mutant_verify_show_timeout_seconds: int = 10
    python_mutant_verify_show_total_timeout_seconds: int = 30
    python_mutant_verify_show_max_calls: int = 20
    python_mutation_candidate_plan_enabled: bool = True
    python_mutation_adapter_generation_timeout_seconds: int = 120
    python_mutation_selected_execution_timeout_seconds: int = 7200
    python_mutation_per_mutant_timeout_seconds: int = 120
    python_mutation_generation_max_source_bytes: int = 5_000_000
    python_mutation_generation_max_changed_lines: int = 1000
    # A file with this many changed lines is a new or rewritten module, not an
    # incremental change, and the caps above only trim its mutants -- the
    # generation pass still runs and can consume the whole report's budget (one
    # 6,997-line file spent 12 of 30 minutes before the gate timed out). Such a
    # target is skipped with a report warning instead, so the run still reports
    # on every file it can actually verify. 0 disables the skip.
    python_enforcement_max_changed_lines_per_file: int = Field(
        default=2000,
        validation_alias=AliasChoices("UTA_PYTHON_ENFORCEMENT_MAX_CHANGED_LINES_PER_FILE"),
    )
    python_mutation_generation_max_opportunities: int = 3000
    python_mutation_generation_max_selected: int = 1000
    # Generation strategy for the full cap profile (see ADR-002):
    #   "batch"    — default: function-granular byte-budgeted batched generation.
    #   "hard_cap" — rollback behavior: drop selected mutants beyond max_selected.
    python_mutation_generation_strategy: str = "batch"
    # Per-batch generated-module byte budget in "batch" mode. Derived from the
    # parse-cost curve in design-python-mutation-scalability.md Appendix A 13.7
    # (8 MB parses in ~1-2s; the tokenizer stays in its linear regime).
    python_mutation_generation_max_generated_bytes: int = 8_000_000
    # Max batches per target in "batch" mode. Bounds total wall time:
    # max_batches * adapter_generation_timeout must stay within the overall
    # verification timeout. Excess is trimmed by priority (omittedByMaxBatches).
    python_mutation_generation_max_batches: int = 12
    # Calibration for the a-priori generated-bytes estimate: mutmut emits ~N mutants
    # per selected line (not one), so the per-line copy estimate is scaled by this
    # factor. Empirically ~2x on the node2 reference target (design Appendix A 13.8 / T8);
    # keeps actual generated modules under the byte budget rather than ~2x over it.
    python_mutation_generation_bytes_per_line_factor: float = 2.0
    # CI uses a smaller deterministic generation cap; repair/full verification
    # uses the larger caps above. These are hard cuts, not sampling.
    python_mutation_generation_ci_max_changed_lines: int = Field(
        default=300,
        validation_alias=AliasChoices(
            "UTA_PYTHON_MUTATION_GENERATION_CI_MAX_CHANGED_LINES",
            "UTA_PYTHON_MUTATION_SAMPLE_LINE_THRESHOLD",
            "UTA_PYTHON_MUTATION_CANDIDATE_SAMPLE_THRESHOLD",
        ),
    )
    python_mutation_generation_ci_max_opportunities: int = 1000
    python_mutation_generation_ci_max_selected: int = Field(
        default=300,
        validation_alias=AliasChoices(
            "UTA_PYTHON_MUTATION_GENERATION_CI_MAX_SELECTED",
            "UTA_PYTHON_MUTATION_SAMPLE_LINE_LIMIT",
            "UTA_PYTHON_MUTATION_CANDIDATE_SAMPLE_LIMIT",
        ),
    )
    python_mutation_max_children: int = 2

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
    # Durable generation workflow state is operator-auditable but bounded.
    workflow_checkpoint_retention_days: int = 30
    workflow_progress_event_retention_days: int = 30
    workflow_retention_interval_seconds: int = 6 * 60 * 60

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

    # Persist per-turn raw OpenCode stdout/stderr lines as JSONL under the
    # target repo. This is diagnostic evidence for no-output/model-provider
    # stalls and is intentionally local to the workspace cache.
    opencode_turn_log_enabled: bool = True
    opencode_turn_log_dir: str = ".uta_cache/opencode_turns"

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

    # Deprecated in favour of agent-core's `opencode_provider_timeout_multipliers`,
    # which expresses the same thing as data for any provider rather than
    # naming one vendor in this project's settings. Still read, and still
    # honoured, because deployed nodes set it: `model_post_init` folds it into
    # the general form. Remove once no environment carries it.
    opencode_deepseek_timeout_multiplier: float = 2.0

    # Generic multiplier applied to all OpenCode LLM turn timeouts.
    # Keep at 1.0 by default; raise for slower providers/models during benchmark runs.
    opencode_timeout_multiplier: float = 1.0

    # Process-mode OpenCode emits JSONL events while the provider is still
    # reasoning. Treat recent stream activity as liveness, but still keep an
    # absolute cap so a pathological turn cannot run forever.
    opencode_active_timeout_multiplier: float = 2.0

    # A session shell or provider retry log is not useful model output. Bound
    # that startup state separately from the full repair budget so fallback can
    # advance promptly when a model never begins the turn.
    opencode_initial_output_timeout_seconds: int = 180

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

    # Root directory for cloned repos. Set via UTA_CLONE_ROOT; default matches prod node layout.
    clone_root: str = "/data/w/code"

    # Number of consecutive repo-task failures before marking POISONED.
    quarantine_threshold: int = 2

    # Maximum concurrent repo tasks in the daemon worker pool.
    max_parallel_repos: int = 1

    # Requeue RUNNING repo tasks whose owning daemon heartbeat has disappeared.
    # The task subprocesses are supervised by the daemon, so a stale daemon
    # heartbeat means the DB can otherwise keep an orphaned RUNNING task forever.
    task_runner_stale_heartbeat_seconds: int = 120

    # Optional global batch cost cap in USD. Daemon stops dequeueing when exceeded.
    batch_cap_usd: Optional[float] = None

    #: Settings whose agent-core default would point a deployed node at a path
    #: it has never written. Applied here rather than as field defaults so the
    #: reason stays attached to them: they preserve what is already on disk,
    #: they do not describe anything about how this project works.
    _DEPLOYED_PATHS = {
        # 63 sites still read `.uta_cache`, and nodes have caches there.
        "agent_cache_dir": ".uta_cache",
        "opencode_turn_log_dir": ".uta_cache/opencode_turns",
        # `uta/app/cli.py` writes agent run logs here and the harness reads them
        # back looking for a provider's 429. Letting the two names diverge
        # loses the evidence silently: instead of backing off, the run walks
        # the whole provider chain, marking each model unhealthy on the way.
        "agent_debug_log_dir": "uta-run-logs",
    }

    def model_post_init(self, __context) -> None:
        # `model_fields_set` is what separates a value the environment supplied
        # from one inherited from agent-core. Testing for emptiness would not:
        # the inherited defaults are non-empty, so the neutral `.agent_cache`
        # would win every time.
        configured = self.model_fields_set
        for name, value in self._DEPLOYED_PATHS.items():
            if name not in configured:
                setattr(self, name, value)

        # Fold the deprecated per-vendor multiplier into the general,
        # provider-keyed setting agent-core reads. Only when the general one
        # was not configured explicitly -- if an operator has written that,
        # they mean it, and silently appending to it would be worse than
        # ignoring the old key.
        if "opencode_provider_timeout_multipliers" not in configured:
            legacy = float(self.opencode_deepseek_timeout_multiplier or 1.0)
            if legacy > 0 and legacy != 1.0:
                self.opencode_provider_timeout_multipliers = f"deepseek={legacy}"


settings = Settings()

# The harness resolves every read through agent-core's active configuration.
# Installing this instance is what makes `uta.shared.config.settings` and the harness
# one object rather than two copies of the same environment. Done at import so
# no entry point has to remember, and so a setting read at another module's
# import time already resolves correctly.
set_default_config(settings)
