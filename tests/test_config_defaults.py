from uta.shared.config import Settings


def test_repository_java_homes_default_empty_and_parse_json_env(monkeypatch):
    monkeypatch.delenv("UTA_REPOSITORY_JAVA_HOMES", raising=False)
    assert Settings(_env_file=None).repository_java_homes == {}

    monkeypatch.setenv(
        "UTA_REPOSITORY_JAVA_HOMES",
        '{"fd_wmonitor_default_store":"/opt/app/jdks/jdk25"}',
    )

    assert Settings(_env_file=None).repository_java_homes == {
        "fd_wmonitor_default_store": "/opt/app/jdks/jdk25"
    }


def test_mutation_repair_groups_per_round_default_is_six():
    settings = Settings(_env_file=None)

    assert settings.mutation_repair_groups_per_round == 6


def test_python_ci_enforcement_timeout_defaults_to_thirty_minutes():
    settings = Settings(_env_file=None)

    assert settings.ci_python_enforcement_timeout_seconds == 1800


def test_java_hanging_test_quarantine_defaults_on():
    settings = Settings(_env_file=None)

    assert settings.ci_enforcement_stall_detection_enabled is True
    assert settings.ci_enforcement_stall_seconds == 120
    assert settings.ci_enforcement_stall_retries == 1
    assert settings.ci_hanging_test_quarantine_enabled is True
    assert settings.ci_hanging_test_quarantine_ttl_days == 14


def test_budget_hard_cap_defaults_are_doubled_for_expensive_mutation():
    settings = Settings(_env_file=None)

    assert settings.budget_hard_cap_multiplier == 4.0
    assert settings.class_budget_hard_cap_multiplier == 6.0


def test_python_mutation_candidate_plan_defaults_and_aliases(monkeypatch):
    settings = Settings(_env_file=None)

    assert settings.python_mutation_candidate_plan_enabled is True
    assert settings.python_mutation_max_children == 2
    assert settings.python_mutation_adapter_generation_timeout_seconds == 120
    assert settings.python_mutation_selected_execution_timeout_seconds == 7200
    assert settings.python_mutation_per_mutant_timeout_seconds == 120
    assert settings.python_mutation_generation_max_source_bytes == 5_000_000
    assert settings.python_mutation_generation_max_changed_lines == 1000
    assert settings.python_mutation_generation_max_opportunities == 3000
    assert settings.python_mutation_generation_max_selected == 1000
    assert settings.python_mutation_generation_ci_max_changed_lines == 300
    assert settings.python_mutation_generation_ci_max_opportunities == 1000
    assert settings.python_mutation_generation_ci_max_selected == 300

    monkeypatch.setenv("UTA_PYTHON_MUTATION_SAMPLE_LINE_THRESHOLD", "123")
    monkeypatch.setenv("UTA_PYTHON_MUTATION_SAMPLE_LINE_LIMIT", "45")

    aliased = Settings(_env_file=None)

    assert aliased.python_mutation_generation_ci_max_changed_lines == 123
    assert aliased.python_mutation_generation_ci_max_selected == 45


def test_python_mutation_batched_generation_defaults_and_env(monkeypatch):
    settings = Settings(_env_file=None)

    # Default strategy uses verified batched generation; hard_cap remains the rollback override.
    assert settings.python_mutation_generation_strategy == "batch"
    # Byte budget derived from the parse-cost curve (Appendix A 13.7): 8 MB.
    assert settings.python_mutation_generation_max_generated_bytes == 8_000_000
    # Max-batch guard bounds total wall time within the overall timeout.
    assert settings.python_mutation_generation_max_batches == 12
    assert settings.python_mutation_generation_bytes_per_line_factor == 2.0

    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_STRATEGY", "hard_cap")
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES", "4000000")
    monkeypatch.setenv("UTA_PYTHON_MUTATION_GENERATION_MAX_BATCHES", "20")

    overridden = Settings(_env_file=None)

    assert overridden.python_mutation_generation_strategy == "hard_cap"
    assert overridden.python_mutation_generation_max_generated_bytes == 4_000_000
    assert overridden.python_mutation_generation_max_batches == 20


def test_repository_java_homes_default_empty_and_parse_json_env(monkeypatch):
    """Absent from the mapping means unchanged: adding one repository's JDK
    must not move any other repository off the daemon default."""
    monkeypatch.delenv("UTA_REPOSITORY_JAVA_HOMES", raising=False)
    assert Settings(_env_file=None).repository_java_homes == {}

    monkeypatch.setenv(
        "UTA_REPOSITORY_JAVA_HOMES",
        '{"fd_wmonitor_default_store":"/opt/app/jdks/jdk25"}',
    )

    assert Settings(_env_file=None).repository_java_homes == {
        "fd_wmonitor_default_store": "/opt/app/jdks/jdk25"
    }
