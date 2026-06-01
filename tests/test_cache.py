import pytest
import os
import shutil
from uta.language.java.parse.cache import CacheManager
from uta.language.java.parse.models import ParseResult, ParsedSymbol

def test_cache_cycle(tmp_path, fixtures_dir):
    cache_dir = tmp_path / ".uta_cache"
    manager = CacheManager(str(cache_dir))
    
    service_path = os.path.join(fixtures_dir, "SampleService.java")
    
    # 1. First get: should be None
    assert manager.get_parsed(service_path) is None
    
    # 2. Save
    result = ParseResult(
        path=service_path,
        package="com.example",
        symbols=[ParsedSymbol(kind="class", name="SampleService", fqn="com.example.SampleService", line=1)]
    )
    manager.save_parsed(service_path, result)
    
    # 3. Second get: should be same
    cached = manager.get_parsed(service_path)
    assert cached is not None
    assert cached.package == "com.example"
    assert cached.symbols[0].name == "SampleService"
    
    # 4. Modify file: should be None (invalidation)
    # Create a temp file
    temp_java = tmp_path / "Temp.java"
    temp_java.write_text("class Temp {}")
    
    manager.save_parsed(str(temp_java), ParseResult(path=str(temp_java), package="p", symbols=[]))
    assert manager.get_parsed(str(temp_java)) is not None
    
    # Update content
    temp_java.write_text("class Temp { int a; }")
    assert manager.get_parsed(str(temp_java)) is None


def test_cache_invalidates_on_version_mismatch(tmp_path, fixtures_dir):
    cache_dir = tmp_path / ".uta_cache"
    manager = CacheManager(str(cache_dir))

    service_path = os.path.join(fixtures_dir, "SampleService.java")
    result = ParseResult(
        path=service_path,
        package="com.example",
        symbols=[ParsedSymbol(kind="class", name="SampleService", fqn="com.example.SampleService", line=1)]
    )
    manager.save_parsed(service_path, result)
    rel_path = str(service_path)
    manager.index[rel_path]["cache_version"] = "old-version"
    manager._save_index()

    assert manager.get_parsed(service_path) is None


def test_cache_deserializes_complexity(tmp_path, fixtures_dir):
    cache_dir = tmp_path / ".uta_cache"
    manager = CacheManager(str(cache_dir))

    service_path = os.path.join(fixtures_dir, "SampleService.java")
    result = ParseResult(
        path=service_path,
        package="com.example",
        symbols=[
            ParsedSymbol(
                kind="method",
                name="run",
                fqn="com.example.SampleService.run",
                line=3,
                complexity={"cyclomatic_approx": 3, "body_lines": 12},
            )
        ],
    )
    manager.save_parsed(service_path, result)

    cached = manager.get_parsed(service_path)
    assert cached is not None
    assert cached.symbols[0].complexity == {"cyclomatic_approx": 3, "body_lines": 12}


def test_cache_recovers_from_corrupt_index(tmp_path):
    cache_dir = tmp_path / ".uta_cache"
    cache_dir.mkdir()
    index_file = cache_dir / "parse_index.json"
    index_file.write_text("")

    manager = CacheManager(str(cache_dir))

    assert manager.index == {}
    assert not index_file.exists()
    assert list(cache_dir.glob("parse_index.json.corrupt.*"))


def test_cache_recovers_from_corrupt_parsed_file(tmp_path):
    cache_dir = tmp_path / ".uta_cache"
    manager = CacheManager(str(cache_dir))
    java_file = tmp_path / "Temp.java"
    java_file.write_text("class Temp {}")

    result = ParseResult(path=str(java_file), package="p", symbols=[])
    manager.save_parsed(str(java_file), result)

    content_hash = manager.get_hash(str(java_file))
    cache_file = manager.parsed_dir / f"{content_hash}.json"
    cache_file.write_text("{")

    assert manager.get_parsed(str(java_file)) is None
    assert str(java_file) not in manager.index
    assert list(manager.parsed_dir.glob(f"{content_hash}.json.corrupt.*"))
