from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TargetLearningKey:
    """Stable key for per-target learning artifacts.

    It keeps Java's legacy class FQN storage compatible while giving Python and
    future languages a language plus target_id identity.
    """

    language: str
    target_id: str
    legacy_class_fqn: Optional[str] = None

    @classmethod
    def from_legacy_class(cls, class_fqn: str) -> "TargetLearningKey":
        return cls(language="java", target_id=str(class_fqn), legacy_class_fqn=str(class_fqn))

    @classmethod
    def from_parts(
        cls,
        *,
        language: Optional[str] = None,
        target_id: Optional[str] = None,
        legacy_class_fqn: Optional[str] = None,
    ) -> "TargetLearningKey":
        resolved_language = str(language or ("java" if legacy_class_fqn else "java")).strip().lower()
        resolved_target = str(target_id or legacy_class_fqn or "").strip()
        if not resolved_target:
            raise ValueError("target_id or legacy_class_fqn is required")
        legacy = legacy_class_fqn if resolved_language == "java" else legacy_class_fqn
        return cls(language=resolved_language, target_id=resolved_target, legacy_class_fqn=legacy)

    def filename(self) -> str:
        return re.sub(r"[^\w.]", "_", self.target_id) + ".jsonl"

    def record_identity(self) -> dict:
        payload = {
            "language": self.language,
            "target_id": self.target_id,
        }
        if self.legacy_class_fqn:
            payload["legacy_class_fqn"] = self.legacy_class_fqn
            payload["class_fqn"] = self.legacy_class_fqn
        elif self.language == "java":
            payload["class_fqn"] = self.target_id
        return payload
