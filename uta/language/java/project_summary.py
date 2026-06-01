from __future__ import annotations

from pathlib import Path
from typing import Optional

from uta.engine.project_summary import ProjectSummaryArtifacts
from uta.language.java.parse.models import CodeGraph


class JavaProjectSummaryProvider:
    language = "java"

    def __init__(self, repo_path: str, graph: CodeGraph, module: Optional[str]):
        self.repo_path = repo_path
        self.graph = graph
        self.module = module

    def sync(self) -> ProjectSummaryArtifacts:
        from uta.engine import project_summary_artifacts as artifacts

        repo = Path(self.repo_path)
        ctx_dir = repo / ".uta_cache" / "context"
        ctx_dir.mkdir(parents=True, exist_ok=True)

        context_body = artifacts._build_context_summary_markdown(self.repo_path, self.graph, self.module)
        context_path = ctx_dir / artifacts.CONTEXT_SUMMARY_FILENAME
        context_path.write_text(context_body, encoding="utf-8")
        artifacts.logger.info("Wrote %s", context_path)

        guidance_body = artifacts._build_test_generation_guidance_markdown(self.repo_path, self.module)
        guidance_path = ctx_dir / artifacts.TEST_GUIDANCE_FILENAME
        guidance_path.write_text(guidance_body, encoding="utf-8")
        artifacts.logger.info("Wrote %s", guidance_path)

        repo_summary = repo / artifacts.REPO_SUMMARY_FILENAME
        if not repo_summary.exists():
            repo_summary.write_text(
                artifacts._build_repo_summary_markdown(self.repo_path, self.graph, self.module),
                encoding="utf-8",
            )
            artifacts.logger.info("Created %s", repo_summary)
        elif artifacts._is_uta_generated_summary(repo_summary):
            repo_summary.write_text(
                artifacts._build_repo_summary_markdown(self.repo_path, self.graph, self.module),
                encoding="utf-8",
            )
            artifacts.logger.info("Refreshed UTA-owned %s", repo_summary)
        else:
            artifacts.logger.info("Leaving existing %s unchanged (not UTA-generated)", repo_summary)

        return ProjectSummaryArtifacts(
            repo_summary_abs=str(repo_summary.resolve()),
            context_summary_abs=str(context_path.resolve()),
            test_guidance_abs=str(guidance_path.resolve()),
            compile_facts_abs=str((ctx_dir / artifacts.COMPILE_FACTS_FILENAME).resolve()),
        )
