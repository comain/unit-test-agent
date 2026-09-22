"""Persistence for per-class coverage-ROI artifacts.

Writing, cache-key checking and clearing the ``<Class>.roi.md`` file is
storage, not analysis: it is the only part of the context builder that owns a
cache invalidation rule.
"""

from typing import Any, Dict, Optional

class JavaRoiStore:
    """The ROI artifact cache of ``ContextBuilder``."""

    def export_roi_scores(
        self,
        class_fqn: str,
        roi_data: Dict[str, Any],
        *,
        source_path: str = "",
        jacoco_xml_path: Optional[str] = None,
        debug: bool = False,
    ) -> str:
        """Write ROI scores to a cached markdown file.

        Returns the absolute path to the file. Reuses cache if source/jacoco
        mtimes haven't changed.
        """
        from uta.language.java.scoring.coverage_roi import (
            compute_roi_cache_key,
            format_roi_markdown,
            is_degenerate_roi_markdown,
        )

        simple_name = class_fqn.split(".")[-1]
        roi_path = self.context_dir / f"{simple_name}.roi.md"

        # Check cache
        src = source_path or self.get_class_source_path(class_fqn)
        cache_key = compute_roi_cache_key(src, jacoco_xml_path, debug=debug)
        cache_marker = f"<!-- cache:{cache_key} -->"

        if roi_path.exists():
            existing = roi_path.read_text(encoding="utf-8")
            first_lines = existing[:200]
            if cache_marker in first_lines and not is_degenerate_roi_markdown(existing):
                return str(roi_path.resolve())

        content = cache_marker + "\n" + format_roi_markdown(roi_data, debug=debug)
        roi_path.write_text(content, encoding="utf-8")
        return str(roi_path.resolve())

    def clear_roi_scores(self, class_fqn: str) -> None:
        """Remove the cached ROI artifact for a class if it exists."""
        simple_name = class_fqn.split(".")[-1]
        roi_path = self.context_dir / f"{simple_name}.roi.md"
        if roi_path.exists():
            roi_path.unlink()
