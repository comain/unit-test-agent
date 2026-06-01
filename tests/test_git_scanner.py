import pytest
from unittest.mock import patch, MagicMock
from uta.engine.source_selection import (
    filter_files,
    get_all_java_files,
    get_all_python_files,
    get_changed_java_files,
    get_changed_python_files,
)

@pytest.fixture
def mock_git_log():
    return """
biz/src/main/java/com/example/Service1.java
biz/src/main/java/com/example/Service1.java
biz/src/main/java/com/example/Service2.java
common/src/main/java/com/example/CommonUtils.java
biz/src/test/java/com/example/Service1Test.java
non-java-file.txt
"""

def test_get_changed_java_files_no_module(mock_git_log):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=mock_git_log, check_returncode=lambda: None)
        
        files = get_changed_java_files("/fake/repo", days=30)
        
        # Should exclude: Service1Test.java (src/test), non-java-file.txt
        # Should include: Service1.java (2 times), Service2.java, CommonUtils.java
        assert len(files) == 3
        # Service1.java should be first
        assert files[0][0] == "biz/src/main/java/com/example/Service1.java"
        assert files[0][1] == 2
        
def test_get_changed_java_files_with_module(mock_git_log):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=mock_git_log, check_returncode=lambda: None)
        
        # Filter by 'biz' module
        files = get_changed_java_files("/fake/repo", days=30, module="biz")
        
        # Should only include Service1 and Service2 (CommonUtils is in common module)
        assert len(files) == 2
        paths = [f[0] for f in files]
        assert "biz/src/main/java/com/example/Service1.java" in paths
        assert "biz/src/main/java/com/example/Service2.java" in paths
        assert "common/src/main/java/com/example/CommonUtils.java" not in paths

def test_filter_files():
    ranked_files = [
        ("file1.java", 10),
        ("file2.java", 5),
        ("file3.java", 2)
    ]
    
    top_2 = filter_files(ranked_files, max_files=2)
    assert top_2 == ["file1.java", "file2.java"]


def test_get_all_java_files(tmp_path):
    (tmp_path / "biz/src/main/java/com/example").mkdir(parents=True)
    (tmp_path / "biz/src/test/java/com/example").mkdir(parents=True)
    (tmp_path / "common/src/main/java/com/example").mkdir(parents=True)
    (tmp_path / "biz/src/main/java/com/example/B.java").write_text("class B {}")
    (tmp_path / "biz/src/main/java/com/example/A.java").write_text("class A {}")
    (tmp_path / "biz/src/test/java/com/example/ATest.java").write_text("class ATest {}")
    (tmp_path / "common/src/main/java/com/example/C.java").write_text("class C {}")

    assert get_all_java_files(str(tmp_path), module="biz") == [
        ("biz/src/main/java/com/example/A.java", 1),
        ("biz/src/main/java/com/example/B.java", 1),
    ]


def test_get_changed_python_files_filters_production_sources():
    git_log = """
jobs/forecast.py
jobs/forecast.py
jobs/train.py
tests/test_forecast.py
.venv/lib/site-packages/vendor.py
jobs/__init__.py
jobs/test_helper.py
README.md
"""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=git_log, check_returncode=lambda: None)

        files = get_changed_python_files("/fake/repo", days=14)

    assert files == [
        ("jobs/forecast.py", 2),
        ("jobs/train.py", 1),
    ]


def test_get_all_python_files_uses_stable_production_filter(tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "b.py").write_text("def b():\n    return 1\n", encoding="utf-8")
    (tmp_path / "jobs" / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "jobs" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "jobs" / "test_helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("def ignored():\n    return 1\n", encoding="utf-8")

    assert get_all_python_files(str(tmp_path)) == [
        ("jobs/a.py", 1),
        ("jobs/b.py", 1),
    ]
