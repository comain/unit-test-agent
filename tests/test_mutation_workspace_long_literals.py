"""Long string literals are not mutmut support files.

Production report 8bbb31f51fbd4e608ec1b43e5603183f crashed in
``write_mutmut_setup`` while scanning ``qa_knowledge/runtime/answer_evaluation.py``.
A concatenated Chinese policy answer has no path separator, so the first
``_literal_repo_path`` look-up skips it. The fallback then joins the string onto
the source file's directory, which *does* contain ``/``, and ``Path.exists()``
raises ``OSError: [Errno 36] File name too long``. The CI process died before
writing an evidence envelope.

A real fixture such as ``fixture.json`` must still be copied.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from uta_py_enforce.mutation_workspace import _literal_repo_path, mutation_support_copy_paths


# Longer than NAME_MAX (255 bytes) so the parent-join fallback raises
# ENAMETOOLONG instead of looking like a missing file. The production
# Chinese answer is 321 UTF-8 bytes; Linux raises, macOS may not, so the
# portable reproduction uses a 300-byte ASCII token.
_LONG_PROMPT = "x" * 300
_PRODUCTION_ANSWER = (
    "晋升店副需先确认具备银牌及以上标签。若符合，请本人发起政委答疑流程查询资格并申请晋升，"
    "申请时提供姓名、工号、目标门店名称及可开始担任店副的时间，并请协助走申请流程；"
    "也可由目标门店店经理发起【店副经理晋升推荐】流程。"
)


def _nested_prompt_repo(tmp_path: Path, prompt: str) -> Path:
    package = tmp_path / "qa_knowledge" / "runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "answer_evaluation.py").write_text(
        f"PREFERRED = {prompt!r}\n",
        encoding="utf-8",
    )
    (tmp_path / "test_answer_evaluation.py").write_text(
        "from qa_knowledge.runtime import answer_evaluation\n",
        encoding="utf-8",
    )
    return tmp_path


def test_a_long_prompt_literal_is_not_a_file_dependency(tmp_path):
    repo = _nested_prompt_repo(tmp_path, _LONG_PROMPT)

    paths = mutation_support_copy_paths(
        repo,
        "qa_knowledge/runtime/answer_evaluation.py",
        ["test_answer_evaluation.py"],
    )

    assert "qa_knowledge/runtime/__init__.py" in paths
    assert not any("x" * 40 in path for path in paths)


def test_the_production_chinese_answer_literal_does_not_crash(tmp_path):
    repo = _nested_prompt_repo(tmp_path, _PRODUCTION_ANSWER)

    paths = mutation_support_copy_paths(
        repo,
        "qa_knowledge/runtime/answer_evaluation.py",
        ["test_answer_evaluation.py"],
    )

    assert "qa_knowledge/runtime/__init__.py" in paths
    assert not any("晋升店副" in path for path in paths)


def test_an_actual_dependency_file_is_preserved(tmp_path):
    (tmp_path / "fixture.json").write_text("{}", encoding="utf-8")

    assert _literal_repo_path(tmp_path, "fixture.json") == Path("fixture.json")


def test_enametoolong_from_exists_is_ignored(tmp_path, monkeypatch):
    def too_long(self):
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(Path, "exists", too_long)
    assert _literal_repo_path(tmp_path, "fixture.json") is None


def test_path_permission_errors_remain_visible(tmp_path, monkeypatch):
    def denied(self):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(Path, "exists", denied)
    with pytest.raises(PermissionError):
        _literal_repo_path(tmp_path, "fixture.json")
