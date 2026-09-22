"""The UTA-local Java enforcement binding.

A real package rather than a namespace one: every other package in this tree
declares itself, and `[tool.setuptools.packages.find]` treats the two
differently when the wheel is built.
"""

from .binding import JavaEnforcementBinding, create_java_enforcement_binding

__all__ = ["JavaEnforcementBinding", "create_java_enforcement_binding"]
