from __future__ import annotations

import re
from typing import Any

from uta.engine.validation import PlanCallable, PlanContext


class JavaMarkdownPlanContextExtractor:
    language = "java"

    def can_extract(self, context: Any) -> bool:
        return isinstance(context, str)

    def extract(self, context: Any) -> PlanContext:
        text = str(context or "")
        section_m = re.search(r"## Public Methods\n(.*?)(?=\n##|\Z)", text, re.DOTALL)
        if not section_m:
            return PlanContext(callables=[], language="java")
        callables = []
        for match in re.finditer(r"^\s*[*-]\s*`([^`]+)`", section_m.group(1), re.MULTILINE):
            signature = match.group(1).strip()
            name_match = re.search(r"\b([a-z][A-Za-z0-9_]+)\s*\(", signature)
            if not name_match:
                continue
            name = name_match.group(1)
            if name in _SKIP_KNOWN:
                continue
            callables.append(PlanCallable(name=name, qualified_name=name, kind="method"))
        return PlanContext(callables=callables, language="java")


_SKIP_KNOWN = frozenset({"main", "equals", "hashCode", "toString", "clone"})
