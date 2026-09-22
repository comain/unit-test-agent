"""Java-specific CI failure-diagnostics & evidence interpretation.

Counterpart to ``uta/language/python/ci_evidence.py``: the language-agnostic CI
report layer (``uta.app.reporting``) delegates Java / Maven / JaCoCo / PIT
specific evidence interpretation here so it only dispatches by language. The
metric *number* parsing (coverage/mutation rates from Maven text vs structured
target results) still lives in the report layer because the structured path is
shared with Python; only the Java-only diagnostics/requirements live here.
"""

from __future__ import annotations

import re
from itertools import islice
from typing import Any, Dict

from uta.language.java.compile import classify_compile_errors
from uta.language.java.enforcement_runner import MISSING_EVIDENCE_SUMMARY
from uta.language.java.enforcement_runner.parsing import (
    GATE_MISS_SUMMARY_MAX_CHARS,
    bounded_reasons,
    coverage_gate_miss_reasons,
    mutation_gate_miss_reasons,
)
from uta.language.java.enforcement_versions import (
    PARENT_ROOT_VERSION,
    TEST_ENFORCER_VERSION,
    PARENT_GENERIC_VERSION,
)


def _enforcement_failed(enforcement: Dict[str, Any]) -> bool:
    """True unless the run is a clean pass.

    Absent/unknown fields fall through to ``True`` so a genuinely broken run
    still explains itself; only an explicit pass suppresses the diagnostics.
    """
    if enforcement.get("passed") is True:
        return False
    if str(enforcement.get("status") or "") == "passed":
        return False
    returncode = enforcement.get("returncode")
    if isinstance(returncode, int) and returncode == 0 and enforcement.get("passed") is None:
        return False
    return True


def java_failure_diagnostics(
    enforcement: Dict[str, Any],
    evidence: Dict[str, Any],
) -> list[Dict[str, Any]]:
    """JaCoCo-missing / PIT-baseline diagnostics from a Maven enforcement run."""
    if not _enforcement_failed(enforcement):
        # These diagnostics are rendered under "失败原因". A run that Maven
        # exited 0 on and the enforcer passed has no failure to explain, so any
        # match here is a false positive on log text, not a verdict.
        return []
    output = f"{enforcement.get('stdout') or ''}\n{enforcement.get('stderr') or ''}"
    diagnostics: list[Dict[str, Any]] = []

    compile_failure = _java_test_compile_failure_detail(output)
    if compile_failure:
        diagnostics.append(compile_failure)

    pit_baseline_failure = pit_baseline_failure_detail(output)
    if pit_baseline_failure:
        diagnostics.append(pit_baseline_failure)

    jacoco_match = re.search(r"JaCoCo XML not found at\s+([^;\n]+);", output)
    if jacoco_match:
        jacoco_path = jacoco_match.group(1).strip()
        project_match = re.search(r"on project ([^:]+):.*?JaCoCo XML not found", output, flags=re.DOTALL)
        filtered_targets = _string_list(evidence.get("filteredTargetClasses"))
        target_tests = _string_list(evidence.get("targetTests"))
        scoped_modules = _scoped_maven_modules(enforcement, evidence)
        target_test_modules = _target_test_modules(evidence, target_tests)
        tests_skipped = _looks_tests_skipped(output)
        target_mismatch = _target_tests_look_unrelated(filtered_targets, target_tests)
        target_test_module_mismatch = bool(
            target_tests
            and scoped_modules
            and target_test_modules
            and any(module not in scoped_modules for module in target_test_modules)
        )
        if tests_skipped:
            title = "JaCoCo coverage report missing because tests were skipped"
            message = (
                "Changed Java source lines were found in a module, but Maven/Surefire skipped "
                "test execution, so JaCoCo had no execution data and could not write XML before "
                "test-enforcer checked coverage."
            )
            hint = (
                "Check module/profile-level Maven test skip settings such as surefire skip, "
                "skipTests, maven.test.skip, or inherited profile properties. The selected test "
                "must actually run in the changed module before JaCoCo can generate coverage."
            )
        elif _looks_jacoco_agent_missing(output):
            title = "JaCoCo coverage agent did not run"
            message = (
                "The selected Maven/Surefire tests ran, but JaCoCo reported missing execution "
                "data, so no coverage XML was generated before test-enforcer checked coverage."
            )
            hint = (
                "Check Surefire argLine wiring. When combining JaCoCo with another javaagent "
                "such as Testable, keep JaCoCo's late-injected agent by using Maven late "
                "property evaluation, for example <argLine>@{argLine} -javaagent:...testable-agent...</argLine>. "
                "Do not use ${argLine} for this case because it can be interpolated before "
                "jacoco:prepare-agent sets the agent."
            )
        elif target_test_module_mismatch:
            title = "Selected test is outside Maven-scoped target modules"
            message = (
                "Changed Java source lines were found in Maven-scoped modules, but the "
                "selected target test is located in another Maven module. That test file "
                "exists, but it is outside the current -pl verification scope, so it cannot "
                "produce JaCoCo XML for the filtered target module."
            )
            hint = (
                "Move or create the target-specific unit test in the same Maven module as "
                "the filtered target class, or run a Maven project selector that includes "
                "the test module and still generates JaCoCo XML for the target module. "
                "For UTA repair, prefer generating the test under the target source module "
                "instead of reusing a provider/integration-module test."
            )
        else:
            title = "JaCoCo coverage report missing"
            message = (
                "Changed Java source lines were found in a module, but Maven did not "
                "produce JaCoCo XML for that module before test-enforcer checked coverage."
            )
            if target_tests and scoped_modules:
                hint = (
                    "The selected target test must execute within the Maven-scoped modules "
                    "and produce coverage in the module that contains the Maven-filtered target "
                    "class. If the existing test file is in another module, such as a provider "
                    "or integration module outside the current -pl selector, move/create a "
                    "target-specific unit test in the same Maven module as the filtered target "
                    "class. Otherwise remove/move non-production source from src/main/java."
                )
            else:
                hint = (
                    "The selected target test must produce coverage in the module that contains "
                    "the Maven-filtered target class; otherwise add/generate a test in that module "
                    "or remove/move non-production source from src/main/java."
                )
        diagnostics.append(
            {
                "type": "missing_jacoco_xml",
                "title": title,
                "message": message,
                "module": _module_from_jacoco_path(jacoco_path),
                "project": project_match.group(1).strip() if project_match else "",
                "jacocoPath": jacoco_path,
                "filteredTargetClasses": filtered_targets,
                "targetTests": target_tests,
                "scopedModules": scoped_modules,
                "targetTestModules": target_test_modules,
                "changedClasses": _string_list(evidence.get("changedClasses")),
                "testsSkipped": tests_skipped,
                "targetMismatch": target_mismatch,
                "targetTestModuleMismatch": target_test_module_mismatch,
                "hint": hint,
            }
        )
    return diagnostics


def _java_test_compile_failure_detail(output: str) -> Dict[str, Any] | None:
    errors = [
        error
        for error in classify_compile_errors(output)
        if "/src/test/java/" in str(error.file).replace("\\", "/")
        or str(error.file).replace("\\", "/").startswith("src/test/java/")
    ]
    if not errors:
        return None
    paths = list(dict.fromkeys(_display_test_compile_path(error.file) for error in errors))
    first = errors[0]
    excerpt_parts = [first.message, *first.detail]
    excerpt = "; ".join(part.strip() for part in excerpt_parts if part.strip())
    return {
        "type": "java_test_compile_failed",
        "title": "Selected Maven scope contains a test compilation blocker",
        "message": (
            "Maven stopped during testCompile before coverage or mutation verification. "
            "The blocking test must compile before UTA can evaluate the selected targets."
        ),
        "failingTestPaths": paths,
        "errorExcerpt": excerpt or "Java test compilation failed.",
        "hint": (
            "Update or remove the stale test API usage shown above, then rerun enforcement. "
            "If the blocker belongs to another repair target, repair that owning test instead "
            "of changing the currently active target test."
        ),
    }


def _display_test_compile_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/")
    marker = "/src/test/java/"
    if marker not in normalized:
        return normalized.lstrip("/")
    prefix, suffix = normalized.rsplit(marker, 1)
    module = prefix.rstrip("/").rsplit("/", 1)[-1]
    if not module or module in {"workspace", "repo", "worktree"}:
        return f"src/test/java/{suffix}"
    return f"{module}/src/test/java/{suffix}"


def should_merge_maven_output_evidence(enforcement: Dict[str, Any], output: str) -> bool:
    return (
        str(enforcement.get("backend") or "") == "maven_enforcer"
        or str(enforcement.get("language") or "") == "java"
        or "test-enforcer" in output.lower()
    )


def pit_baseline_failure_detail(output: str) -> Dict[str, Any] | None:
    if "Mutation testing requires a green suite" not in output and "did not pass without mutation" not in output:
        return None

    project_match = re.search(
        r"on project ([^:]+):.*?(?:Mutation testing requires a green suite|did not pass without mutation)",
        output,
        flags=re.DOTALL,
    )
    failing_tests = _pit_failing_test_lines(output)
    return {
        "type": "pit_baseline_not_green",
        "title": "PIT baseline tests failed before mutation scoring completed",
        "message": (
            "PIT could not calculate a valid mutation score because at least one selected "
            "baseline test failed before mutation execution. Any earlier PIT numbers in the "
            "Maven log are partial module diagnostics, not the final diff mutation gate result."
        ),
        "project": project_match.group(1).strip() if project_match else "",
        "failingTests": failing_tests,
        "errorExcerpt": failing_tests[0] if failing_tests else "Mutation testing requires a green suite.",
        "hint": "Fix the failing baseline test first; then rerun UTA test-enforcement to calculate mutation score.",
    }


def test_enforcement_requirements(enforcement: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not enforcement:
        return None
    summary = str(enforcement.get("summary") or "")
    detected = _detected_maven_tooling(enforcement)
    tooling_unavailable = isinstance(detected, dict) and detected.get("available") is False
    missing_evidence = enforcement.get("status") == "missing_evidence" and summary.startswith(
        MISSING_EVIDENCE_SUMMARY.split(":", 1)[0]
    )
    if not missing_evidence and not tooling_unavailable:
        return None
    return {
        "summary": MISSING_EVIDENCE_SUMMARY,
        "detected": detected,
        "requiredPlugin": f"test-enforcer >= {TEST_ENFORCER_VERSION}",
        "inheritancePaths": [
            {
                "title": "如果项目继承 example-root",
                "instruction": f"升级父 POM 到 example-root {PARENT_ROOT_VERSION} 或更高版本；该父链会通过 example-parent-generic 引入 test-enforcement profile 和 test-enforcer。",
                "snippet": f"<parent>\n  <groupId>com.example.platform</groupId>\n  <artifactId>example-root</artifactId>\n  <version>{PARENT_ROOT_VERSION}</version>\n</parent>",
            },
            {
                "title": "如果项目直接继承 example-parent-generic",
                "instruction": f"升级父 POM 到 example-parent-generic {PARENT_GENERIC_VERSION} 或更高版本；不要只在命令里加 test.enforcement.enabled，父 POM/profile 必须能解析出 test-enforcer。",
                "snippet": f"<parent>\n  <groupId>com.example.common</groupId>\n  <artifactId>example-parent-generic</artifactId>\n  <version>{PARENT_GENERIC_VERSION}</version>\n</parent>",
            },
        ],
        "fallback": {
            "title": "无法升级父 POM 时的兜底方案",
            "instruction": (
                "在项目自己的 test-enforcement profile 中直接引入已发布的 test-enforcer。"
                "同时必须把 JaCoCo 和 PIT 绑定到 Maven 生命周期：test 前执行 jacoco:prepare-agent，"
                "test 后生成 jacoco:report，verify 阶段执行 pitest:mutationCoverage，"
                "并保留 test-enforcer 的 filter-diff/check-coverage/check-mutation 配置。"
                "UTA/RDC 需要同时看到 diff coverage 和 check-mutation 输出的显式 changed-line mutation verdict；"
                "原始 PIT Test strength 仅用于诊断，不能作为门禁结果。"
                f"UTA 只把 Maven effective-pom 中 active build plugin 里的 test-enforcer >= {TEST_ENFORCER_VERSION} 视为可修复证据。"
            ),
            "snippet": (
                "<profile>\n"
                "  <id>test-enforcement</id>\n"
                "  <activation><property><name>test.enforcement.enabled</name><value>true</value></property></activation>\n"
                "  <build><plugins>\n"
                "    <!-- jacoco-maven-plugin: prepare-agent before test, report after test -->\n"
                "    <!-- pitest-maven: mutationCoverage in verify, scoped by test-enforcer filter-diff properties -->\n"
                "    <!-- PIT emits XML; check-mutation applies the changed-line mutation gate -->\n"
                "    <plugin>\n"
                "      <groupId><!-- released UTA plugin groupId --></groupId>\n"
                "      <artifactId>test-enforcer</artifactId>\n"
                f"      <version>{TEST_ENFORCER_VERSION}</version>\n"
                "      <!-- executions: filter-diff, check-coverage, check-mutation -->\n"
                "    </plugin>\n"
                "  </plugins></build>\n"
                "</profile>"
            ),
        },
        "versions": [
            f"required resolved build plugin: test-enforcer >= {TEST_ENFORCER_VERSION}",
            f"normal parent rollout: example-parent-generic >= {PARENT_GENERIC_VERSION}",
            f"Platform-family parent rollout: example-root >= {PARENT_ROOT_VERSION}",
        ],
        "usageGuide": "../../docs/test-enforce-usage.md",
    }


def output_evidence_detail(output: str) -> Dict[str, Any]:
    """Coverage/mutation metrics parsed from raw Maven/PIT test-enforcer output."""
    mutation_blocked = pit_baseline_failure_detail(output)
    if mutation_blocked:
        return {
            "coverage": _coverage_detail(output),
            "mutation": None,
            "pitMutation": None,
            "mutationBlocked": mutation_blocked,
        }
    mutation = _mutation_detail(output, allow_pit_fallback=False)
    pit_mutation = None
    if not mutation:
        pit_mutation = _mutation_detail(output, allow_pit_fallback=True)
    return {
        "coverage": _coverage_detail(output),
        "mutation": mutation,
        "pitMutation": pit_mutation,
    }


def missing_pom_warnings(output: str) -> list[str]:
    """Return bounded Maven warnings for absent descriptors, not transfer failures."""
    return [
        match.group(0)
        for match in islice(
            re.finditer(
                r"(?m)^\[WARNING\] The POM for .+ is missing, no dependency information available$",
                output,
            ),
            10,
        )
    ]


def java_gate_failure_summary(
    result: Dict[str, Any], *, max_chars: int = GATE_MISS_SUMMARY_MAX_CHARS
) -> str | None:
    """Return a concise per-target reason for a failed Maven test-enforcement gate."""
    if result.get("passed") is True:
        return None
    output = f"{result.get('summary') or ''}\n{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
    reasons: list[str] = []
    reasons.extend(coverage_gate_miss_reasons(output))
    reasons.extend(mutation_gate_miss_reasons(output))

    blocked = pit_baseline_failure_detail(output)
    if blocked:
        excerpt = str(blocked.get("errorExcerpt") or "").strip()
        reasons.append(
            "PIT baseline failed before mutation scoring"
            + (f": {excerpt}" if excerpt else "")
        )

    test_failure = _surefire_failure_summary(output)
    if test_failure:
        reasons.append(test_failure)

    if not reasons:
        summary = str(result.get("summary") or "").strip()
        if summary:
            reasons.append(summary)
    if not reasons:
        return None
    return bounded_reasons(
        list(dict.fromkeys(reason for reason in reasons if reason)), max_chars
    )


def _surefire_failure_summary(output: str) -> str | None:
    matches = list(
        re.finditer(
            r"Tests run:\s*\d+,\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*\d+",
            output,
            flags=re.IGNORECASE,
        )
    )
    if not matches:
        return None
    failures = sum(int(match.group(1)) for match in matches)
    errors = sum(int(match.group(2)) for match in matches)
    if failures + errors <= 0:
        return None
    failing_tests = _surefire_failing_test_names(output)
    issue_parts = []
    if failures:
        issue_parts.append(f"{failures} failures")
    if errors:
        issue_parts.append(f"{errors} errors")
    message = "Selected test run failed: " + ", ".join(issue_parts)
    if failing_tests:
        message += " (" + ", ".join(failing_tests[:4])
        if len(failing_tests) > 4:
            message += f", +{len(failing_tests) - 4} more"
        message += ")"
    return message


def _surefire_failing_test_names(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        stripped = re.sub(r"^\s*(?:\[ERROR\]\s*)+", "", line).strip()
        match = re.match(r"([A-Za-z_$][\w$]*(?:Test|Tests|TestManual|IT))(?:[.:]\w+)?", stripped)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _coverage_detail(output: str) -> Dict[str, Any] | None:
    matches = re.findall(
        r"diff line coverage\s+([0-9]+(?:\.[0-9]+)?)%\s+.*?\((\d+)/(\d+)\)",
        output,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None
    covered = sum(int(match[1]) for match in matches)
    total = sum(int(match[2]) for match in matches)
    rate = (covered / total * 100) if total else 0.0
    return {
        "rate": rate,
        "formattedRate": f"{rate:.2f}%",
        "covered": covered,
        "total": total,
        "modules": len(matches),
    }


def _mutation_detail(output: str, *, allow_pit_fallback: bool = True) -> Dict[str, Any] | None:
    diff_matches = re.findall(
        r"diff mutation score\s+([0-9]+(?:\.[0-9]+)?)%\s+(?:passed\s+)?for\s+[^\s]+\s+"
        r"\((\d+)/(\d+)\s+detected;\s+\d+\s+survived;\s+\d+\s+no\s+coverage\s+excluded\)",
        output,
        flags=re.IGNORECASE,
    )
    if diff_matches:
        detected = sum(int(match[1]) for match in diff_matches)
        total = sum(int(match[2]) for match in diff_matches)
        rate = (detected / total * 100) if total else 0.0
        return {
            "rate": rate,
            "formattedRate": f"{rate:.2f}%",
            "killed": detected,
            "generated": total,
            "modules": len(diff_matches),
            "source": "diff",
        }

    if not allow_pit_fallback:
        return None

    matches = re.findall(
        r"Generated\s+(\d+)\s+mutations\s+Killed\s+(\d+)\s+\(([0-9]+(?:\.[0-9]+)?)%\)",
        output,
        flags=re.IGNORECASE,
    )
    if not matches:
        matches = re.findall(
            r"PIT\s+generated=(\d+)\s+killed=(\d+).*?test-strength=([0-9]+(?:\.[0-9]+)?)%",
            output,
            flags=re.IGNORECASE,
        )
    if not matches:
        return None
    raw_generated = sum(int(match[0]) for match in matches)
    killed = sum(int(match[1]) for match in matches)
    no_coverage_matches = re.findall(
        r"Mutations with no coverage\s+(\d+)\.\s+Test strength\s+[0-9]+(?:\.[0-9]+)?%",
        output,
        flags=re.IGNORECASE,
    )
    no_coverage = sum(int(value) for value in no_coverage_matches)
    # Coverage is gated separately. For PIT fallback display, match PIT Test Strength:
    # only mutants reached by tests belong in the mutation-strength denominator.
    generated = max(raw_generated - no_coverage, 0) if no_coverage_matches else raw_generated
    strength_matches = re.findall(r"Test strength\s+([0-9]+(?:\.[0-9]+)?)%", output, flags=re.IGNORECASE)
    rate = (killed / generated * 100) if generated else 0.0
    detail = {
        "rate": rate,
        "formattedRate": f"{rate:.2f}%",
        "killed": killed,
        "generated": generated,
        "modules": len(matches),
        "source": "pit",
    }
    if no_coverage_matches:
        detail["rawGenerated"] = raw_generated
        detail["noCoverage"] = no_coverage
    if strength_matches:
        detail["testStrength"] = float(strength_matches[-1])
        detail["formattedTestStrength"] = f"{float(strength_matches[-1]):.2f}%"
    return detail


def _pit_failing_test_lines(output: str) -> list[str]:
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line in output.splitlines():
        error_record = re.match(r"^\s*(?:\[ERROR\]\s*)+", line) is not None
        stripped = re.sub(r"^\s*(?:\[ERROR\]\s*)+", "", line).strip()
        if not stripped:
            continue
        description = re.search(
            r"Description\s*\[testClass=([^,\]]+),\s*name=([^,\]]+)", stripped
        )
        if description:
            method = description.group(2).strip().split("(", 1)[0]
            candidate = f"{description.group(1).strip()}.{method}"
        elif error_record and not stripped.startswith("at "):
            # Surefire's explicit failure header and summary are authoritative.
            # Ordinary stack frames can belong to expected exceptions logged by
            # a passing test and must never be presented as baseline failures.
            match = re.match(
                r"((?:[\w$]+\.)*[\w$]+Test\.[\w$<>]+)(?::\d+)?(?:\s|$)", stripped
            )
            candidate = match.group(1) if match else ""
        else:
            candidate = ""
        identity_match = re.match(
            r"(?:[\w$]+\.)*([\w$]+Test)\.([\w$<>]+)", candidate
        )
        identity = identity_match.groups() if identity_match else (candidate, "")
        if candidate and identity not in seen:
            seen.add(identity)
            failures.append(candidate)
        if len(failures) >= 5:
            break
    return failures


def _looks_jacoco_agent_missing(output: str) -> bool:
    lowered = output.lower()
    return (
        "skipping jacoco execution due to missing execution data file" in lowered
        and re.search(r"\btests run:\s*\d+", output, flags=re.IGNORECASE) is not None
    )


def _looks_tests_skipped(output: str) -> bool:
    lowered = output.lower()
    return (
        "tests are skipped." in lowered
        or "skipping execution of surefire because it has been configured to skip tests" in lowered
        or "not compiling test sources" in lowered
    )


def _target_tests_look_unrelated(filtered_targets: list[str], target_tests: list[str]) -> bool:
    if not filtered_targets or not target_tests:
        return False

    target_simple_names = {
        filtered_target.rsplit(".", 1)[-1].removesuffix("*")
        for filtered_target in filtered_targets
        if filtered_target
    }
    test_simple_names = {
        target_test.rsplit(".", 1)[-1]
        for target_test in target_tests
        if target_test
    }
    if not target_simple_names or not test_simple_names:
        return False

    return any(
        not any(target_simple and target_simple in test_simple for test_simple in test_simple_names)
        for target_simple in target_simple_names
    )


def _module_from_jacoco_path(path_text: str) -> str:
    marker = "/target/site/jacoco/jacoco.xml"
    if marker not in path_text:
        return ""
    module_path = path_text.split(marker, 1)[0].rstrip("/")
    return module_path.rsplit("/", 1)[-1] if module_path else ""


def _scoped_maven_modules(enforcement: Dict[str, Any], evidence: Dict[str, Any]) -> list[str]:
    modules = _string_list(evidence.get("changedModules"))
    if modules:
        return modules
    command = enforcement.get("command")
    if not isinstance(command, (list, tuple)):
        return []
    parsed: list[str] = []
    for index, item in enumerate(command):
        text = str(item)
        if text == "-pl" and index + 1 < len(command):
            parsed.extend(_split_maven_modules(str(command[index + 1])))
        elif text.startswith("-pl="):
            parsed.extend(_split_maven_modules(text.split("=", 1)[1]))
        elif text == "--projects" and index + 1 < len(command):
            parsed.extend(_split_maven_modules(str(command[index + 1])))
        elif text.startswith("--projects="):
            parsed.extend(_split_maven_modules(text.split("=", 1)[1]))
    return list(dict.fromkeys(module for module in parsed if module))


def _target_test_modules(evidence: Dict[str, Any], target_tests: list[str]) -> list[str]:
    if not target_tests:
        return []
    test_suffixes = {
        f"src/test/java/{target_test.replace('.', '/')}.java"
        for target_test in target_tests
        if target_test
    }
    modules: list[str] = []
    for path_text in _string_list(evidence.get("changedJavaFiles")):
        normalized = path_text.replace("\\", "/").lstrip("/")
        if not any(normalized.endswith(suffix) for suffix in test_suffixes):
            continue
        module = _module_from_java_test_path(normalized)
        if module and module not in modules:
            modules.append(module)
    return modules


def _module_from_java_test_path(path_text: str) -> str:
    marker = "/src/test/java/"
    if marker not in path_text:
        return ""
    return path_text.split(marker, 1)[0].rstrip("/")


def _split_maven_modules(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _detected_maven_tooling(enforcement: Dict[str, Any]) -> Dict[str, Any] | None:
    evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else None
    tooling = evidence.get("tooling") if isinstance(evidence, dict) else None
    if not isinstance(tooling, dict):
        return None
    artifact_id = tooling.get("artifactId") or ""
    version = tooling.get("version") or ""
    required_version = tooling.get("requiredVersion") or _required_version_for_tooling(artifact_id)
    reason = tooling.get("reason") or ""
    if not any((artifact_id, version, reason)):
        return None
    return {
        "available": tooling.get("available"),
        "artifactId": artifact_id,
        "version": version,
        "requiredVersion": required_version,
        "reason": reason,
    }


def _required_version_for_tooling(artifact_id: str) -> str:
    if artifact_id == "test-enforcer":
        return TEST_ENFORCER_VERSION
    if artifact_id == "example-parent-generic":
        return PARENT_GENERIC_VERSION
    if artifact_id == "example-root":
        return PARENT_ROOT_VERSION
    return ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


class JavaRawOutputEvidence:
    """Java's reading of raw Maven/PIT output, as a backend role.

    Generic enforcement used to import these three functions directly, which is
    the "generic enforcement must not depend on Java" the spec forbids and was
    one of the last edges pointing the wrong way between the two packages.
    Wrapping them changes no parsing; it only gives composition something to
    register.
    """

    language = "java"

    @staticmethod
    def baseline_failure_detail(output: str):
        return pit_baseline_failure_detail(output)

    @staticmethod
    def should_merge(enforcement, output: str) -> bool:
        return should_merge_maven_output_evidence(dict(enforcement), output)

    @staticmethod
    def output_detail(output: str):
        return output_evidence_detail(output)

    @staticmethod
    def dependency_warnings(output: str) -> list[str]:
        return missing_pom_warnings(output)
