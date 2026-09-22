from pathlib import Path

from uta.language.python.module_resolver import resolve_python_module


def test_resolve_python_module_keeps_real_src_package_prefix(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (repo / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "log_config.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "src" / "app_shidiao.py").write_text("from .log_config import VALUE\n", encoding="utf-8")

    resolved = resolve_python_module(repo, "src/app_shidiao.py")

    assert resolved.module_name == "src.app_shidiao"
    assert resolved.pythonpath_roots == (repo,)


def test_resolve_python_module_strips_src_when_src_is_only_source_root(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src" / "shidiao").mkdir(parents=True)
    (repo / "src" / "shidiao" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "shidiao" / "routes.py").write_text("def route():\n    return 1\n", encoding="utf-8")

    resolved = resolve_python_module(repo, "src/shidiao/routes.py")

    assert resolved.module_name == "shidiao.routes"
    assert resolved.pythonpath_roots == (repo / "src", repo)


def test_resolve_python_module_uses_path_fallback_without_package_markers(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "worker.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    resolved = resolve_python_module(repo, "src/worker.py")

    assert resolved.module_name == "worker"
    assert resolved.pythonpath_roots == (repo / "src", repo)


def test_resolve_python_module_maps_dotted_filename_to_importable_stem(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "pipecat" / "config").mkdir(parents=True)
    (repo / "pipecat" / "config" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pipecat" / "config" / "settings.local.py").write_text("VALUE = 1\n", encoding="utf-8")

    resolved = resolve_python_module(repo, "pipecat/config/settings.local.py")

    assert resolved.module_name == "pipecat.config.settings"
    assert resolved.pythonpath_roots == (repo,)


def test_resolve_python_module_keeps_namespace_package_prefix(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "pipecat" / "config").mkdir(parents=True)
    (repo / "pipecat" / "config" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pipecat" / "config" / "settings.py").write_text("VALUE = 1\n", encoding="utf-8")

    resolved = resolve_python_module(repo, "pipecat/config/settings.py")

    assert resolved.module_name == "pipecat.config.settings"
    assert resolved.pythonpath_roots == (repo,)
