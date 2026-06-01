import os

import pytest
from uta.maven.jacoco import parse_jacoco_report, parse_jacoco_line_coverage_for_classes, extract_uncovered_clusters, format_uncovered_clusters_markdown
from uta.maven.pitest import (
    parse_pitest_report,
    compute_mutation_stats,
    summarize_surviving_mutants,
    format_mutation_families_markdown,
)

def test_parse_jacoco(fixtures_dir):
    xml_path = os.path.join(fixtures_dir, "jacoco_sample.xml")
    stats = parse_jacoco_report(xml_path, "com.example.service.SampleService")
    
    assert stats["instruction"] == 80.0 # 40 / 50
    assert stats["branch"] == 50.0      # 2 / 4
    assert stats["line"] == (10/15)*100


def test_parse_jacoco_line_coverage_for_classes(fixtures_dir):
    xml_path = os.path.join(fixtures_dir, "jacoco_sample.xml")
    stats = parse_jacoco_line_coverage_for_classes(xml_path, ["com.example.service.SampleService"])
    assert round(stats["line"], 4) == round((10 / 15) * 100, 4)
    assert stats["covered_lines"] == 10
    assert stats["missed_lines"] == 5
    assert stats["matched_classes"] == 1

def test_parse_pitest(fixtures_dir):
    xml_path = os.path.join(fixtures_dir, "pitest_sample.xml")
    survivors = parse_pitest_report(xml_path, "com.example.service.SampleService")
    
    assert len(survivors) == 1
    s = survivors[0]
    assert s["line"] == 15
    assert s["method"] == ""
    assert "ConditionalsBoundaryMutator" in s["mutation_type"]


def test_compute_mutation_stats_from_fixture(fixtures_dir):
    """Mutation gate uses ``compute_mutation_stats`` on Pitest XML (1 killed, 1 survived)."""
    xml_path = os.path.join(fixtures_dir, "pitest_sample.xml")
    stats = compute_mutation_stats(xml_path, "com.example.service.SampleService")
    assert stats["total"] == 2
    assert stats["killed"] == 1
    assert stats["survived"] == 1
    assert stats["score"] == 50.0
    assert stats["status_counts"]["KILLED"] == 1
    assert stats["status_counts"]["SURVIVED"] == 1


def test_summarize_surviving_mutants_groups_and_formats(fixtures_dir):
    xml_path = os.path.join(fixtures_dir, "pitest_sample.xml")
    families = summarize_surviving_mutants(xml_path, "com.example.service.SampleService")

    assert len(families) == 1
    assert families[0]["family"] == "boundary"
    assert families[0]["method"] == "(unknown)"
    assert families[0]["count"] == 1
    rendered = format_mutation_families_markdown(families)
    assert "`boundary`" in rendered
    assert "`(unknown)`" in rendered
    assert "line 15" in rendered


def test_summarize_surviving_mutants_ranks_by_method_count_and_deprioritizes_metrics(tmp_path):
    xml_path = tmp_path / "mutations.xml"
    xml_path.write_text(
        """<mutations>
  <mutation status="SURVIVED">
    <mutatedClass>com.example.service.SampleService</mutatedClass>
    <mutatedMethod>queryInventory</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.ReturnValsMutator</mutator>
    <lineNumber>15</lineNumber>
    <description>replaced return value with null</description>
  </mutation>
  <mutation status="SURVIVED">
    <mutatedClass>com.example.service.SampleService</mutatedClass>
    <mutatedMethod>queryInventory</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator</mutator>
    <lineNumber>18</lineNumber>
    <description>changed conditional boundary</description>
  </mutation>
  <mutation status="SURVIVED">
    <mutatedClass>com.example.service.SampleService</mutatedClass>
    <mutatedMethod>metricsCounter</mutatedMethod>
    <mutator>org.pitest.mutationtest.engine.gregor.mutators.VoidMethodCallMutator</mutator>
    <lineNumber>90</lineNumber>
    <description>removed call to io.micrometer.core.instrument.Counter::increment</description>
  </mutation>
</mutations>""",
        encoding="utf-8",
    )

    families = summarize_surviving_mutants(str(xml_path), "com.example.service.SampleService", max_families=5)

    assert len(families) == 3
    assert families[0]["method"] == "queryInventory"
    assert families[0]["count"] == 1
    assert families[0]["deprioritized"] is False
    assert families[-1]["method"] == "metricsCounter"
    assert families[-1]["deprioritized"] is True
    assert families[-1]["killability"] == "low"

    rendered = format_mutation_families_markdown(families)
    assert "deprioritize unless there is an easy seam" in rendered


def test_extract_uncovered_clusters_and_format(tmp_path):
    xml_path = tmp_path / "jacoco.xml"
    xml_path.write_text(
        """<report name="demo">
  <package name="com/example/service">
    <class name="com/example/service/SampleService" sourcefilename="SampleService.java">
      <method name="doWork" desc="()V">
        <counter type="LINE" missed="3" covered="2"/>
        <counter type="BRANCH" missed="2" covered="1"/>
      </method>
      <method name="cheap" desc="()V">
        <counter type="LINE" missed="1" covered="4"/>
        <counter type="BRANCH" missed="0" covered="0"/>
      </method>
    </class>
    <sourcefile name="SampleService.java">
      <line nr="10" mi="1" ci="0" mb="0" cb="0"/>
      <line nr="11" mi="1" ci="0" mb="0" cb="0"/>
      <line nr="14" mi="1" ci="0" mb="0" cb="0"/>
      <line nr="30" mi="0" ci="1" mb="1" cb="0"/>
    </sourcefile>
  </package>
</report>""",
        encoding="utf-8",
    )

    summary = extract_uncovered_clusters(str(xml_path), "com.example.service.SampleService")

    assert summary["methods"][0]["name"] == "doWork"
    assert summary["methods"][0]["missed_line"] == 3
    assert summary["line_clusters"][0]["start"] == 10
    assert summary["line_clusters"][0]["end"] == 11
    rendered = format_uncovered_clusters_markdown(summary)
    assert "Methods with missed coverage:" in rendered
    assert "Uncovered source line clusters:" in rendered
    assert "lines 10-11" in rendered


def test_run_pitest_builds_maven_argv(monkeypatch):
    """Guard: Pitest invocation must pass target class and test FQN to Maven."""
    captured = {}

    def fake_run(cmd, cwd, capture_output, timeout, env=None):
        captured["cmd"] = cmd
        captured["env"] = env or {}
        class R:
            returncode = 0
            stdout = b"ok"
            stderr = b""

        return R()

    monkeypatch.setattr("uta.maven.pitest.subprocess.run", fake_run)
    from uta.maven.pitest import run_pitest

    ok, out = run_pitest("/repo", "com.foo.Bar", "com.foo.BarTest", "biz")
    assert ok
    cmd = captured["cmd"]
    assert "org.pitest:pitest-maven" in " ".join(cmd)
    assert "-DtargetClasses=com.foo.Bar" in cmd
    assert "-DtargetTests=com.foo.BarTest" in cmd
    assert "-DfailWhenNoMutations=false" in cmd
    assert "-DskipTests=false" in cmd
    assert "-Dmaven.test.skip=false" in cmd
    assert "-DjvmArgs=-Djava.net.preferIPv6Addresses=true -Djava.net.preferIPv4Stack=false" in cmd
    assert "-Djava.net.preferIPv6Addresses=true" in captured["env"]["MAVEN_OPTS"]
    assert "-pl" in cmd and "biz" in cmd


def test_parse_pitest_green_suite_failure_extracts_test_and_assertion():
    from uta.maven.pitest import parse_pitest_green_suite_failure, format_pitest_green_suite_failure

    output = """
10:48:12 PM PIT >> INFO : Created 1 mutation test units in pre scan
Description [testClass=com.example.service.PickingServiceTest, name=finishedShouldCreateZeroQtyFlowForUnfinishedDetailsAndUpdateMainFields]
java.lang.AssertionError: expected:<5.000000> but was:<5>
1 tests did not pass without mutation when calculating line coverage. Mutation testing requires a green suite.
"""

    details = parse_pitest_green_suite_failure(output)

    assert details is not None
    assert details["failing_test_count"] == 1
    assert details["test_class"] == "com.example.service.PickingServiceTest"
    assert details["test_method"] == "finishedShouldCreateZeroQtyFlowForUnfinishedDetailsAndUpdateMainFields"
    rendered = format_pitest_green_suite_failure(details)
    assert "mutation testing requires a green suite" in rendered.lower()
    assert "finishedShouldCreateZeroQtyFlowForUnfinishedDetailsAndUpdateMainFields" in rendered


def test_run_pitest_prefers_green_suite_summary_over_truncated_tail(monkeypatch):
    def fake_run(cmd, cwd, capture_output, timeout, env=None):
        class R:
            returncode = 1
            stdout = (
                b"Description [testClass=com.example.FooTest, name=shouldStayGreen]\\n"
                b"java.lang.AssertionError: expected:<5.000000> but was:<5>\\n"
                b"1 tests did not pass without mutation when calculating line coverage. "
                b"Mutation testing requires a green suite.\\n"
            )
            stderr = b""

        return R()

    monkeypatch.setattr("uta.maven.pitest.subprocess.run", fake_run)

    from uta.maven.pitest import run_pitest

    ok, out = run_pitest("/repo", "com.foo.Bar", "com.foo.BarTest", "biz")

    assert ok is False
    assert "green suite" in out
    assert "com.example.FooTest.shouldStayGreen" in out


def test_run_test_with_jacoco_builds_maven_argv(monkeypatch, tmp_path):
    captured = {}

    agent_dir = tmp_path / ".m2" / "repository" / "org" / "jacoco" / "org.jacoco.agent" / "0.8.12"
    agent_dir.mkdir(parents=True)
    (agent_dir / "org.jacoco.agent-0.8.12-runtime.jar").write_text("jar")

    def fake_run(cmd, cwd, capture_output, timeout):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = b"Tests run: 1, Failures: 0, Errors: 0, Skipped: 0"
            stderr = b""
        return R()

    monkeypatch.setattr("uta.maven.jacoco.subprocess.run", fake_run)
    monkeypatch.setattr("uta.maven.jacoco.Path.home", lambda: tmp_path)
    from uta.maven.jacoco import run_test_with_jacoco

    ok, _ = run_test_with_jacoco("/repo", "BarTest", "biz")
    assert ok
    cmd = captured["cmd"]
    assert "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent" in cmd
    assert "test" in cmd
    assert "org.jacoco:jacoco-maven-plugin:0.8.12:report" in cmd
    assert "-Dtest=BarTest" in cmd
    assert "-DskipTests=false" in cmd
    assert "-Dmaven.test.skip=false" in cmd


def test_run_tests_with_jacoco_batch_builds_maven_argv(monkeypatch, tmp_path):
    captured = {}

    agent_dir = tmp_path / ".m2" / "repository" / "org" / "jacoco" / "org.jacoco.agent" / "0.8.12"
    agent_dir.mkdir(parents=True)
    (agent_dir / "org.jacoco.agent-0.8.12-runtime.jar").write_text("jar")

    def fake_run(cmd, cwd, capture_output, timeout):
        captured["cmd"] = cmd
        class R:
            returncode = 0
            stdout = b"Tests run: 2, Failures: 0, Errors: 0, Skipped: 0"
            stderr = b""
        return R()

    monkeypatch.setattr("uta.maven.jacoco.subprocess.run", fake_run)
    monkeypatch.setattr("uta.maven.jacoco.Path.home", lambda: tmp_path)
    from uta.maven.jacoco import run_tests_with_jacoco_batch

    ok, _ = run_tests_with_jacoco_batch("/repo", ["BarTest", "BazTest"], "biz")
    assert ok
    cmd = captured["cmd"]
    assert "-Dtest=BarTest,BazTest" in cmd
    assert "-DskipTests=false" in cmd
    assert "-Dmaven.test.skip=false" in cmd


def test_run_test_with_jacoco_deletes_stale_xml_before_run(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    module_dir = repo / "biz"
    jacoco_dir = module_dir / "target" / "site" / "jacoco"
    jacoco_dir.mkdir(parents=True)
    stale_xml = jacoco_dir / "jacoco.xml"
    stale_xml.write_text("<report/>", encoding="utf-8")

    agent_dir = tmp_path / ".m2" / "repository" / "org" / "jacoco" / "org.jacoco.agent" / "0.8.12"
    agent_dir.mkdir(parents=True)
    (agent_dir / "org.jacoco.agent-0.8.12-runtime.jar").write_text("jar")

    def fake_run(cmd, cwd, capture_output, timeout):
        assert not stale_xml.exists()
        fresh_dir = module_dir / "target" / "site" / "jacoco"
        fresh_dir.mkdir(parents=True, exist_ok=True)
        (fresh_dir / "jacoco.xml").write_text("<report/>", encoding="utf-8")
        class R:
            returncode = 0
            stdout = b"Tests run: 1, Failures: 0, Errors: 0, Skipped: 0"
            stderr = b""
        return R()

    monkeypatch.setattr("uta.maven.jacoco.subprocess.run", fake_run)
    monkeypatch.setattr("uta.maven.jacoco.Path.home", lambda: tmp_path)
    from uta.maven.jacoco import run_test_with_jacoco

    ok, _ = run_test_with_jacoco(str(repo), "BarTest", "biz")
    assert ok


def test_parse_surefire_results_per_test_class(tmp_path):
    reports = tmp_path / "biz" / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-com.example.PassTest.xml").write_text(
        """<testsuite tests="1" failures="0" errors="0" skipped="0">
<testcase classname="com.example.PassTest" name="ok"/>
</testsuite>""",
        encoding="utf-8",
    )
    (reports / "TEST-com.example.FailTest.xml").write_text(
        """<testsuite tests="1" failures="0" errors="1" skipped="0">
<testcase classname="com.example.FailTest" name="boom">
<error message="broken">stack line 1
stack line 2</error>
</testcase>
</testsuite>""",
        encoding="utf-8",
    )

    from uta.maven.jacoco import parse_surefire_results

    results = parse_surefire_results(str(tmp_path), ["PassTest", "FailTest"], "biz")
    assert results["PassTest"]["passed"] is True
    assert results["FailTest"]["passed"] is False
    assert "broken" in results["FailTest"]["output"]
