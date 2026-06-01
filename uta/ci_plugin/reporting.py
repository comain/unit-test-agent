from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
import re
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, select_autoescape

from uta.ci_plugin.context import assemble_base_context
from uta.ci_plugin.enforcement import MISSING_EVIDENCE_SUMMARY
from uta.ci_plugin.fix_sessions import can_create_fix_session
from uta.ci_plugin.models import CiTaskRecord

GMT8 = timezone(timedelta(hours=8))


class CiReportRenderer:
    def __init__(self) -> None:
        template_dir = Path(__file__).resolve().parent / "templates"
        self.env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["html"]),
        )
        self.env.filters["gmt8"] = format_gmt8

    def status_html(self, record: CiTaskRecord) -> str:
        return self.env.get_template("status.html").render(record=record, detail=self.detail(record))

    def report_html(self, record: CiTaskRecord) -> str:
        return self.env.get_template("report.html").render(record=record, detail=self.detail(record))

    def repair_progress_html(self, progress: Dict[str, Any]) -> str:
        session = progress.get("session") if isinstance(progress, dict) else None
        rerun_enforcement = session.get("rerunEnforcement") if isinstance(session, dict) else None
        progress = {
            **progress,
            "rerunEvidence": self._evidence_detail(rerun_enforcement),
        }
        return self.env.get_template("repair_progress.html").render(progress=progress)

    def recent_jobs_html(
        self,
        records: list[CiTaskRecord],
        *,
        since: datetime,
        generated_at: datetime,
        hours: int,
        limit: int = 200,
    ) -> str:
        return self.env.get_template("recent_jobs.html").render(
            detail=self.recent_jobs_detail(records, since=since, generated_at=generated_at, hours=hours, limit=limit)
        )

    def recent_jobs_detail(
        self,
        records: list[CiTaskRecord],
        *,
        since: datetime,
        generated_at: datetime,
        hours: int,
        limit: int = 200,
    ) -> Dict[str, Any]:
        rows = [self._recent_job_row(record) for record in records]
        status_counts = Counter(row["status"] for row in rows)
        language_counts = Counter(row["language"] for row in rows)
        enforcement_counts = Counter(row["enforcementStatus"] for row in rows)
        return {
            "generatedAt": generated_at.isoformat(),
            "since": since.isoformat(),
            "hours": hours,
            "limit": limit,
            "total": len(rows),
            "statusCounts": dict(sorted(status_counts.items())),
            "languageCounts": dict(sorted(language_counts.items())),
            "enforcementCounts": dict(sorted(enforcement_counts.items())),
            "rows": rows,
        }

    def detail(self, record: CiTaskRecord) -> Dict[str, Any]:
        enforcement = record.enforcement_result
        return {
            "taskId": record.task_id,
            "status": record.status.value,
            "appName": record.request.app_name,
            "branch": record.request.branch,
            "gitUrl": record.request.git_url,
            "commitId": record.request.commit_id,
            "jiraId": record.request.jira_id,
            "operator": record.request.operator,
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "taskUrl": record.task_url,
            "reportUrl": record.report_url,
            "summary": record.summary,
            "enforcement": enforcement,
            "evidence": self._evidence_detail(enforcement),
            "testEnforcementRequirements": self._test_enforcement_requirements(enforcement),
            "canCreateFixSession": can_create_fix_session(record),
            "callback": {
                "succeeded": record.callback_succeeded,
                "error": record.callback_error,
                "history": record.callback_history,
            },
            "context": self._context_detail(record),
            "fixSessions": record.fix_sessions,
        }

    @staticmethod
    def _recent_job_row(record: CiTaskRecord) -> Dict[str, Any]:
        enforcement = record.enforcement_result if isinstance(record.enforcement_result, dict) else {}
        fix_status_counts = Counter(
            str(session.get("status") or "unknown")
            for session in record.fix_sessions
            if isinstance(session, dict)
        )
        return {
            "taskId": record.task_id,
            "status": record.status.value,
            "appName": record.request.app_name,
            "branch": record.request.branch,
            "gitUrl": record.request.git_url,
            "commitId": record.request.commit_id,
            "jiraId": record.request.jira_id,
            "operator": record.request.operator,
            "language": record.request.language,
            "ciTaskId": record.request.task_id,
            "recordId": record.request.record_id,
            "taskTemplateId": record.request.task_template_id,
            "createdAt": record.created_at.isoformat(),
            "updatedAt": record.updated_at.isoformat(),
            "taskUrl": record.task_url,
            "reportUrl": record.report_url,
            "summary": record.summary,
            "enforcementStatus": str(enforcement.get("status") or "pending"),
            "enforcementPassed": enforcement.get("passed"),
            "callbackSucceeded": record.callback_succeeded,
            "callbackError": record.callback_error,
            "fixSessionCount": len(record.fix_sessions),
            "fixSessionStatusCounts": dict(sorted(fix_status_counts.items())),
        }

    @classmethod
    def _evidence_detail(cls, enforcement: Dict[str, Any] | None) -> Dict[str, Any]:
        if not enforcement:
            return {"coverage": None, "mutation": None, "pitMutation": None}
        if enforcement.get("status") == "missing_evidence":
            return {
                "coverage": cls._zero_coverage_detail(),
                "mutation": cls._zero_mutation_detail(),
                "pitMutation": None,
            }
        structured = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else None
        if structured:
            return {
                "coverage": cls._structured_coverage_detail(structured.get("coverage")),
                "mutation": cls._structured_mutation_detail(structured.get("mutation")),
                "pitMutation": None,
            }
        output = f"{enforcement.get('stdout') or ''}\n{enforcement.get('stderr') or ''}"
        is_java_test_enforcer_output = "test-enforcer" in output.lower()
        mutation = cls._mutation_detail(output, allow_pit_fallback=not is_java_test_enforcer_output)
        if is_java_test_enforcer_output and not mutation and cls._has_scoped_pitest_targets(output):
            mutation = cls._mutation_detail(output, allow_pit_fallback=True)
            if mutation:
                mutation["source"] = "pit_scoped"
        pit_mutation = None
        if is_java_test_enforcer_output and not mutation:
            pit_mutation = cls._mutation_detail(output, allow_pit_fallback=True)
        return {
            "coverage": cls._coverage_detail(output),
            "mutation": mutation,
            "pitMutation": pit_mutation,
        }

    @staticmethod
    def _zero_coverage_detail() -> Dict[str, Any]:
        return {
            "rate": 0.0,
            "formattedRate": "0.00%",
            "covered": 0,
            "total": 0,
            "modules": 0,
        }

    @staticmethod
    def _zero_mutation_detail() -> Dict[str, Any]:
        return {
            "rate": 0.0,
            "formattedRate": "0.00%",
            "killed": 0,
            "generated": 0,
            "modules": 0,
            "source": "missing_evidence",
        }

    @classmethod
    def _test_enforcement_requirements(cls, enforcement: Dict[str, Any] | None) -> Dict[str, Any] | None:
        if not enforcement or enforcement.get("status") != "missing_evidence":
            return None
        summary = str(enforcement.get("summary") or "")
        if not summary.startswith(MISSING_EVIDENCE_SUMMARY.split(":", 1)[0]):
            return None
        return {
            "summary": MISSING_EVIDENCE_SUMMARY,
            "detected": cls._detected_maven_tooling(enforcement),
            "requiredPlugin": "test-enforcer >= 1.0.12",
            "inheritancePaths": [
                {
                    "title": "如果项目继承 service-parent",
                    "instruction": "升级父 POM 到 service-parent 1.0.0 或更高版本；该父链会通过 quality-parent 引入 test-enforcement profile 和 test-enforcer。",
                    "snippet": "<parent>\n  <groupId>com.example.platform</groupId>\n  <artifactId>service-parent</artifactId>\n  <version>1.0.0</version>\n</parent>",
                },
                {
                    "title": "如果项目直接继承 quality-parent",
                    "instruction": "升级父 POM 到 quality-parent 1.0.0 或更高版本；不要只在命令里加 test.enforcement.enabled，父 POM/profile 必须能解析出 test-enforcer。",
                    "snippet": "<parent>\n  <groupId>com.example.build</groupId>\n  <artifactId>quality-parent</artifactId>\n  <version>1.0.0</version>\n</parent>",
                },
            ],
            "fallback": {
                "title": "无法升级父 POM 时的兜底方案",
                "instruction": (
                    "在项目自己的 test-enforcement profile 中直接引入已发布的 test-enforcer。"
                    "同时必须把 JaCoCo 和 PIT 绑定到 Maven 生命周期：test 前执行 jacoco:prepare-agent，"
                    "test 后生成 jacoco:report，verify 阶段执行 pitest:mutationCoverage，"
                    "并保留 test-enforcer 的 filter-diff/check-coverage 配置。"
                    "UTA/CI 需要同时看到 diff coverage、非空 pitest.targets 和 PIT Test strength 输出；"
                    "只声明 test-enforcer 插件或只有未关联 filter-diff 的 PIT 输出都不够。"
                    "UTA 只把 Maven effective-pom 中 active build plugin 里的 test-enforcer >= 1.0.12 视为可修复证据。"
                ),
                "snippet": (
                    "<profile>\n"
                    "  <id>test-enforcement</id>\n"
                    "  <activation><property><name>test.enforcement.enabled</name><value>true</value></property></activation>\n"
                    "  <build><plugins>\n"
                    "    <!-- jacoco-maven-plugin: prepare-agent before test, report after test -->\n"
                    "    <!-- pitest-maven: mutationCoverage in verify, scoped by test-enforcer filter-diff properties -->\n"
                    "    <!-- output must include non-empty pitest.targets from filter-diff plus PIT Test strength -->\n"
                    "    <plugin>\n"
                    "      <groupId><!-- released plugin groupId --></groupId>\n"
                    "      <artifactId>test-enforcer</artifactId>\n"
                    "      <version>1.0.12</version>\n"
                    "      <!-- executions: filter-diff in initialize, check-coverage in verify -->\n"
                    "    </plugin>\n"
                    "  </plugins></build>\n"
                    "</profile>"
                ),
            },
            "versions": [
                "required resolved build plugin: test-enforcer >= 1.0.12",
                "normal parent rollout: quality-parent >= 1.0.0",
                "platform parent rollout: service-parent >= 1.0.0",
            ],
            "usageGuide": "../../docs/test-enforce-usage.md",
        }

    @staticmethod
    def _detected_maven_tooling(enforcement: Dict[str, Any]) -> Dict[str, Any] | None:
        evidence = enforcement.get("evidence") if isinstance(enforcement.get("evidence"), dict) else None
        tooling = evidence.get("tooling") if isinstance(evidence, dict) else None
        if not isinstance(tooling, dict):
            return None
        artifact_id = tooling.get("artifactId") or ""
        version = tooling.get("version") or ""
        reason = tooling.get("reason") or ""
        if not any((artifact_id, version, reason)):
            return None
        return {
            "artifactId": artifact_id,
            "version": version,
            "reason": reason,
        }

    @staticmethod
    def _structured_coverage_detail(summary: Any) -> Dict[str, Any] | None:
        if not isinstance(summary, dict):
            return None
        rate = float(summary.get("rate") or 0.0)
        return {
            "rate": rate,
            "formattedRate": f"{rate:.2f}%",
            "covered": int(summary.get("covered") or 0),
            "total": int(summary.get("total") or 0),
            "modules": int(summary.get("modules") or 1),
            "source": "python",
        }

    @staticmethod
    def _structured_mutation_detail(summary: Any) -> Dict[str, Any] | None:
        if not isinstance(summary, dict):
            return None
        rate = float(summary.get("rate") or 0.0)
        return {
            "rate": rate,
            "formattedRate": f"{rate:.2f}%",
            "killed": int(summary.get("killed") or 0),
            "generated": int(summary.get("generated") or 0),
            "survived": int(summary.get("survived") or 0),
            "modules": int(summary.get("modules") or 1),
            "source": "python",
        }

    @staticmethod
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

    @staticmethod
    def _mutation_detail(output: str, *, allow_pit_fallback: bool = True) -> Dict[str, Any] | None:
        diff_matches = re.findall(
            r"diff mutation score\s+([0-9]+(?:\.[0-9]+)?)%\s+.*?\((\d+)/(\d+)\s+detected\)",
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
        generated = sum(int(match[0]) for match in matches)
        killed = sum(int(match[1]) for match in matches)
        strength_matches = re.findall(r"Test strength\s+([0-9]+(?:\.[0-9]+)?)%", output, flags=re.IGNORECASE)
        rate = float(strength_matches[-1]) if strength_matches else ((killed / generated * 100) if generated else 0.0)
        return {
            "rate": rate,
            "formattedRate": f"{rate:.2f}%",
            "killed": killed,
            "generated": generated,
            "modules": len(matches),
            "source": "pit",
        }

    @staticmethod
    def _has_scoped_pitest_targets(output: str) -> bool:
        for match in re.finditer(r"\bpitest\.targets=(\d+)\s+\[([^\]]*)\]", output, flags=re.IGNORECASE):
            if int(match.group(1)) > 0 and match.group(2).strip():
                return True
        return False

    @staticmethod
    def _context_detail(record: CiTaskRecord) -> Dict[str, Any]:
        context = CiReportRenderer._latest_repair_context(record) or assemble_base_context(record)
        return {
            "sources": context.get("sources") or [],
            "missingReasons": context.get("missingReasons") or [],
        }

    @staticmethod
    def _latest_repair_context(record: CiTaskRecord) -> Dict[str, Any] | None:
        for session in reversed(record.fix_sessions):
            context = session.get("ciContext")
            if isinstance(context, dict) and context:
                return context
        return None


def format_gmt8(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(GMT8).strftime("%Y-%m-%d %H:%M:%S GMT+8")
