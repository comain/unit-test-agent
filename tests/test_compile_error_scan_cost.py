"""Parsing a Maven log must stay linear in its size.

`_LOC_RE`'s path class matches spaces and dots, so on a line containing no
".java" the engine backtracks from every start position -- quadratic in the
line length. Maven logs are mostly such lines, and once report rendering began
classifying compile errors on full build output, one report's tests went from
6 seconds to over 100.
"""

from __future__ import annotations

import time

from uta.language.java.compile.error_classifier import classify_compile_errors


def _noise(line_count: int, width: int) -> str:
    return "\n".join("[INFO] " + ("a b c d/e " * width) + "noise" for _ in range(line_count))


def test_a_log_of_long_non_java_lines_is_cheap():
    started = time.monotonic()
    errors = classify_compile_errors(_noise(200, 400))
    elapsed = time.monotonic() - started

    assert errors == []
    # Comfortably under a second; the unguarded scan took ~80.
    assert elapsed < 2.0, f"scan took {elapsed:.2f}s"


def test_cost_does_not_explode_with_line_length():
    """Doubling the line length must not quadruple the time."""
    def cost(width: int) -> float:
        started = time.monotonic()
        classify_compile_errors(_noise(40, width))
        return time.monotonic() - started

    narrow = cost(400)
    wide = cost(1600)

    # Quadratic would be ~16x for a 4x length increase. Allow generous slack
    # for timer noise on a loaded machine while still failing on quadratic.
    assert wide < max(0.5, narrow * 8 + 0.05), f"narrow={narrow:.4f}s wide={wide:.4f}s"


def test_the_guard_does_not_change_what_is_parsed():
    raw = (
        "[INFO] " + ("filler text without the extension " * 200) + "\n"
        "[ERROR] /repo/m/src/test/java/com/x/FooTest.java:[12,5] cannot find symbol\n"
        "symbol:   class Bar\n"
        "location: class com.x.FooTest\n"
    )

    errors = classify_compile_errors(raw)

    assert len(errors) == 1
    assert errors[0].file == "/repo/m/src/test/java/com/x/FooTest.java"
    assert errors[0].line == 12
    assert errors[0].detail == ("symbol:   class Bar", "location: class com.x.FooTest")
