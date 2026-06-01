import logging
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from uta.config import settings

logger = logging.getLogger("uta.jacoco")


def _resolve_jacoco_agent_jar() -> Optional[str]:
    jacoco_version = "0.8.12"
    home = Path.home()
    agent_candidates = [
        home / ".m2" / "repository" / "org" / "jacoco" / "org.jacoco.agent" / jacoco_version / f"org.jacoco.agent-{jacoco_version}-runtime.jar",
    ]
    for repo_dir in (home / ".m2" / "repository",):
        candidate = repo_dir / "org" / "jacoco" / "org.jacoco.agent" / jacoco_version / f"org.jacoco.agent-{jacoco_version}-runtime.jar"
        if candidate.exists():
            agent_candidates.insert(0, candidate)
            break
    for candidate in agent_candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _clear_jacoco_artifacts(module_base: Path, exec_file: Path) -> None:
    """Delete stale Jacoco and Surefire outputs before a test run."""
    if exec_file.exists():
        logger.debug("Deleting stale jacoco.exec: %s", exec_file)
        exec_file.unlink()
    jacoco_site_dir = module_base / "target" / "site" / "jacoco"
    if jacoco_site_dir.exists():
        logger.debug("Deleting stale Jacoco site: %s", jacoco_site_dir)
        shutil.rmtree(jacoco_site_dir)
    surefire_dir = module_base / "target" / "surefire-reports"
    if surefire_dir.exists():
        shutil.rmtree(surefire_dir)


def _run_maven_jacoco(
    repo_path: str,
    test_selector: str,
    module: Optional[str] = None,
    timeout: int = 600,
) -> Tuple[bool, str]:
    """Run one Maven/Jacoco invocation for a test selector."""

    module_base = Path(repo_path) / (module if module else "")
    exec_file = module_base / "target" / "jacoco.exec"

    _clear_jacoco_artifacts(module_base, exec_file)

    cmd = [
        settings.maven_bin,
        "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent",
        "test",
        "org.jacoco:jacoco-maven-plugin:0.8.12:report",
        f"-Dtest={test_selector}",
        "-DfailIfNoTests=false",
        "-Dsurefire.failIfNoSpecifiedTests=false",
        "-DskipTests=false",
        "-Dmaven.test.skip=false",
    ]

    # Find the agent jar and pass it explicitly so Surefire picks it up
    # even if the project has custom argLine configuration.
    agent_jar = _resolve_jacoco_agent_jar()
    if agent_jar:
        cmd.append(f"-DargLine=-javaagent:{agent_jar}=destfile={exec_file}")
        logger.info("Using Jacoco agent: %s", agent_jar)
    else:
        logger.warning("No Jacoco agent jar found! Coverage will rely on prepare-agent goal only.")

    if module:
        cmd.extend(["-pl", module, "-am"])

    logger.info("Jacoco command: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd, cwd=repo_path, capture_output=True, timeout=timeout,
        )
        output = result.stdout.decode(errors="replace")
        stderr = result.stderr.decode(errors="replace")
        combined = f"{output}\n{stderr}"

        # Log key diagnostics
        for line in output.split("\n"):
            if any(k in line for k in ["Tests run:", "jacoco", "argLine", "No tests were executed"]):
                logger.info("MVN: %s", line.strip())

        # Check if jacoco.exec was created
        if exec_file.exists():
            logger.info("jacoco.exec created: %s (%d bytes)", exec_file, exec_file.stat().st_size)
        else:
            logger.warning("jacoco.exec NOT created at %s", exec_file)

        # Check if XML report was generated
        xml_path = module_base / "target" / "site" / "jacoco" / "jacoco.xml"
        if xml_path.exists():
            logger.info("Jacoco XML report exists: %s (%d bytes)", xml_path, xml_path.stat().st_size)
        else:
            logger.warning("Jacoco XML report NOT found at %s", xml_path)

        if any(
            marker in combined
            for marker in (
                "Tests are skipped.",
                "Skipping execution of surefire because it has been configured to skip tests",
            )
        ):
            return False, f"Jacoco/test skipped unexpectedly:\n{combined[-2000:]}"

        if result.returncode != 0:
            # Even on test failure, try to generate Jacoco report from partial coverage
            if exec_file.exists() and not xml_path.exists():
                logger.info("Tests failed but jacoco.exec exists — generating report anyway")
                report_cmd = [
                    settings.maven_bin, "org.jacoco:jacoco-maven-plugin:0.8.12:report",
                ]
                if module:
                    report_cmd.extend(["-pl", module])
                subprocess.run(report_cmd, cwd=repo_path, capture_output=True,
                               timeout=60, check=False)
            return False, f"Jacoco/test failed:\n{stderr[-2000:]}\n{output[-2000:]}"
        return True, output
    except subprocess.TimeoutExpired:
        return False, "Jacoco test run timed out"


def run_test_with_jacoco(
    repo_path: str,
    test_class: str,
    module: Optional[str] = None,
    timeout: int = 600,
) -> Tuple[bool, str]:
    """Run a single test class with Jacoco coverage instrumentation."""
    return _run_maven_jacoco(repo_path, test_class, module=module, timeout=timeout)


def run_tests_with_jacoco_batch(
    repo_path: str,
    test_classes: List[str],
    module: Optional[str] = None,
    timeout: int = 900,
) -> Tuple[bool, str]:
    """Run a batch of test classes once with Jacoco coverage instrumentation."""
    selector = ",".join(test_classes)
    return _run_maven_jacoco(repo_path, selector, module=module, timeout=timeout)


def find_jacoco_report(repo_path: str, module: Optional[str] = None) -> Optional[str]:
    """Find the Jacoco XML report."""
    base = Path(repo_path) / (module if module else "") / "target" / "site" / "jacoco" / "jacoco.xml"
    if base.exists():
        return str(base)
    return None


def parse_jacoco_report(xml_path: str, class_fqn: str) -> Dict[str, float]:
    """Parse Jacoco XML and return coverage for a specific class."""
    if not os.path.exists(xml_path):
        logger.warning("Jacoco XML not found: %s", xml_path)
        return {}

    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Jacoco XML structure: report -> package -> class
    package_name = ".".join(class_fqn.split(".")[:-1]).replace(".", "/")
    simple_name = class_fqn.split(".")[-1]
    target_class_name = f"{package_name}/{simple_name}"

    logger.debug("Looking for package=%s, class=%s", package_name, target_class_name)

    # Log all packages and classes found in the report for debugging
    all_packages = [p.get("name") for p in root.findall("package")]
    logger.debug("Packages in report: %s", all_packages)

    for package in root.findall("package"):
        if package.get("name") == package_name:
            all_classes = [c.get("name") for c in package.findall("class")]
            logger.debug("Classes in package %s: %s", package_name, all_classes)
            for cls in package.findall("class"):
                if cls.get("name") == target_class_name:
                    stats = {}
                    for counter in cls.findall("counter"):
                        ctype = counter.get("type")
                        missed = int(counter.get("missed"))
                        covered = int(counter.get("covered"))
                        total = missed + covered
                        if total > 0:
                            stats[ctype.lower()] = (covered / total) * 100
                        else:
                            stats[ctype.lower()] = 100.0
                    logger.info("Coverage for %s: %s", class_fqn, stats)
                    return stats

    logger.warning("Class %s not found in Jacoco report (searched for %s)", class_fqn, target_class_name)
    return {}


def parse_jacoco_line_coverage_for_classes(xml_path: str, class_fqns: List[str]) -> Dict[str, float]:
    """Compute weighted line coverage across a set of classes from a JaCoCo XML report."""
    if not os.path.exists(xml_path):
        logger.warning("Jacoco XML not found: %s", xml_path)
        return {"line": 0.0, "covered_lines": 0, "missed_lines": 0, "matched_classes": 0}

    targets = {
        ".".join(class_fqn.split(".")[:-1]).replace(".", "/") + "/" + class_fqn.split(".")[-1]
        for class_fqn in class_fqns
    }
    if not targets:
        return {"line": 0.0, "covered_lines": 0, "missed_lines": 0, "matched_classes": 0}

    tree = ET.parse(xml_path)
    root = tree.getroot()
    covered_lines = 0
    missed_lines = 0
    matched_classes = 0
    for package in root.findall("package"):
        for cls in package.findall("class"):
            class_name = cls.get("name")
            if class_name not in targets:
                continue
            matched_classes += 1
            for counter in cls.findall("counter"):
                if counter.get("type") != "LINE":
                    continue
                missed_lines += int(counter.get("missed", "0") or 0)
                covered_lines += int(counter.get("covered", "0") or 0)
                break
    total_lines = covered_lines + missed_lines
    line_pct = (covered_lines / total_lines * 100.0) if total_lines > 0 else 100.0
    return {
        "line": line_pct,
        "covered_lines": covered_lines,
        "missed_lines": missed_lines,
        "matched_classes": matched_classes,
    }


def extract_uncovered_clusters(
    xml_path: str,
    class_fqn: str,
    *,
    max_methods: int = 8,
    max_line_clusters: int = 8,
    cluster_gap: int = 2,
) -> Dict[str, Any]:
    """Extract uncovered methods and source line clusters for a class."""
    if not os.path.exists(xml_path):
        return {"methods": [], "line_clusters": []}

    tree = ET.parse(xml_path)
    root = tree.getroot()
    package_name = ".".join(class_fqn.split(".")[:-1]).replace(".", "/")
    simple_name = class_fqn.split(".")[-1]
    target_class_name = f"{package_name}/{simple_name}"

    target_package = None
    target_class = None
    for package in root.findall("package"):
        if package.get("name") != package_name:
            continue
        target_package = package
        for cls in package.findall("class"):
            if cls.get("name") == target_class_name:
                target_class = cls
                break
        break

    if target_package is None or target_class is None:
        return {"methods": [], "line_clusters": []}

    methods: List[Dict[str, Any]] = []
    for method in target_class.findall("method"):
        missed_line = 0
        missed_branch = 0
        for counter in method.findall("counter"):
            ctype = counter.get("type")
            if ctype == "LINE":
                missed_line = int(counter.get("missed", "0") or 0)
            elif ctype == "BRANCH":
                missed_branch = int(counter.get("missed", "0") or 0)
        if missed_line or missed_branch:
            methods.append(
                {
                    "name": method.get("name", "unknown"),
                    "desc": method.get("desc", ""),
                    "missed_line": missed_line,
                    "missed_branch": missed_branch,
                }
            )
    methods.sort(key=lambda item: (-item["missed_line"], -item["missed_branch"], item["name"]))

    sourcefilename = target_class.get("sourcefilename", f"{simple_name}.java")
    sourcefile = None
    for candidate in target_package.findall("sourcefile"):
        if candidate.get("name") == sourcefilename:
            sourcefile = candidate
            break

    line_numbers: List[int] = []
    if sourcefile is not None:
        for line in sourcefile.findall("line"):
            nr = int(line.get("nr", "0") or 0)
            mi = int(line.get("mi", "0") or 0)
            mb = int(line.get("mb", "0") or 0)
            if mi > 0 or mb > 0:
                line_numbers.append(nr)

    line_numbers = sorted(set(line_numbers))
    clusters: List[Dict[str, Any]] = []
    if line_numbers:
        start = prev = line_numbers[0]
        for nr in line_numbers[1:]:
            if nr - prev <= cluster_gap:
                prev = nr
                continue
            clusters.append({"start": start, "end": prev, "count": prev - start + 1})
            start = prev = nr
        clusters.append({"start": start, "end": prev, "count": prev - start + 1})
        clusters.sort(key=lambda item: (-item["count"], item["start"]))

    return {
        "methods": methods[:max_methods],
        "line_clusters": clusters[:max_line_clusters],
    }


def format_uncovered_clusters_markdown(summary: Dict[str, Any]) -> str:
    methods = summary.get("methods") or []
    clusters = summary.get("line_clusters") or []
    if not methods and not clusters:
        return "- No uncovered method or line clusters were extracted.\n"

    lines: List[str] = []
    if methods:
        lines.append("Methods with missed coverage:")
        for method in methods:
            lines.append(
                "- `{}` (missed lines: {}, missed branches: {})".format(
                    method.get("name", "unknown"),
                    int(method.get("missed_line", 0) or 0),
                    int(method.get("missed_branch", 0) or 0),
                )
            )
    if clusters:
        if lines:
            lines.append("")
        lines.append("Uncovered source line clusters:")
        for cluster in clusters:
            lines.append(
                "- lines {}-{} ({} line(s))".format(
                    int(cluster.get("start", 0) or 0),
                    int(cluster.get("end", 0) or 0),
                    int(cluster.get("count", 0) or 0),
                )
            )
    return "\n".join(lines) + "\n"


def parse_surefire_results(
    repo_path: str,
    test_classes: List[str],
    module: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Parse Surefire XML reports and return per-test-class pass/fail summaries."""
    module_base = Path(repo_path) / (module if module else "")
    surefire_dir = module_base / "target" / "surefire-reports"
    results: Dict[str, Dict[str, Any]] = {}
    if not surefire_dir.exists():
        logger.warning("Surefire reports directory not found: %s", surefire_dir)
        return results

    for test_class in test_classes:
        report_paths = sorted(surefire_dir.glob(f"TEST-*{test_class}.xml"))
        if not report_paths:
            results[test_class] = {
                "passed": False,
                "output": f"No Surefire XML report found for {test_class}",
            }
            continue

        passed = True
        details: List[str] = []
        for report_path in report_paths:
            try:
                root = ET.parse(report_path).getroot()
            except ET.ParseError as exc:
                passed = False
                details.append(f"{report_path.name}: failed to parse XML ({exc})")
                continue

            failures = int(root.get("failures", "0") or 0)
            errors = int(root.get("errors", "0") or 0)
            if failures or errors:
                passed = False
                for testcase in root.findall("testcase"):
                    name = testcase.get("name", "unknown")
                    classname = testcase.get("classname", test_class)
                    for tag in ("failure", "error"):
                        for node in testcase.findall(tag):
                            message = (node.get("message") or "").strip()
                            text = (node.text or "").strip()
                            snippet = text[:800] if text else ""
                            details.append(
                                f"{classname}.{name}: {tag}"
                                + (f" — {message}" if message else "")
                                + (f"\n{snippet}" if snippet else "")
                            )

        output = "\n\n".join(details).strip()
        results[test_class] = {
            "passed": passed,
            "output": output,
        }

    return results
