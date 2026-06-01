from pathlib import Path

from uta.tasks.autopush import passed_result_targets_and_paths


def test_passed_result_autopush_selection_scopes_to_passed_targets(tmp_path):
    repo = tmp_path
    conftest = repo / "tests" / "uta_generated" / "conftest.py"
    conftest.parent.mkdir(parents=True)
    conftest.write_text("import pytest\n", encoding="utf-8")
    passed_test = repo / "tests" / "uta_generated" / "test_passed.py"
    failed_test = repo / "tests" / "uta_generated" / "test_failed.py"
    passed_test.write_text("def test_passed():\n    assert True\n", encoding="utf-8")
    failed_test.write_text("def test_failed():\n    assert False\n", encoding="utf-8")

    targets, paths = passed_result_targets_and_paths(
        str(repo),
        {
            "pyfile:src/passed.py": {
                "status": "PASS",
                "test_file_path": str(passed_test),
            },
            "pyfile:src/failed.py": {
                "status": "FAIL",
                "test_file_path": str(failed_test),
            },
        },
    )

    assert targets == ["pyfile:src/passed.py"]
    assert paths == [
        "tests/uta_generated/conftest.py",
        "tests/uta_generated/test_passed.py",
    ]
