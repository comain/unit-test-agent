"""Re-export the mutmut sitecustomize shim from the lightweight package."""

from uta_py_enforce.mutmut_import_compat import (
    mutmut_import_compat_source,
    write_mutmut_import_compat,
)

__all__ = ["mutmut_import_compat_source", "write_mutmut_import_compat"]
