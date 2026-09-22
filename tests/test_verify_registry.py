from uta_enforce_core.registry import EnforcementRegistry
from uta.enforcement.bindings import JavaEnforcementBinding, UtaPythonEnforcementProxy


def test_enforcement_registry_exposes_java_and_python():
    registry = EnforcementRegistry([JavaEnforcementBinding(), UtaPythonEnforcementProxy()])

    assert registry.get("java").language == "java"
    assert registry.get("python").language == "python"
    assert registry.languages == ("java", "python")
