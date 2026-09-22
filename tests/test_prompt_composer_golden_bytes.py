"""Exact-byte contracts for language-owned final prompt composition."""

from __future__ import annotations

import hashlib
from pathlib import Path

from uta.language.java.phases.generation_turn import render_generate_tests_prompt
from uta.language.java.phases.planning import render_plan_tests_prompt
from uta.language.python.phases import render_repair_prompt
from uta.shared.languages import RawTargetSelection, default_registry


def _digest(text: str) -> tuple[int, str]:
    payload = text.encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def test_durable_java_plan_composer_bytes_include_optional_replan_suffix(monkeypatch):
    monkeypatch.delenv("UTA_RDC_CONTEXT_PATH", raising=False)
    monkeypatch.setattr(
        "uta.language.java.phases.planning.ensure_stage_introspect_file",
        lambda *_args: "/state/stages/plan.md",
    )
    prompt = render_plan_tests_prompt(
        {
            "repo_path": "/repo",
            "batch": ["com.example.订单Service", "com.example.PriceService"],
            "coverage_gate": 80,
            "quality_mode": "ci_incremental",
            "ci_diff_coverage_gate": 95,
            "ci_diff_mutation_gate": 100,
            "strict_coverage_classes": [
                {
                    "class_fqn": "com.example.订单Service",
                    "line_count": 240,
                    "public_method_count": 12,
                }
            ],
            "target_context_paths": {
                "com.example.订单Service": {
                    "context_abs": "/ctx/订单.md",
                    "symbols_abs": "/ctx/订单.symbols.md",
                    "roi_abs": "/ctx/订单.roi.md",
                },
                "com.example.PriceService": {
                    "context_abs": "/ctx/price.md",
                    "symbols_abs": "/ctx/price.symbols.md",
                },
            },
            "roi_enabled": True,
            "plan_index_query_command": "/opt/uta/bin/uta-query-index",
            "spec_context": "订单金额必须大于零。",
            "phase_results": {
                "plan_tests": {
                    "evidence": {
                        "replan_reasons": [
                            "Cover the zero-value boundary.",
                            "Include the retry failure path.",
                        ]
                    }
                }
            },
        }
    )

    assert _digest(prompt) == (
        8493,
        "d24e9e3a603a461de0cdedd1c068a753ddd9f8d32c0d87cda1f87e8043882a45",
    )


class _DurableGenerationPorts:
    @staticmethod
    def mockito_api_guidance(_repo_path):
        return "- Use the repository's Mockito 1.x API."

    @staticmethod
    def compress_plan(text):
        return f"COMPRESSED PLAN:\n{text}"


def test_durable_java_generation_composer_bytes_include_batch_and_plan_suffixes(monkeypatch):
    monkeypatch.delenv("UTA_RDC_CONTEXT_PATH", raising=False)
    monkeypatch.setattr(
        "uta.language.java.phases.generation_turn.ensure_stage_introspect_file",
        lambda *_args: "/state/stages/generate.md",
    )
    monkeypatch.setattr(
        "uta.language.java.phases.generation_turn.time.time", lambda: 1_700_000_000
    )
    prompt = render_generate_tests_prompt(
        {
            "repo_path": "/repo",
            "module": "biz",
            "batch": ["com.example.订单Service", "com.example.PriceService"],
            "coverage_gate": 80,
            "quality_mode": "class_batch",
            "ci_diff_coverage_gate": 95,
            "ci_diff_mutation_gate": 100,
            "strict_coverage_classes": [],
            "context_dir": "/ctx",
            "generation_index_query_command": "/opt/uta/bin/uta-query-index",
            "spec_context": "订单金额必须大于零。",
            "target_context_paths": {
                "com.example.订单Service": {
                    "source_abs": "/repo/biz/src/main/java/com/example/订单Service.java",
                    "context_abs": "/ctx/订单.md",
                    "symbols_abs": "/ctx/订单.symbols.md",
                },
                "com.example.PriceService": {
                    "source_abs": "/repo/biz/src/main/java/com/example/PriceService.java",
                    "context_abs": "/ctx/price.md",
                    "symbols_abs": "/ctx/price.symbols.md",
                },
            },
            "project_prompt_paths": {
                "repo_summary_abs": "/repo/.uta_summary.md",
                "repo_summary_exists": False,
                "context_summary_abs": "/ctx/project_summary.md",
                "test_guidance_abs": "/ctx/test_guidance.md",
                "compile_facts_abs": "/ctx/compile_facts.md",
                "compile_facts_exists": False,
            },
            "phase_results": {
                "plan_tests": {
                    "plan_path": "/state/plans/approved.md",
                    "plan_text": "PUBLIC METHODS\n- create\n- cancel",
                }
            },
        },
        ports=_DurableGenerationPorts(),
    )

    assert _digest(prompt) == (
        7586,
        "bf858148de769488c1348804defc84bc31e0e5d7e2d6ac6f420b44cc9f7553f2",
    )


class _LegacyJavaContextBuilder:
    def __init__(self, repo_path, graph, flows):
        self.repo_path = Path(repo_path)

    def export_context_files(self):
        path = self.repo_path / ".uta_cache" / "context"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def export_target_context_files(self, class_fqn, **_kwargs):
        return {
            "context_abs": "/ctx/legacy-order.md",
            "symbols_abs": "/ctx/legacy-order.symbols.md",
            "roi_abs": "",
        }

    @staticmethod
    def get_class_source_path(_class_fqn):
        return "/repo/biz/src/main/java/com/example/OrderService.java"


class _LegacyJavaClient:
    def __init__(self):
        self.prompts = []
        self.sessions = []

    def open_session(self, model_id=None, permissions=None):
        session_id = f"legacy-{len(self.sessions) + 1}"
        self.sessions.append(session_id)
        return session_id

    def run_node(self, *, prompt, **_kwargs):
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return {"type": "completed", "result": "PUBLIC METHODS\n- create\nBRANCH AXES\n- zero/positive"}
        return {"type": "completed", "result": "generation completed"}

    @staticmethod
    def get_messages(_session_id):
        return []




def _python_target():
    return default_registry().adapter_for("python").normalize_target(
        RawTargetSelection(target="jobs/订单.py::calculate_total")
    )


def test_durable_python_coverage_composer_bytes_preserve_alias_values(monkeypatch):
    monkeypatch.delenv("UTA_RDC_CONTEXT_PATH", raising=False)
    target = _python_target()
    prompt = render_repair_prompt(
        {
            "repo_path": "/repo",
            "target": target.as_selection(),
            "generated_test_path": "tests/uta_generated/test_jobs_订单.py",
            "target_context_paths": {
                "context_abs": "/ctx/订单.md",
                "json_abs": "/ctx/订单.json",
            },
            "index_query_command": "/opt/uta/bin/uta-query-index",
            "coverage_gate": 95,
            "mutation_gate": 100,
            "phase_results": {
                "measure_coverage": {
                    "evidence": {"message": "line 24: 未覆盖边界分支"}
                }
            },
        },
        "fix_coverage",
    )

    assert _digest(prompt) == (
        2860,
        "30c699222a7993f957867fc6fd9311a6e5365d3b80f87462934b3c0a5f667ec8",
    )
