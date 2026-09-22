"""Real Java-8 reactor proof; run explicitly with pytest -m integration."""

from pathlib import Path
import os
import shutil
import subprocess
import copy
import xml.etree.ElementTree as ET

import pytest


pytestmark = pytest.mark.integration


def _write(root: Path, path: str, text: str) -> None:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)


def create_reactor(root: Path, *, module_local_config: bool = True, enforcer_version: str = "1.0.16") -> None:
    _write(root, "pom.xml", """<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>reactor</artifactId>
<version>1</version><packaging>pom</packaging><modules><module>service</module><module>biz</module></modules>
<properties><maven.compiler.source>8</maven.compiler.source><maven.compiler.target>8</maven.compiler.target>
<skipPitest>false</skipPitest><pitest.targets>example.StaleTarget</pitest.targets></properties>
<dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency></dependencies>
<build><plugins>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.11.0</version></plugin>
<plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>2.22.2</version></plugin>
<plugin><groupId>com.example.build.maven-plugins</groupId><artifactId>test-enforcer</artifactId><version>1.0.16</version>
<executions><execution><id>filter</id><phase>initialize</phase><goals><goal>filter-diff</goal></goals></execution></executions></plugin>
<plugin><groupId>org.pitest</groupId><artifactId>pitest-maven</artifactId><version>1.15.0</version>
<configuration><skip>${skipPitest}</skip><targetClasses><param>${pitest.targets}</param></targetClasses>
<testStrengthThreshold>100</testStrengthThreshold><outputFormats><param>XML</param><param>HTML</param></outputFormats>
<timestampedReports>false</timestampedReports></configuration>
<executions><execution><id>mutation</id><phase>verify</phase><goals><goal>mutationCoverage</goal></goals>
<configuration><skip>${skipPitest}</skip><targetClasses><param>${pitest.targets}</param></targetClasses></configuration>
</execution></executions></plugin></plugins></build></project>""")
    for module in ("service", "biz"):
        dependency = "" if module == "service" else """<dependencies><dependency><groupId>example</groupId>
<artifactId>service</artifactId><version>1</version></dependency></dependencies>"""
        _write(root, f"{module}/pom.xml", f"""<project><modelVersion>4.0.0</modelVersion>
<parent><groupId>example</groupId><artifactId>reactor</artifactId><version>1</version></parent>
<artifactId>{module}</artifactId>{dependency}</project>""")
    if module_local_config:
        # The incident POM declares PIT and its defaults separately in each module.
        # Inherited parent configuration is a distinct Maven interpolation case.
        tree = ET.parse(root / "pom.xml")
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        plugins = tree.find("m:build/m:plugins", ns)
        pit = next(p for p in plugins if p.findtext("m:artifactId", namespaces=ns) == "pitest-maven")
        plugins.remove(pit)
        for module in ("service", "biz"):
            child = ET.parse(root / module / "pom.xml")
            properties = ET.SubElement(child.getroot(), "properties")
            ET.SubElement(properties, "skipPitest").text = "false"
            ET.SubElement(properties, "pitest.targets").text = "example.StaleTarget"
            ET.SubElement(properties, "targetClasses").text = "${pitest.targets}"
            build = ET.SubElement(child.getroot(), "build")
            child_plugins = ET.SubElement(build, "plugins")
            plugin_copy = copy.deepcopy(pit)
            for node in plugin_copy.iter():
                node.tag = node.tag.split("}")[-1]
            child_plugins.append(plugin_copy)
            child.write(root / module / "pom.xml", encoding="unicode")
        for node in tree.getroot().iter():
            node.tag = node.tag.split("}")[-1]
        tree.write(root / "pom.xml", encoding="unicode")
    _write(root, "service/src/main/java/example/DependencyLogic.java", """package example;
public class DependencyLogic { public int calculate(int n) { return n + 1; } }
""")
    _write(root, "biz/src/main/java/example/PricingLogic.java", """package example;
public class PricingLogic {
    public int calculate(int n) {
        return n + 1;
    }
}
""")
    _write(root, "biz/src/test/java/example/PricingLogicTest.java", """package example;
import org.junit.Test;
import static org.junit.Assert.assertEquals;
public class PricingLogicTest {
    @Test public void addsTwo() { assertEquals(5, new PricingLogic().calculate(3)); }
}
""")
    pom = root / "pom.xml"
    pom.write_text(pom.read_text().replace("<version>1.0.16</version>", f"<version>{enforcer_version}</version>"))
    for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c",
                 "user.email=fixture@example.invalid", "commit", "-qm", "base"],
                 ["update-ref", "refs/remotes/origin/master", "HEAD"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    source = root / "biz/src/main/java/example/PricingLogic.java"
    source.write_text(source.read_text().replace("n + 1", "n + 2"))
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-qm", "change pricing"], cwd=root, check=True)


def maven_command() -> list[str]:
    executable = os.environ.get("UTA_TEST_MAVEN") or shutil.which("mvn")
    assert executable, "Set UTA_TEST_MAVEN to Maven 3.9.10 (Java 8 required)"
    return [executable, "-B", "-ntp", "verify", "-pl", "biz", "-am",
            "-Dtest.enforcement.enabled=true", "-Dtest.enforcement.pitest.testStrengthThreshold=100"]


@pytest.mark.parametrize("weak", [False, True])
def test_pom_excluded_classes_cannot_erase_authoritative_targets(tmp_path, weak):
    from uta.language.java.maven_compat.launcher import prepare_command
    create_reactor(tmp_path)
    pom = tmp_path / "biz/pom.xml"
    tree = ET.parse(pom)
    plugin = next(p for p in tree.findall("build/plugins/plugin")
                  if p.findtext("artifactId") == "pitest-maven")
    config = plugin.find("configuration")
    excluded = ET.SubElement(config, "excludedClasses")
    ET.SubElement(excluded, "param").text = "example.PricingLogic*"
    ET.SubElement(config, "failWhenNoMutations").text = "false"
    tree.write(pom, encoding="unicode")
    if weak:
        test = tmp_path / "biz/src/test/java/example/PricingLogicTest.java"
        test.write_text(test.read_text().replace("assertEquals(5, new PricingLogic().calculate(3))",
                                                "org.junit.Assert.assertTrue(new PricingLogic().calculate(3) > 0)"))
    cmd = prepare_command(maven_command() + ["-DtargetTests=example.PricingLogicTest"], tmp_path)
    result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=240)
    assert "Generated 2 mutations" in result.stdout, result.stdout[-4000:]
    assert result.returncode == 0, result.stdout[-4000:]
    assert "[uta-pit-compat] verified module=biz skip=false" in result.stdout


def test_report_command_reused_for_repair_metadata(tmp_path):
    from uta.language.java.maven_compat.launcher import prepare_command
    from uta.language.java.enforcement_runner import MavenEnforcementRunner
    from uta.language.java.maven_project import test_enforcement_tooling_status
    create_reactor(tmp_path)
    command = prepare_command(maven_command(), tmp_path)
    original_evidence = Path(next(x.split("=", 1)[1] for x in command if x.startswith("-Duta.pit.compat.evidence=")))
    original_evidence.write_text("original report evidence must not be overwritten")
    runner = MavenEnforcementRunner("mvn verify -Dtest.enforcement.enabled=true")
    status = test_enforcement_tooling_status(tmp_path, maven_bin=command[0],
        run_maven_command=runner._run_command, profile_source_cmd=command)
    assert status.available, status.reason
    assert original_evidence.read_text() == "original report evidence must not be overwritten"


def move_gate_into_profiles(root: Path) -> None:
    """Mirror DMS: root-only diff execution and child-owned profile properties."""
    parent = ET.parse(root / "pom.xml")
    plugin = next(p for p in parent.findall("build/plugins/plugin") if p.findtext("artifactId") == "test-enforcer")
    ET.SubElement(plugin, "inherited").text = "false"
    for module in ("service", "biz"):
        tree = ET.parse(root / module / "pom.xml")
        child_plugin = copy.deepcopy(plugin)
        child_plugin.remove(child_plugin.find("inherited"))
        tree.find("build/plugins").insert(0, child_plugin)
        pit = next(p for p in tree.findall("build/plugins/plugin") if p.findtext("artifactId") == "pitest-maven")
        execution = pit.find("executions/execution")
        execution.remove(execution.find("configuration"))
        # DMS also pins an unrelated historical test in its active profile.
        ET.SubElement(tree.find("properties"), "targetTests").text = "example.StaleTest"
        tests = ET.SubElement(pit.find("configuration"), "targetTests")
        ET.SubElement(tests, "param").text = "${targetTests}"
        tree.write(root / module / "pom.xml", encoding="unicode")
    parent.write(root / "pom.xml", encoding="unicode")
    for path in (root / "pom.xml", root / "service/pom.xml", root / "biz/pom.xml"):
        tree = ET.parse(path)
        project = tree.getroot()
        profile = ET.SubElement(ET.SubElement(project, "profiles"), "profile")
        ET.SubElement(profile, "id").text = "test-enforcement"
        activation = ET.SubElement(ET.SubElement(profile, "activation"), "property")
        ET.SubElement(activation, "name").text = "test.enforcement.enabled"
        ET.SubElement(activation, "value").text = "true"
        for tag in ("properties", "build"):
            element = project.find(tag)
            project.remove(element)
            profile.append(element)
        tree.write(path, encoding="unicode")


@pytest.mark.parametrize("compat,weak", [(False, False), (True, False), (True, True)])
def test_profile_reactor_preserves_no_target_skip(tmp_path, compat, weak):
    create_reactor(tmp_path)
    move_gate_into_profiles(tmp_path)
    _write(tmp_path, "service/src/test/java/example/DependencyLogicTest.java", """package example;
import org.junit.Test;
import static org.junit.Assert.assertEquals;
public class DependencyLogicTest {
    @Test public void addsOne() { assertEquals(4, new DependencyLogic().calculate(3)); }
}
""")
    if weak:
        test_file = tmp_path / "biz/src/test/java/example/PricingLogicTest.java"
        test_file.write_text(test_file.read_text().replace(
            "assertEquals(5, new PricingLogic().calculate(3))",
            "org.junit.Assert.assertTrue(new PricingLogic().calculate(3) > 0)",
        ))
    cmd = maven_command() + ["-DskipTests=false", "-Dmaven.test.skip=false", "-Dmaven.test.failure.ignore=true",
                             "-DtargetTests=example.PricingLogicTest"]
    if compat:
        from uta.language.java.maven_compat.launcher import prepare_command
        cmd = prepare_command(cmd, tmp_path)
    result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=240)
    (tmp_path / "maven.log").write_text(result.stdout + result.stderr)
    if not compat:
        assert result.returncode != 0
        assert "No mutations found" in result.stdout
        return
    assert "[uta-pit-compat] verified module=service skip=true" in result.stdout
    assert "[uta-pit-compat] verified module=biz skip=false" in result.stdout
    assert result.returncode == 0, result.stdout[-6000:]
    if not weak:
        assert "Generated 2 mutations Killed 2 (100%)" in result.stdout
    from uta.language.java.maven_compat.launcher import validate_completion
    assert validate_completion(cmd)["modules"]


def test_reactor_property_binding_probe(tmp_path):
    create_reactor(tmp_path)
    result = subprocess.run(maven_command(), cwd=tmp_path, capture_output=True, text=True, timeout=240)
    (tmp_path / "maven.log").write_text(result.stdout + result.stderr)
    # This is a counterexample to the proposed root cause, not a GREEN extension
    # test: effective-POM literals alone do not prove runtime binding is frozen.
    assert result.returncode == 0, result.stdout[-6000:]
    assert "Generated 2 mutations Killed 2 (100%)" in result.stdout
    assert not list((tmp_path / "service/target").glob("pit-reports/**/mutations.xml"))


@pytest.mark.parametrize("enforcer_version", ["1.0.15", "1.0.16"])
def test_real_profile_version_preflight(tmp_path, enforcer_version):
    from uta.language.java.maven_project import test_enforcement_tooling_status as tooling_status

    create_reactor(tmp_path, enforcer_version=enforcer_version)
    tree = ET.parse(tmp_path / "pom.xml")
    root = tree.getroot()
    plugins = root.find("build/plugins")
    enforcer = next(p for p in plugins if p.findtext("artifactId") == "test-enforcer")
    plugins.remove(enforcer)
    profile = ET.SubElement(ET.SubElement(root, "profiles"), "profile")
    ET.SubElement(profile, "id").text = "test-enforcement"
    activation = ET.SubElement(ET.SubElement(profile, "activation"), "property")
    ET.SubElement(activation, "name").text = "test.enforcement.enabled"
    ET.SubElement(activation, "value").text = "true"
    ET.SubElement(ET.SubElement(profile, "build"), "plugins").append(enforcer)
    tree.write(tmp_path / "pom.xml", encoding="unicode")
    calls = []
    def run(cmd, repo):
        calls.append(cmd)
        return subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=120)
    result = tooling_status(tmp_path, maven_bin=maven_command()[0],
                            run_maven_command=run, profile_source_cmd=maven_command())
    assert result.version == enforcer_version, result.reason
    assert result.available == (enforcer_version == "1.0.16")
    assert len(calls) == 1 and "help:effective-pom" in calls[0]


def test_missing_obligated_pit_execution_is_not_a_green_build(tmp_path):
    from uta.language.java.maven_compat.launcher import prepare_command, validate_completion
    create_reactor(tmp_path)
    tree = ET.parse(tmp_path / "biz/pom.xml")
    plugins = tree.find("build/plugins")
    pit = next(p for p in plugins if p.findtext("artifactId") == "pitest-maven")
    plugins.remove(pit)
    tree.write(tmp_path / "biz/pom.xml", encoding="unicode")
    cmd = prepare_command(maven_command(), tmp_path)
    result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout[-3000:]
    with pytest.raises(OSError, match="every obligated module"):
        validate_completion(cmd)


def test_old_enforcer_cannot_use_standalone_compatibility(tmp_path):
    from uta.language.java.maven_compat.launcher import prepare_command
    create_reactor(tmp_path, enforcer_version="1.0.13")
    cmd = prepare_command(maven_command(), tmp_path)
    result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=240)
    assert result.returncode != 0
    assert "test-enforcer >= 1.0.16 release required" in result.stdout


def test_filter_diff_after_pit_keeps_module_completion(tmp_path):
    """A module that really ran PIT stays completed when filter-diff runs again.

    test-enforcer re-runs filter-diff late in the same module, which used to
    erase the completion PIT had already recorded and reported a green,
    fully-verified build as an unverified one.
    """
    from uta.language.java.maven_compat.launcher import prepare_command, validate_completion

    create_reactor(tmp_path)
    tree = ET.parse(tmp_path / "biz/pom.xml")
    plugins = tree.find("build/plugins")
    plugin = ET.SubElement(plugins, "plugin")
    ET.SubElement(plugin, "groupId").text = "com.example.build.maven-plugins"
    ET.SubElement(plugin, "artifactId").text = "test-enforcer"
    ET.SubElement(plugin, "version").text = "1.0.16"
    execution = ET.SubElement(ET.SubElement(plugin, "executions"), "execution")
    ET.SubElement(execution, "id").text = "filter-again"
    ET.SubElement(execution, "phase").text = "verify"
    ET.SubElement(ET.SubElement(execution, "goals"), "goal").text = "filter-diff"
    tree.write(tmp_path / "biz/pom.xml", encoding="unicode")

    cmd = prepare_command(maven_command(), tmp_path)
    result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=240)
    (tmp_path / "maven.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-6000:]
    assert result.stdout.count("[uta-pit-compat] verified module=biz") >= 1

    modules = {module["id"].split(":")[1]: module["state"] for module in validate_completion(cmd)["modules"]}
    assert modules["biz"] == "completed", modules


def test_fresh_isolated_pit_report_is_published_for_check_mutation(tmp_path):
    """The released gate must consume this invocation's validated PIT XML.

    UTA isolates PIT reports so stale target output cannot make a run green.
    The check-mutation goal still reads target/pit-reports/mutations.xml, so the
    compatibility extension must replace that path only after validating the
    isolated report produced by the current PIT execution.
    """
    from uta.language.java.maven_compat.launcher import prepare_command

    create_reactor(tmp_path)
    tree = ET.parse(tmp_path / "biz/pom.xml")
    plugins = tree.find("build/plugins")
    enforcer = ET.SubElement(plugins, "plugin")
    ET.SubElement(enforcer, "groupId").text = "com.example.build.maven-plugins"
    ET.SubElement(enforcer, "artifactId").text = "test-enforcer"
    ET.SubElement(enforcer, "version").text = "1.0.16"
    executions = ET.SubElement(enforcer, "executions")
    execution = ET.SubElement(executions, "execution")
    ET.SubElement(execution, "id").text = "check-mutation"
    ET.SubElement(execution, "phase").text = "verify"
    ET.SubElement(ET.SubElement(execution, "goals"), "goal").text = "check-mutation"
    tree.write(tmp_path / "biz/pom.xml", encoding="unicode")

    stale = tmp_path / "biz/target/pit-reports/mutations.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text("<mutations><mutation><mutatedClass>example.StaleTarget</mutatedClass></mutation></mutations>")

    cmd = prepare_command(maven_command(), tmp_path)
    result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=240)
    (tmp_path / "maven.log").write_text(result.stdout + result.stderr)

    assert result.returncode == 0, result.stdout[-6000:]
    assert "diff mutation score 100.00% passed for biz" in result.stdout
    published = ET.parse(stale)
    assert {node.text for node in published.findall(".//mutatedClass")} == {"example.PricingLogic"}
