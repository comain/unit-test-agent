from uta.testgen.spec_context import (
    MAX_SPEC_CONTEXT_BYTES,
    SPEC_CONTEXT_TRUNCATION_MARKER,
    resolve_spec_context,
)


def test_resolve_spec_context_empty_and_inline():
    assert resolve_spec_context(None) == ""
    assert resolve_spec_context("") == ""
    assert resolve_spec_context("Refunds allowed within 30 days.") == "Refunds allowed within 30 days."


def test_resolve_spec_context_reads_existing_file(tmp_path):
    spec_file = tmp_path / "refund-rules.md"
    spec_file.write_text("Refund window is 30 days; cap is $500.\n", encoding="utf-8")

    assert resolve_spec_context(str(spec_file)) == "Refund window is 30 days; cap is $500."


def test_resolve_spec_context_treats_missing_path_as_inline(tmp_path):
    value = str(tmp_path / "does-not-exist.md")

    assert resolve_spec_context(value) == value


def test_resolve_spec_context_truncates_with_marker(tmp_path):
    spec_file = tmp_path / "big.md"
    spec_file.write_text("r" * (MAX_SPEC_CONTEXT_BYTES + 500), encoding="utf-8")

    resolved = resolve_spec_context(str(spec_file))

    assert resolved.endswith(SPEC_CONTEXT_TRUNCATION_MARKER)
    assert len(resolved.encode("utf-8")) <= MAX_SPEC_CONTEXT_BYTES + len(SPEC_CONTEXT_TRUNCATION_MARKER) + 1
