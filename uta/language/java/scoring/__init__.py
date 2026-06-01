from __future__ import annotations

from pathlib import Path
from typing import Any

from uta.engine.scoring import TargetScoreResult
from uta.engine.targets import TargetRef


class JavaTargetScorer:
    language = "java"

    def score_target(self, repo_path: Path, target: TargetRef, **kwargs: Any) -> TargetScoreResult:
        graph = kwargs.get("graph")
        if graph is None:
            raise ValueError("JavaTargetScorer requires graph")
        from uta.language.java.scoring.coverage_roi import compute_class_roi

        roi = compute_class_roi(
            target.target_id,
            graph,
            jacoco_xml_path=kwargs.get("jacoco_xml_path"),
        )
        return TargetScoreResult(
            language="java",
            target_id=target.target_id,
            methods=list(roi.get("methods") or []),
            summary=dict(roi.get("summary") or {}),
            provenance=dict(roi.get("provenance") or {}),
        )
