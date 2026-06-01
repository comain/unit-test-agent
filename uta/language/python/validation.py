from __future__ import annotations

from typing import Any

from uta.engine.validation import PlanCallable, PlanContext


class PythonPayloadPlanContextExtractor:
    language = "python"

    def can_extract(self, context: Any) -> bool:
        return isinstance(context, dict) and (
            str(context.get("language") or "").strip().lower() == "python"
            or isinstance(context.get("symbols"), list)
        )

    def extract(self, context: Any) -> PlanContext:
        payload = context if isinstance(context, dict) else {}
        callables = []
        for symbol in payload.get("symbols") or []:
            if symbol.get("kind") not in {"function", "method"}:
                continue
            name = str(symbol.get("name") or "")
            if not name or name in _SKIP_KNOWN:
                continue
            visibility_rank = 2 if name.startswith("_") else 0
            callables.append(
                PlanCallable(
                    name=name,
                    qualified_name=str(symbol.get("qualified_name") or name),
                    kind=str(symbol.get("kind") or "function"),
                    visibility_rank=visibility_rank,
                    start_line=int(symbol.get("line") or 0),
                    end_line=int(symbol.get("end_line") or symbol.get("line") or 0),
                    metadata=dict(symbol),
                )
            )
        return PlanContext(callables=callables, language="python")


_SKIP_KNOWN = frozenset({"main", "equals", "hashCode", "toString", "clone"})
