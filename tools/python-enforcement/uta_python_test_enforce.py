#!/usr/bin/env python3
"""Lightweight UTA Python enforcement entrypoint."""

from __future__ import annotations

import sys

from uta_py_enforce.cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
