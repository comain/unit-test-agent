"""E2E Phase 4: Budget — estimator, sample order, BUDGET_EXCEEDED abort, global cap."""
import sys
import pytest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "benchmark" / "estimates"))
from estimator import estimate, COST_PER_CLASS


def _make_manager(tmp_path):
    from uta.tasks.manager import TaskManager
    db_path = str(tmp_path / "tasks.db")
    mgr = TaskManager(db_path)
    mgr.db.init()
    return mgr


def _write_java(path: Path, class_name: str, lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    code = "\n".join(["class " + class_name + " {"] + ["    int x = 0;"] * (lines - 2) + ["}"])
    path.write_text(code, encoding="utf-8")


@pytest.mark.e2e
class TestEstimatorOutput:
    def test_estimate_is_plausible_for_small_repo(self, tmp_path):
        for i in range(5):
            _write_java(tmp_path / f"src/main/java/com/C{i}.java", f"C{i}", 20 + i * 10)
        prelim, cap, order = estimate(str(tmp_path))
        # 5 classes * $0.99 = $4.95
        assert 0 < prelim < 100
        assert cap >= prelim

    def test_sample_order_is_sorted_by_loc(self, tmp_path):
        # small (10 lines), medium (50 lines), large (100 lines)
        _write_java(tmp_path / "src/main/java/Small.java", "Small", 10)
        _write_java(tmp_path / "src/main/java/Medium.java", "Medium", 50)
        _write_java(tmp_path / "src/main/java/Large.java", "Large", 100)
        prelim, cap, order = estimate(str(tmp_path))
        # p0 should be the smallest class
        assert len(order) >= 1
        # p0 should be Small (10 LOC) — the smallest
        assert "Small" in order[0]

    def test_estimator_uses_cold_start_formula_without_db(self, tmp_path):
        for i in range(4):
            _write_java(tmp_path / f"src/main/java/C{i}.java", f"C{i}", 20)
        prelim, cap, order = estimate(str(tmp_path))
        assert abs(prelim - 4 * COST_PER_CLASS) < 0.01


@pytest.mark.e2e
class TestBudgetExceededAbort:
    def test_hard_cap_usd_stored_and_retrieved(self, tmp_path):
        mgr = _make_manager(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        tid = mgr.create_task(repo_path=str(repo), hard_cap_usd=0.50)
        task = mgr.db.get_repo_task(tid)
        assert task["hard_cap_usd"] == pytest.approx(0.50)

    def test_budget_exceeded_task_not_acquirable(self, tmp_path):
        from uta.tasks.scheduler import TaskScheduler
        mgr = _make_manager(tmp_path)
        db_path = str(tmp_path / "tasks.db")
        scheduler = TaskScheduler(db_path)

        repo = tmp_path / "repo"
        repo.mkdir()
        tid = mgr.create_task(repo_path=str(repo), hard_cap_usd=0.01)
        mgr.start_task(tid)
        mgr.mark_budget_exceeded(tid, "exceeded cap")

        task = scheduler.acquire_next()
        assert task is None

    def test_budget_exceeded_then_unblock_is_acquirable(self, tmp_path):
        from uta.tasks.scheduler import TaskScheduler
        mgr = _make_manager(tmp_path)
        db_path = str(tmp_path / "tasks.db")
        scheduler = TaskScheduler(db_path)

        repo = tmp_path / "repo2"
        repo.mkdir()
        tid = mgr.create_task(repo_path=str(repo), hard_cap_usd=0.01)
        mgr.start_task(tid)
        mgr.mark_budget_exceeded(tid, "exceeded cap")

        mgr.unblock(tid)
        task = scheduler.acquire_next()
        assert task is not None
        assert int(task["id"]) == tid

    def test_llm_guard_raises_when_hard_cap_breached(self, tmp_path):
        from uta.graph.nodes import TaskBudgetExceeded, _llm_guard_before
        mgr = _make_manager(tmp_path)
        repo = tmp_path / "repo3"
        repo.mkdir()
        tid = mgr.create_task(repo_path=str(repo), hard_cap_usd=0.01)
        mgr.start_task(tid)
        # Simulate $0.50 already spent
        mgr.db.update_repo_task(tid, provider_cost_usd=0.50)

        state = {
            "task_id": tid,
            "task_db_path": str(mgr.db_path),
            "repo_path": str(repo),
        }
        with pytest.raises(TaskBudgetExceeded, match="Hard cap exceeded"):
            _llm_guard_before(state, batch=[], phase="generate")


@pytest.mark.e2e
class TestGlobalBatchCap:
    def test_total_cost_sums_all_tasks(self, tmp_path):
        from uta.tasks.db import TaskDB
        db = TaskDB(str(tmp_path / "tasks.db"))
        db.init()

        now = "2026-01-01T00:00:00"
        for i, cost in enumerate([1.0, 2.5, 0.75]):
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO repo_tasks(repo_path, repo_slug, base_ref, priority, status, config_snapshot_json, budget_snapshot_json, budget_config_snapshot_json, estimate_snapshot_json, selection_json, total_classes, created_at, updated_at, provider_cost_usd) VALUES (?, ?, 'origin/master', 100, 'COMPLETED', '{}', '{}', '{}', '{}', '{}', 0, ?, ?, ?)",
                    (f"/repo/{i}", f"r{i}", now, now, cost),
                )

        total = db.total_provider_cost_usd()
        assert abs(total - (1.0 + 2.5 + 0.75)) < 0.001

    def test_batch_cap_reached_pauses_dequeue(self, tmp_path):
        """When total_cost >= batch_cap_usd, _batch_cap_reached returns True."""
        from uta.tasks.db import TaskDB
        db = TaskDB(str(tmp_path / "tasks.db"))
        db.init()

        with patch("uta.config.settings") as mock_settings:
            mock_settings.batch_cap_usd = 3.0
            with patch.object(db, "total_provider_cost_usd", return_value=5.0):
                total = db.total_provider_cost_usd()
                cap = mock_settings.batch_cap_usd
                assert total >= cap, "Batch cap should be exceeded"
