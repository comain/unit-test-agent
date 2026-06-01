"""Tests for benchmark/estimates/estimator.py (Task 11)."""
import sys
import json
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "benchmark" / "estimates"))
from estimator import estimate, _count_java_classes, _regression_estimate, COST_PER_CLASS


def _write_java(path: Path, class_name: str, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    code = "\n".join(["class " + class_name + " {"] + ["    int x = 0;"] * (lines - 2) + ["}"])
    path.write_text(code, encoding="utf-8")


class TestCountJavaClasses:
    def test_counts_classes_under_src_main(self, tmp_path):
        _write_java(tmp_path / "src/main/java/com/example/Foo.java", "Foo", 10)
        _write_java(tmp_path / "src/main/java/com/example/Bar.java", "Bar", 20)
        classes = _count_java_classes(str(tmp_path))
        assert len(classes) == 2

    def test_ignores_test_sources(self, tmp_path):
        _write_java(tmp_path / "src/main/java/com/Foo.java", "Foo", 5)
        _write_java(tmp_path / "src/test/java/com/FooTest.java", "FooTest", 5)
        classes = _count_java_classes(str(tmp_path))
        assert len(classes) == 1
        assert all("FooTest" not in fqn for fqn, _ in classes)

    def test_returns_loc(self, tmp_path):
        _write_java(tmp_path / "src/main/java/com/Big.java", "Big", 50)
        classes = _count_java_classes(str(tmp_path))
        assert len(classes) == 1
        fqn, loc = classes[0]
        assert loc == 50

    def test_empty_repo(self, tmp_path):
        classes = _count_java_classes(str(tmp_path))
        assert classes == []


class TestRegressionEstimate:
    def test_returns_none_for_missing_db(self):
        result = _regression_estimate("/nonexistent/path/tasks.db")
        assert result is None

    def test_returns_none_for_fewer_than_five_runs(self, tmp_path):
        import sqlite3
        db_path = str(tmp_path / "tasks.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE repo_tasks (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("CREATE TABLE class_tasks (id INTEGER PRIMARY KEY, repo_task_id INTEGER, provider_cost_usd REAL)")
        conn.execute("INSERT INTO repo_tasks VALUES (1, 'COMPLETED')")
        conn.execute("INSERT INTO class_tasks VALUES (1, 1, 0.99)")
        conn.commit()
        conn.close()
        result = _regression_estimate(db_path)
        assert result is None

    def test_returns_slope_for_five_plus_runs(self, tmp_path):
        import sqlite3
        db_path = str(tmp_path / "tasks.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE repo_tasks (id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute("CREATE TABLE class_tasks (id INTEGER PRIMARY KEY, repo_task_id INTEGER, provider_cost_usd REAL)")
        # 5 completed runs with varying class counts: i*5 classes @ $1/class each
        for i in range(1, 6):
            conn.execute(f"INSERT INTO repo_tasks VALUES ({i}, 'COMPLETED')")
            for j in range(i * 5):
                conn.execute("INSERT INTO class_tasks VALUES (?, ?, ?)", (i * 100 + j, i, 1.0))
        conn.commit()
        conn.close()
        slope = _regression_estimate(db_path)
        assert slope is not None
        assert 0.5 <= slope <= 2.0  # Should be ~$1/class


class TestEstimate:
    def test_returns_tuple_of_three(self, tmp_path):
        result = estimate(str(tmp_path))
        assert len(result) == 3
        prelim, cap, order = result
        assert isinstance(prelim, float)
        assert isinstance(cap, float)
        assert isinstance(order, list)

    def test_cap_is_larger_than_prelim(self, tmp_path):
        _write_java(tmp_path / "src/main/java/Foo.java", "Foo", 20)
        prelim, cap, order = estimate(str(tmp_path))
        assert cap >= prelim

    def test_sample_order_has_p0_p90_p95(self, tmp_path):
        # Create 20 classes of varying sizes
        for i in range(20):
            _write_java(
                tmp_path / f"src/main/java/com/Class{i}.java",
                f"Class{i}",
                10 + i * 5,
            )
        prelim, cap, order = estimate(str(tmp_path))
        assert len(order) >= 1
        assert len(order) <= 3

    def test_empty_repo_returns_zero_cost(self, tmp_path):
        prelim, cap, order = estimate(str(tmp_path))
        assert prelim == 0.0
        assert order == []

    def test_cold_start_cost_per_class(self, tmp_path):
        """Should use COST_PER_CLASS = $0.99 when no DB provided."""
        for i in range(10):
            _write_java(tmp_path / f"src/main/java/C{i}.java", f"C{i}", 10)
        prelim, cap, order = estimate(str(tmp_path))
        assert abs(prelim - 10 * COST_PER_CLASS) < 0.01
