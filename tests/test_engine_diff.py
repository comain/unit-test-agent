"""Unit spec for the shared git-diff helpers in uta.enforcement.diff."""

from uta.enforcement.diff import changed_production_files, parse_added_diff_lines


def test_parse_added_diff_lines_unified_zero():
    diff = (
        "diff --git a/jobs/forecast.py b/jobs/forecast.py\n"
        "--- a/jobs/forecast.py\n"
        "+++ b/jobs/forecast.py\n"
        "@@ -10,0 +11,2 @@\n"
        "+    new_line_one\n"
        "+    new_line_two\n"
        "@@ -20,1 +22,1 @@\n"
        "-    old\n"
        "+    replaced\n"
    )
    assert parse_added_diff_lines(diff) == {11, 12, 22}


def test_parse_added_diff_lines_ignores_headers_and_deletions():
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,0 @@\n-    gone_one\n-    gone_two\n"
    )
    assert parse_added_diff_lines(diff) == set()


def test_parse_added_diff_lines_empty():
    assert parse_added_diff_lines("") == set()


class _Adapter:
    def is_production_source_path(self, path: str) -> bool:
        return path.endswith(".py") and "tests/" not in path


def test_changed_production_files_uses_adapter_filter(monkeypatch):
    import uta.enforcement.diff as diff

    monkeypatch.setattr(
        diff,
        "changed_paths",
        lambda repo, base_ref: ["jobs/a.py", "tests/test_a.py", "README.md", "jobs/b.py"],
    )
    assert changed_production_files(_Adapter(), "/repo", "HEAD~1") == ["jobs/a.py", "jobs/b.py"]
