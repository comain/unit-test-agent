"""Python runtime configuration helpers for lightweight enforcement."""

from __future__ import annotations

import os
import sys
from typing import Mapping


CI_SAMPLING_ENV = "UTA_PYTHON_ENABLE_CI_MUTATION_SAMPLING"


def runtime_bins(syntax_version: str) -> tuple[str, str]:
    if syntax_version == "python2":
        return os.environ.get("UTA_PYTHON2_BIN", "python2"), os.environ.get("UTA_PYTHON2_MUTMUT_BIN", "mutmut")
    return os.environ.get("UTA_PYTHON_BIN", sys.executable), os.environ.get("UTA_PYTHON_MUTMUT_BIN", "mutmut")


def env_without_ci_sampling(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env if base_env is not None else os.environ)
    env.pop(CI_SAMPLING_ENV, None)
    return env


# -- mutation generation strategy (ADR-002) ----------------------------------
#
# Defaults mirror `uta.shared.config`, which is what production runs. A binding
# that defaulted to `hard_cap` would quietly enforce less than the product it
# replaces, and the difference only shows on diffs large enough to hit the cap
# -- exactly the ones worth mutating.

GENERATION_STRATEGY_ENV = "UTA_PYTHON_MUTATION_GENERATION_STRATEGY"

#: Keep mutmut's generated `mutants/` tree after a run instead of deleting it.
#: Diagnostic only -- the tree is a full copy of the repository under test.
KEEP_MUTANTS_ENV = "UTA_PYTHON_MUTATION_KEEP_MUTANTS"

#: The cap the `hard_cap` strategy applies, and the ceiling batching works under.
GENERATION_HARD_CAP = 1000


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "") or 0)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or 0.0)
    except ValueError:
        return default
    return value if value > 0 else default


def generation_strategy() -> str:
    """`batch` (ADR-002 default) or `hard_cap` (its documented rollback)."""
    value = (os.environ.get(GENERATION_STRATEGY_ENV, "") or "").strip().lower()
    return value if value in ("batch", "hard_cap") else "batch"


def generation_max_generated_bytes() -> int:
    """Per-batch budget for the generated module, keeping CPython's parse linear."""
    return _int_env("UTA_PYTHON_MUTATION_GENERATION_MAX_GENERATED_BYTES", 8_000_000)


def generation_max_batches() -> int:
    """Guard on total batches; work beyond it is trimmed lowest-priority first."""
    return _int_env("UTA_PYTHON_MUTATION_GENERATION_MAX_BATCHES", 12)


def generation_bytes_per_line_factor() -> float:
    """mutmut emits several mutants per line; the raw estimate under-counts ~2x."""
    return _float_env("UTA_PYTHON_MUTATION_GENERATION_BYTES_PER_LINE_FACTOR", 2.0)
