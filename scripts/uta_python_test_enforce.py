#!/usr/bin/env python3
"""Removed compatibility path for UTA Python enforcement.

Use tools/python-enforcement/uta_python_test_enforce.py instead.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "scripts/uta_python_test_enforce.py has been removed as an enforcement "
        "implementation. Configure UTA_PYTHON_ENFORCE_SCRIPT to point at "
        "tools/python-enforcement/uta_python_test_enforce.py.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
