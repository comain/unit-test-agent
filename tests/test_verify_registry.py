from uta.engine.verification import default_verification_registry


def test_default_verification_registry_exposes_java_and_python():
    registry = default_verification_registry()

    assert registry.runner_for("java").language == "java"
    assert registry.runner_for("python").language == "python"
