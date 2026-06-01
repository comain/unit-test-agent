"""Generate a minimal Java test class skeleton from context.md fields.

Replaces the model's ad-hoc skeleton derivation with a deterministic Python
template, saving one LLM turn per class (K strategy).

The emitted starter matches the repo's primary prompt contract:
- JUnit 4
- Mockito 2.x
- compile-safe package/import/mock boilerplate
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class SkeletonSpec:
    class_fqn: str
    package: str
    simple_name: str
    test_simple_name: str
    dependency_types: List[str] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)


_SKIP_MOCK_TYPES = frozenset({"String", "Integer", "Long", "Boolean", "int", "long",
                               "boolean", "double", "float", "Object"})


def _java_mock_field_name(type_name: str) -> str:
    simple = type_name.split(".")[-1]
    return simple[0].lower() + simple[1:]


def generate_test_skeleton(spec: SkeletonSpec) -> str:
    """Return a compilable Java test class skeleton string."""
    lines: List[str] = []
    mock_types = [t for t in spec.dependency_types if t.split(".")[-1] not in _SKIP_MOCK_TYPES]
    lines.append(f"package {spec.package};")
    lines.append("")

    # Always-needed imports
    base_imports = [
        "import org.junit.Test;",
        "import org.junit.runner.RunWith;",
        "import org.mockito.InjectMocks;",
        "import org.mockito.Mock;",
        "import org.mockito.junit.MockitoJUnitRunner;",
        "import static org.junit.Assert.*;",
        "import static org.mockito.Mockito.*;",
    ]
    seen_imports = set()
    for imp in base_imports:
        if imp not in seen_imports:
            lines.append(imp)
            seen_imports.add(imp)
    for imp in spec.imports:
        stmt = imp if imp.startswith("import ") else f"import {imp};"
        if stmt not in seen_imports:
            lines.append(stmt)
            seen_imports.add(stmt)
    lines.append("")
    if mock_types:
        lines.append("@RunWith(MockitoJUnitRunner.class)")
    lines.append(f"class {spec.test_simple_name} {{")
    lines.append("")
    for dep_type in mock_types:
        simple = dep_type.split(".")[-1]
        field_name = _java_mock_field_name(simple)
        lines.append(f"    @Mock")
        lines.append(f"    private {simple} {field_name};")
        lines.append("")
    lines.append("    @InjectMocks")
    lines.append(f"    private {spec.simple_name} underTest;")
    lines.append("")
    lines.append("    @Test")
    lines.append("    void testPlaceholder() {")
    lines.append("        // TODO: add test logic")
    lines.append("        assertNotNull(underTest);")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def spec_from_context_md(context_md: str, class_fqn: str) -> SkeletonSpec:
    """Parse a .context.md file and return a SkeletonSpec."""
    package = ".".join(class_fqn.split(".")[:-1])
    simple_name = class_fqn.split(".")[-1]
    test_simple_name = simple_name + "Test"

    dep_types: List[str] = []
    imports: List[str] = []

    dep_section = re.search(r"## Dependency Types\n(.*?)(?=\n##|\Z)", context_md, re.DOTALL)
    if dep_section:
        for m in re.finditer(r"`([A-Za-z][A-Za-z0-9_$.]*)`", dep_section.group(1)):
            t = m.group(1)
            if "." not in t and t not in _SKIP_MOCK_TYPES:
                dep_types.append(t)

    import_section = re.search(r"## Imports\n(.*?)(?=\n##|\Z)", context_md, re.DOTALL)
    if import_section:
        for m in re.finditer(r"`([A-Za-z][A-Za-z0-9_.]+)`", import_section.group(1)):
            imp = m.group(1)
            if "." in imp:
                imports.append(imp)

    return SkeletonSpec(
        class_fqn=class_fqn,
        package=package,
        simple_name=simple_name,
        test_simple_name=test_simple_name,
        dependency_types=list(dict.fromkeys(dep_types)),
        imports=list(dict.fromkeys(imports)),
    )


def generate_skeleton_from_context_file(context_path: str, class_fqn: str) -> str:
    """Convenience: read context_path, parse, and return skeleton string."""
    text = Path(context_path).read_text(encoding="utf-8")
    spec = spec_from_context_md(text, class_fqn)
    return generate_test_skeleton(spec)
