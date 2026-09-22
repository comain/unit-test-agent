
from uta.language.java.generation import _ci_incremental_target_tests_for_batch
from uta.language.java.workspace import discover_ci_incremental_java_test_files


def test_ci_incremental_java_test_discovery_rejects_cross_module_test(tmp_path):
    repo = tmp_path / "repo"
    service_pom = repo / "service/pom.xml"
    provider_pom = repo / "provider/pom.xml"
    prod = repo / "service/src/main/java/com/demo/FooService.java"
    cross_module_test = repo / "provider/src/test/java/com/demo/FooServiceTest.java"
    service_pom.parent.mkdir(parents=True)
    provider_pom.parent.mkdir(parents=True)
    prod.parent.mkdir(parents=True)
    cross_module_test.parent.mkdir(parents=True)
    service_pom.write_text("<project><artifactId>service</artifactId></project>\n", encoding="utf-8")
    provider_pom.write_text("<project><artifactId>provider</artifactId></project>\n", encoding="utf-8")
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    cross_module_test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")

    discovered = discover_ci_incremental_java_test_files(
        {"repo_path": str(repo)},
        str(repo),
        "com.demo.FooService",
        "service",
    )

    assert discovered == []


def test_ci_incremental_target_tests_rejects_evidence_test_outside_target_module(tmp_path):
    repo = tmp_path / "repo"
    service_pom = repo / "service/pom.xml"
    provider_pom = repo / "provider/pom.xml"
    prod = repo / "service/src/main/java/com/demo/FooService.java"
    cross_module_test = repo / "provider/src/test/java/com/demo/FooServiceTest.java"
    service_pom.parent.mkdir(parents=True)
    provider_pom.parent.mkdir(parents=True)
    prod.parent.mkdir(parents=True)
    cross_module_test.parent.mkdir(parents=True)
    service_pom.write_text("<project><artifactId>service</artifactId></project>\n", encoding="utf-8")
    provider_pom.write_text("<project><artifactId>provider</artifactId></project>\n", encoding="utf-8")
    prod.write_text("package com.demo; class FooService {}\n", encoding="utf-8")
    cross_module_test.write_text("package com.demo; class FooServiceTest {}\n", encoding="utf-8")
    state = {
        "repo_path": str(repo),
        "rdc_context": {
            "enforcement": {
                "evidence": {
                    "targetTests": ["com.demo.FooServiceTest"],
                }
            }
        },
    }

    selected = _ci_incremental_target_tests_for_batch(state, str(repo), ["com.demo.FooService"])

    assert selected == []
