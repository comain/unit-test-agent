import importlib.util
import sys
from pathlib import Path


def _load_script():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "setup-fetchcode.py"
    spec = importlib.util.spec_from_file_location("setup_fetchcode", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _init_repo(path: Path, remote: str):
    path.mkdir(parents=True)
    module = _load_script()
    assert module.run(["git", "init"], cwd=path) == 0
    assert module.run(["git", "remote", "add", "origin", remote], cwd=path) == 0


def test_discover_repo_entries_preserves_api_subdirectories(tmp_path):
    module = _load_script()
    source_root = tmp_path / "services"
    _init_repo(source_root / "core-repo", "git@example:services/core-repo.git")
    _init_repo(source_root / "api" / "order-api", "git@example:services/api/order-api.git")

    entries = module.discover_repo_entries([str(source_root)])

    assert [(entry.group, entry.relative_path, entry.git_url) for entry in entries] == [
        ("services", "api/order-api", "git@example:services/api/order-api.git"),
        ("services", "core-repo", "git@example:services/core-repo.git"),
    ]


def test_repo_list_round_trip_and_destination_mapping(tmp_path):
    module = _load_script()
    entries = [
        module.RepoEntry(
            group="service_a",
            relative_path="api/product-api",
            git_url="git@example:service_a/api/product-api.git",
            source_path="/Users/example/service_a/api/product-api",
        )
    ]
    repo_list = tmp_path / "repo.txt"

    module.write_repo_list(repo_list, entries)
    loaded = module.read_repo_list(repo_list)

    assert loaded == entries
    assert module.destination_for_entry(tmp_path / "projectdir", loaded[0]) == (
        tmp_path / "projectdir" / "service_a" / "api" / "product-api"
    )


def test_api_named_repo_destinations_are_grouped_under_api_dir(tmp_path):
    module = _load_script()

    api_entry = module.RepoEntry(
        group="services",
        relative_path="example-services-core-api",
        git_url="git@example:services/example-services-core-api.git",
        source_path="/Users/example/services/example-services-core-api",
    )
    non_api_entry = module.RepoEntry(
        group="services",
        relative_path="sample-core",
        git_url="git@example:services/sample-core.git",
        source_path="/Users/example/services/sample-core",
    )

    assert module.destination_for_entry(tmp_path / "projectdir", api_entry) == (
        tmp_path / "projectdir" / "services" / "api" / "example-services-core-api"
    )
    assert module.destination_for_entry(tmp_path / "projectdir", non_api_entry) == (
        tmp_path / "projectdir" / "services" / "sample-core"
    )


def test_parse_repo_list_rejects_malformed_lines():
    module = _load_script()

    try:
        module.parse_repo_list_line("services\tonly-two-fields", line_no=7)
    except ValueError as exc:
        assert "line 7" in str(exc)
    else:
        raise AssertionError("malformed repo list line should fail")


def test_api_repo_detection_for_manifest_entries():
    module = _load_script()

    assert module.entry_looks_like_api_repo(
        module.RepoEntry("services", "api/order-api", "git@example:services/api/order-api.git", "/services/api/order-api")
    )
    assert module.entry_looks_like_api_repo(
        module.RepoEntry("service_a", "product-openapi", "git@example:service_a/product-openapi.git", "/service_a/product-openapi")
    )
    assert not module.entry_looks_like_api_repo(
        module.RepoEntry("service_b", "route-core", "git@example:service_b/route-core.git", "/service_b/route-core")
    )


def test_use_existing_repo_list_requires_file(tmp_path):
    module = _load_script()
    missing = tmp_path / "missing-repo.txt"

    exit_code = module.main([
        "--project-dir",
        str(tmp_path / "project"),
        "--repo-list",
        str(missing),
        "--use-existing-repo-list",
        "--skip-maven",
        "--dry-run",
    ])

    assert exit_code == 2


def test_existing_repo_list_with_api_repo_skips_maven_without_settings(tmp_path):
    module = _load_script()
    repo_list = tmp_path / "repo.txt"
    module.write_repo_list(
        repo_list,
        [
            module.RepoEntry(
                group="services",
                relative_path="api/order-api",
                git_url="git@example:services/api/order-api.git",
                source_path="/services/api/order-api",
            )
        ],
    )

    exit_code = module.main([
        "--project-dir",
        str(tmp_path / "project"),
        "--repo-list",
        str(repo_list),
        "--use-existing-repo-list",
        "--skip-git",
        "--settings",
        str(tmp_path / "missing-settings.xml"),
        "--dry-run",
    ])

    assert exit_code == 0


def test_existing_repo_list_reports_missing_repos(tmp_path):
    module = _load_script()
    repo_list = tmp_path / "repo.txt"
    module.write_repo_list(
        repo_list,
        [
            module.RepoEntry(
                group="services",
                relative_path="missing-core",
                git_url="git@example:services/missing-core.git",
                source_path="/services/missing-core",
            )
        ],
    )
    manifest = tmp_path / "manifest.json"

    exit_code = module.main([
        "--project-dir",
        str(tmp_path / "project"),
        "--repo-list",
        str(repo_list),
        "--use-existing-repo-list",
        "--skip-git",
        "--skip-maven",
        "--manifest",
        str(manifest),
    ])

    assert exit_code == 1
    assert '"missing_repo_count": 1' in manifest.read_text(encoding="utf-8")


def test_scan_only_generates_repo_list_without_requiring_maven_settings(tmp_path):
    module = _load_script()
    source_root = tmp_path / "services"
    _init_repo(source_root / "service-core", "git@example:services/service-core.git")
    repo_list = tmp_path / "repo.txt"

    exit_code = module.main([
        "--source-root",
        str(source_root),
        "--repo-list",
        str(repo_list),
        "--scan-only",
        "--settings",
        str(tmp_path / "missing-settings.xml"),
    ])

    assert exit_code == 0
    loaded = module.read_repo_list(repo_list)
    assert [(entry.group, entry.relative_path) for entry in loaded] == [("services", "service-core")]


def test_internal_api_jar_filter_requires_group_and_api_keyword(tmp_path):
    module = _load_script()
    api_jar = tmp_path / "com.example.order-api-1.0.0.jar"
    non_api_jar = tmp_path / "com.example.order-core-1.0.0.jar"
    external_api_jar = tmp_path / "org.example.partner-api-1.0.0.jar"
    for jar in (api_jar, non_api_jar, external_api_jar):
        jar.write_text("jar", encoding="utf-8")

    groups = ["com.example", "com.example"]

    assert module.jar_is_internal_api(api_jar, groups)
    assert not module.jar_is_internal_api(non_api_jar, groups)
    assert not module.jar_is_internal_api(external_api_jar, groups)
