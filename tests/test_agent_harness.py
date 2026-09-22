from uta.testgen.harness import create_agent_harness


def test_factory_selects_configured_harness_without_product_agent_branching(
    monkeypatch,
):
    seen = {}
    configured = object()

    def create(spec):
        seen["spec"] = spec
        return configured

    monkeypatch.setattr("uta.testgen.harness.create_configured_harness", create)

    harness = create_agent_harness()

    assert harness is configured
    assert seen["spec"].name
    assert seen["spec"].cache_dir
