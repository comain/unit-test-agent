from pathlib import Path

from uta.engine.languages import RawTargetSelection, default_registry
from uta.engine.scoring import default_scorer_registry
from uta.language.python.scoring import PythonTargetScorer


def test_default_scorer_registry_exposes_java_and_python():
    registry = default_scorer_registry()

    assert registry.scorer_for("java").language == "java"
    assert registry.scorer_for("python").language == "python"


def test_python_target_scorer_scores_source_callables(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "jobs" / "forecast.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def forecast(store_id, day):\n"
        "    return store_id + day\n\n"
        "def _helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    target = default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/forecast.py::forecast")
    )

    result = PythonTargetScorer().score_target(Path(repo), target)

    assert result.language == "python"
    assert result.target_id == "pysymbol:jobs/forecast.py::forecast"
    assert [method["name"] for method in result.methods] == ["forecast"]
    assert result.methods[0]["planning_score"] > 0
