from pathlib import Path

from uta.enforcement.test_quality import (
    TestQualityFinding,
    aggregate_test_quality,
    read_bounded_test_file,
    summarize_test_quality_payloads,
)
from uta.language.java.test_quality import (
    attach_java_test_quality,
    scan_java_test_quality,
    scan_java_test_quality_evidence,
)
from uta.language.python.test_quality import scan_python_test_quality, scan_python_test_quality_evidence


def test_finding_serializes_to_report_safe_camel_case():
    finding = TestQualityFinding(
        language="python",
        file_path="tests/test_demo.py",
        rule_id="python-weak-assert-not-none",
        category="weak_assertion",
        severity="warning",
        message="Primary assertion only checks non-null/truthiness.",
        line=3,
        evidence="assert result is not None",
    )

    assert finding.to_evidence_dict() == {
        "language": "python",
        "filePath": "tests/test_demo.py",
        "ruleId": "python-weak-assert-not-none",
        "category": "weak_assertion",
        "severity": "warning",
        "message": "Primary assertion only checks non-null/truthiness.",
        "line": 3,
        "evidence": "assert result is not None",
    }


def test_aggregate_test_quality_bounds_warning_count_and_top_rule_ids():
    warnings = [
        TestQualityFinding(
            language="python",
            file_path=f"tests/test_{idx}.py",
            rule_id="python-weak-assert-not-none" if idx % 2 else "python-weak-len-only",
            category="weak_assertion",
            severity="warning",
            message="weak",
            line=idx,
            evidence="assert result",
        )
        for idx in range(8)
    ]

    evidence = aggregate_test_quality(warnings, max_warnings=3)

    assert evidence["warningCount"] == 8
    assert len(evidence["warnings"]) == 3
    assert {item["ruleId"]: item["count"] for item in evidence["topRuleIds"]} == {
        "python-weak-assert-not-none": 4,
        "python-weak-len-only": 4,
    }


def test_summarize_test_quality_payloads_merges_counts_and_top_rules():
    summary = summarize_test_quality_payloads(
        [
            {
                "warningCount": 2,
                "scannerFailureCount": 1,
                "topRuleIds": [{"ruleId": "python-weak-assert-not-none", "count": 2}],
            },
            {
                "warningCount": 3,
                "topRuleIds": [
                    {"ruleId": "python-weak-assert-not-none", "count": 1},
                    {"ruleId": "java-weak-not-null", "count": 2},
                ],
            },
        ]
    )

    assert summary["warningCount"] == 5
    assert summary["scannerFailureCount"] == 1
    assert summary["warnings"] == []
    assert summary["topRuleIds"] == [
        {"ruleId": "python-weak-assert-not-none", "count": 3},
        {"ruleId": "java-weak-not-null", "count": 2},
    ]


def test_read_bounded_test_file_truncates_large_file(tmp_path):
    path = tmp_path / "test_large.py"
    path.write_text("x" * 200_000, encoding="utf-8")

    text = read_bounded_test_file(path, max_bytes=128)

    assert len(text.encode("utf-8")) <= 128


def test_python_scanner_flags_weak_assertions_and_mock_only_tests():
    text = """
def test_returns_result():
    result = service.run()
    assert result is not None

def test_sends_event(mock_bus):
    service.run()
    mock_bus.publish.assert_called_once()
"""

    findings = scan_python_test_quality(Path("tests/test_service.py"), text)
    rule_ids = {finding.rule_id for finding in findings}

    assert "python-weak-assert-not-none" in rule_ids
    assert "python-impl-detail-mock-call-count" in rule_ids


def test_python_scanner_does_not_flag_behavior_value_assertions():
    text = """
def test_calculates_california_tax():
    result = process_order(order(subtotal=100, state="CA"))
    assert result.tax == 7.25
    assert result.total == 107.25

def test_rejects_unknown_state():
    with pytest.raises(UnknownStateError):
        process_order(order(subtotal=100, state="??"))
"""

    findings = scan_python_test_quality(Path("tests/test_order.py"), text)

    assert findings == []


def test_python_scanner_emits_happy_path_hint_when_no_failure_path_tests():
    text = """
def test_calculates_california_tax():
    result = process_order(order(subtotal=100, state="CA"))
    assert result.tax == 7.25
"""

    findings = scan_python_test_quality(Path("tests/test_order.py"), text)

    assert [finding.rule_id for finding in findings] == ["python-happy-path-only-hint"]
    assert findings[0].severity == "info"
    assert findings[0].category == "happy_path_only_hint"


def test_python_scanner_recognizes_try_except_failure_tests():
    text = """
def test_rejects_bad_input():
    try:
        service.run(bad=True)
        pytest.fail("expected bad input to be rejected")
    except ValueError as exc:
        assert "bad input" in str(exc)

def test_retries_on_timeout():
    result = service.run_with_retry(timeout=True)
    assert result.status == "fallback"
"""

    findings = scan_python_test_quality(Path("tests/test_service.py"), text)

    assert findings == []


def test_python_success_status_assertion_still_allows_happy_path_hint():
    text = """
def test_returns_ok_status():
    result = service.run()
    assert result.status == "ok"
"""

    findings = scan_python_test_quality(Path("tests/test_service.py"), text)

    assert [finding.rule_id for finding in findings] == ["python-happy-path-only-hint"]


def test_python_scanner_flags_no_observable_assertion():
    text = """
def test_runs_service():
    service.run()
"""

    findings = scan_python_test_quality(Path("tests/test_service.py"), text)

    assert [finding.rule_id for finding in findings] == ["python-no-observable-assertion"]
    assert "observable behavior" in findings[0].message


def test_python_scanner_flags_smoke_only_no_behavior_context():
    text = """
def test_service_does_not_raise():
    with does_not_raise():
        service.run()
"""

    findings = scan_python_test_quality(Path("tests/test_service.py"), text)

    assert [finding.rule_id for finding in findings] == ["python-smoke-only-no-behavior"]
    assert "only proves execution did not raise" in findings[0].message


def test_python_scanner_handles_class_based_unittest_style_tests():
    text = """
class TestOrderService(unittest.TestCase):
    def test_returns_result(self):
        result = service.run()
        self.assertIsNotNone(result)

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            service.run(bad=True)
"""

    findings = scan_python_test_quality(Path("tests/test_service.py"), text)

    assert [finding.rule_id for finding in findings] == ["python-weak-assert-not-none"]
    assert findings[0].evidence == "self.assertIsNotNone(result)"


def test_python_mirroring_requires_input_derived_arithmetic():
    mirrored = """
def test_total():
    expected = price * quantity
    assert compute_total(price, quantity) == expected

def test_error():
    with pytest.raises(ValueError):
        compute_total(-1, 0)
"""
    explicit = """
def test_total():
    expected = 2 + 3
    assert add(2, 3) == expected

def test_path():
    expected = load("data/x.json")
    assert build_path() == expected

def test_error():
    with pytest.raises(ValueError):
        add(None, 1)
"""

    mirrored_findings = scan_python_test_quality(Path("tests/test_total.py"), mirrored)
    explicit_findings = scan_python_test_quality(Path("tests/test_total.py"), explicit)

    assert [finding.rule_id for finding in mirrored_findings] == ["python-mirroring-formula"]
    assert explicit_findings == []


def test_python_test_quality_evidence_uses_repo_relative_paths(tmp_path):
    repo = tmp_path / "repo"
    test_file = repo / "tests" / "test_service.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        """
def test_returns_result():
    result = service.run()
    assert result is not None
""",
        encoding="utf-8",
    )

    evidence = scan_python_test_quality_evidence(repo, [test_file])

    assert evidence["warningCount"] == 2
    assert evidence["warnings"][0]["filePath"] == "tests/test_service.py"
    assert evidence["warnings"][0]["ruleId"] == "python-weak-assert-not-none"
    assert evidence["warnings"][1]["ruleId"] == "python-happy-path-only-hint"


def test_python_test_quality_evidence_reports_scanner_failures(tmp_path):
    repo = tmp_path / "repo"

    evidence = scan_python_test_quality_evidence(repo, ["tests/missing.py"])

    assert evidence["warningCount"] == 1
    assert evidence["scannerFailureCount"] == 1
    assert evidence["warnings"][0]["ruleId"] == "python-test-quality-scan-failed"


def test_java_scanner_flags_weak_assertions_and_verify_only_tests():
    text = """
public class OrderServiceTest {
  @Test
  public void returnsResult() {
    Result result = service.run();
    assertNotNull(result);
  }

  @Test
  public void sendsEvent() {
    service.run();
    verify(bus).publish(any());
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)
    rule_ids = {finding.rule_id for finding in findings}

    assert "java-weak-not-null" in rule_ids
    assert "java-impl-detail-verify-only" in rule_ids


def test_java_scanner_does_not_flag_value_assertions():
    text = """
public class OrderServiceTest {
  @Test
  public void calculatesCaliforniaTax() {
    OrderResult result = service.process(order);
    assertEquals(new BigDecimal("7.25"), result.getTax());
    assertEquals(new BigDecimal("107.25"), result.getTotal());
  }

  @Test
  public void rejectsUnknownState() {
    assertThrows(UnknownStateException.class, () -> service.process(badOrder));
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert findings == []


def test_java_scanner_emits_happy_path_hint_when_no_failure_path_tests():
    text = """
public class OrderServiceTest {
  @Test
  public void calculatesCaliforniaTax() {
    assertEquals(new BigDecimal("7.25"), service.process(order).getTax());
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert [finding.rule_id for finding in findings] == ["java-happy-path-only-hint"]
    assert findings[0].severity == "info"


def test_java_scanner_recognizes_try_catch_failure_tests():
    text = """
public class OrderServiceTest {
  @Test
  public void rejectsUnknownState() {
    try {
      service.process(badOrder);
      Assert.fail("expected rejection");
    } catch (UnknownStateException ex) {
      assertEquals("unknown state", ex.getMessage());
    }
  }

  @Test
  public void rejectsBlankDate() {
    Result result = service.process(blankDateOrder);
    assertEquals("FAILED", result.getStatus());
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert findings == []


def test_java_scanner_recognizes_assert_false_expected_failure_style():
    text = """
public class OrderServiceTest {
  @Test
  public void invalidDateThrows() {
    try {
      service.process(invalidDateOrder);
      assertFalse(true);
    } catch (IllegalArgumentException ex) {
      assertTrue(ex.getMessage().contains("invalid"));
    }
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert findings == []


def test_java_scanner_preserves_test_expected_exception_support():
    text = """
public class OrderServiceTest {
  @Test(expected = IllegalArgumentException.class)
  public void invalidDateThrows() {
    service.process(invalidDateOrder);
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert findings == []


def test_java_success_status_assertion_still_allows_happy_path_hint():
    text = """
public class OrderServiceTest {
  @Test
  public void returnsOkStatus() {
    Result result = service.run();
    assertEquals("OK", result.getStatus());
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert [finding.rule_id for finding in findings] == ["java-happy-path-only-hint"]


def test_java_scanner_flags_no_observable_assertion():
    text = """
public class OrderServiceTest {
  @Test
  public void runsService() {
    service.run();
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert [finding.rule_id for finding in findings] == ["java-no-observable-assertion"]
    assert "observable behavior" in findings[0].message


def test_java_scanner_flags_smoke_only_assert_does_not_throw():
    text = """
public class OrderServiceTest {
  @Test
  public void serviceDoesNotThrow() {
    assertDoesNotThrow(() -> service.run());
  }
}
"""

    findings = scan_java_test_quality(Path("src/test/java/OrderServiceTest.java"), text)

    assert [finding.rule_id for finding in findings] == ["java-smoke-only-no-behavior"]
    assert "only proves execution did not throw" in findings[0].message


def test_java_mirroring_requires_input_derived_arithmetic():
    mirrored = """
public class TotalTest {
  @Test
  public void computesTotal() {
    long expected = price * quantity;
    assertEquals(expected, service.total(price, quantity));
    assertThrows(IllegalArgumentException.class, () -> service.total(-1, 0));
  }
}
"""
    explicit = """
public class TotalTest {
  @Test
  public void computesTotal() {
    String expected = "a/b";
    assertEquals(expected, service.path());
    long expectedTotal = 2 + 3;
    assertEquals(expectedTotal, service.total(2, 3));
    assertThrows(IllegalArgumentException.class, () -> service.total(-1, 0));
  }
}
"""

    mirrored_findings = scan_java_test_quality(Path("src/test/java/TotalTest.java"), mirrored)
    explicit_findings = scan_java_test_quality(Path("src/test/java/TotalTest.java"), explicit)

    assert [finding.rule_id for finding in mirrored_findings] == ["java-mirroring-formula"]
    assert explicit_findings == []


WEAK_JAVA_TEST = """
public class OrderServiceTest {
  @Test
  public void returnsResult() {
    assertNotNull(service.run());
  }
}
"""


def test_java_evidence_wrapper_scans_existing_file_with_repo_relative_path(tmp_path):
    repo = tmp_path / "repo"
    test_file = repo / "svc" / "src" / "test" / "java" / "OrderServiceTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(WEAK_JAVA_TEST, encoding="utf-8")

    evidence = scan_java_test_quality_evidence(repo, ["svc/src/test/java/OrderServiceTest.java"])

    assert evidence["warningCount"] == 2
    assert evidence["warnings"][0]["filePath"] == "svc/src/test/java/OrderServiceTest.java"
    assert evidence["warnings"][0]["ruleId"] == "java-weak-not-null"
    assert evidence["warnings"][1]["ruleId"] == "java-happy-path-only-hint"


def test_java_evidence_wrapper_skips_missing_files_silently(tmp_path):
    evidence = scan_java_test_quality_evidence(tmp_path, ["svc/src/test/java/NeverCreatedTest.java"])

    assert evidence == {}


def test_attach_java_test_quality_prefers_candidates_and_skips_enriched(tmp_path):
    repo = tmp_path
    discovered = repo / "svc" / "src" / "test" / "java" / "RealTest.java"
    discovered.parent.mkdir(parents=True)
    discovered.write_text(WEAK_JAVA_TEST, encoding="utf-8")
    results = {
        "com.demo.Real": {
            "status": "PASS",
            "test_file_path": "svc/src/test/java/ConventionalTest.java",
            "candidate_test_file_paths": ["svc/src/test/java/RealTest.java"],
        },
        "com.demo.RateLimited": {
            "status": "PROVIDER_RATE_LIMITED",
            "test_file_path": "svc/src/test/java/NeverCreatedTest.java",
        },
        "com.demo.Enriched": {
            "status": "PASS",
            "test_file_path": "svc/src/test/java/RealTest.java",
            "testQuality": {"warningCount": 7},
        },
    }

    attach_java_test_quality(repo, results)

    assert results["com.demo.Real"]["testQuality"]["warningCount"] == 2
    assert "testQuality" not in results["com.demo.RateLimited"]
    assert results["com.demo.Enriched"]["testQuality"] == {"warningCount": 7}
