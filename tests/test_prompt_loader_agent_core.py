from __future__ import annotations

import pytest
from jinja2 import Template, UndefinedError

from uta.testgen.prompts import load_prompt, render_prompt_split


def test_compatibility_loader_exposes_a_render_facade_not_jinja_template():
    prompt = load_prompt("fix_compile")

    assert callable(prompt.render)
    assert not isinstance(prompt, Template)


def test_shared_prompt_rendering_rejects_missing_required_values():
    with pytest.raises(UndefinedError):
        render_prompt_split(
            "fix_compile",
            class_fqn="com.example.Foo",
            test_file_path="src/test/FooTest.java",
            target_context_abs="/ctx/Foo.context.md",
            target_symbols_abs="/ctx/Foo.symbols.md",
            maven_module_flag="",
            # compile_errors is deliberately absent.
        )
