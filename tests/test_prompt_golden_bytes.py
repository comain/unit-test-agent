"""Frozen byte contracts for the pre-agent-core prompt renderer migration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from uta.testgen.prompts.loader import render_prompt, render_prompt_split


def _digest(text: str) -> tuple[int, str]:
    payload = text.encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


_COMMON = {
    "rdc_context_abs": "",
    "stage_introspect_abs": "",
}

_PROMPT_CASES = {
    "plan_tests": {
        **_COMMON,
        "batch": ["com.example.订单Service"],
        "coverage_gate": 80,
        "quality_mode": "class_batch",
        "ci_diff_coverage_gate": 95,
        "ci_diff_mutation_gate": 100,
        "strict_coverage_classes": [],
        "target_context_files": "- `com.example.订单Service` → `/ctx/订单.md`",
        "roi_enabled": False,
        "index_query_command": "/opt/uta/bin/uta-query-index",
        "spec_context": "",
    },
    "generate_test": {
        **_COMMON,
        "class_fqn": "com.example.订单Service",
        "source_path": "/repo/src/main/java/com/example/订单Service.java",
        "target_context_abs": "/ctx/订单.context.md",
        "target_symbols_abs": "/ctx/订单.symbols.md",
        "index_query_command": "/opt/uta/bin/uta-query-index",
        "wave_one_only": False,
        "maven_instructions": "\n\nRun deterministic verification.",
        "maven_module_flag": " -pl biz -am",
        "test_class_name": "订单ServiceTest",
        "coverage_gate": 80,
        "quality_mode": "class_batch",
        "ci_diff_coverage_gate": 95,
        "ci_diff_mutation_gate": 100,
        "run_id": "run-固定-001",
        "mockito_api_guidance": "",
        "spec_context": "",
        "context_summary_abs": "/ctx/context_summary.md",
        "test_guidance_abs": "/ctx/test_guidance.md",
        "repo_summary_exists": False,
        "repo_summary_abs": "/repo/PROJECT_SUMMARY.md",
        "compile_facts_exists": False,
        "compile_facts_abs": "/ctx/compile_facts.md",
    },
    "fix_compile": {
        **_COMMON,
        "class_fqn": "com.example.订单Service",
        "compile_errors": "[ERROR] 找不到符号 Foo\n[ERROR] incompatible types",
        "test_file_path": "biz/src/test/java/com/example/订单ServiceTest.java",
        "maven_module_flag": " -pl biz -am",
        "mockito_api_guidance": "",
        "target_context_abs": "/ctx/订单.context.md",
        "target_symbols_abs": "/ctx/订单.symbols.md",
    },
    "fix_coverage": {
        **_COMMON,
        "class_fqn": "com.example.订单Service",
        "current_coverage": 42.25,
        "coverage_gate": 80,
        "source_path": "/repo/订单Service.java",
        "test_file_path": "/repo/订单ServiceTest.java",
        "test_class_name": "订单ServiceTest",
        "maven_module_flag": "",
        "target_context_abs": "/ctx/订单.context.md",
        "target_symbols_abs": "/ctx/订单.symbols.md",
        "uncovered_summary": "- `calculate(金额)` lines 20-28",
        "roi_abs": "",
    },
    "fix_mutations": {
        **_COMMON,
        "class_fqn": "com.example.订单Service",
        "current_coverage": 81.25,
        "current_mutation_score": 55.5,
        "mutation_gate": 70,
        "source_path": "/repo/订单Service.java",
        "test_file_path": "/repo/订单ServiceTest.java",
        "target_context_abs": "/ctx/订单.context.md",
        "target_symbols_abs": "/ctx/订单.symbols.md",
        "mutation_family_summary_abs": "/ctx/订单.mutations.md",
        "mutation_family_summary": "- boundary: 边界值 100",
        "mutation_roi_enabled": False,
        "mutation_roi_skip_expensive": False,
        "surviving_mutants": [
            {"line": 42, "mutation_type": "NEGATE_CONDITIONALS", "detail": "边界"}
        ],
    },
    "python_generate_test": {
        "display_name": "jobs/订单.py",
        "target_id": "pyfile:jobs/订单.py",
        "source_path": "jobs/订单.py",
        "canonical_module": "jobs.订单",
        "symbol": "",
        "syntax_version": "python3",
        "parser_backend": "tree-sitter",
        "generated_test_path": "tests/uta_generated/test_订单.py",
        "existing_test_path": "",
        "existing_test_reasons": [],
        "context_abs": "/ctx/订单.md",
        "context_json_abs": "/ctx/订单.json",
        "index_query_command": "/opt/uta/bin/uta-query-index",
        "companion_files": [],
        "side_effect_hints": [],
        "changed_line_hints": [],
        "scored_methods": [],
        "prior_hints": [],
        "spec_context": "",
    },
    "python_fix_compile": {
        "display_name": "jobs/订单.py",
        "target_id": "pyfile:jobs/订单.py",
        "source_path": "jobs/订单.py",
        "canonical_module": "jobs.订单",
        "symbol": "",
        "generated_test_path": "tests/uta_generated/test_订单.py",
        "test_file_path": "tests/uta_generated/test_订单.py",
        "context_abs": "/ctx/订单.md",
        "target_context_abs": "/ctx/订单.md",
        "context_json_abs": "/ctx/订单.json",
        "index_query_command": "/opt/uta/bin/uta-query-index",
        "compile_errors": "SyntaxError: 无效语法",
    },
    "python_fix_coverage": {
        "display_name": "jobs/订单.py",
        "target_id": "pyfile:jobs/订单.py",
        "source_path": "jobs/订单.py",
        "canonical_module": "jobs.订单",
        "symbol": "",
        "generated_test_path": "tests/uta_generated/test_订单.py",
        "test_file_path": "tests/uta_generated/test_订单.py",
        "coverage_gate": 80.0,
        "ci_diff_coverage_gate": 95,
        "context_abs": "/ctx/订单.md",
        "target_context_abs": "/ctx/订单.md",
        "context_json_abs": "/ctx/订单.json",
        "index_query_command": "/opt/uta/bin/uta-query-index",
        "coverage_diagnostics": "line 18: 未覆盖分支",
        "coverage_report": "",
    },
    "python_fix_mutations": {
        "display_name": "jobs/订单.py",
        "target_id": "pyfile:jobs/订单.py",
        "source_path": "jobs/订单.py",
        "canonical_module": "jobs.订单",
        "symbol": "",
        "generated_test_path": "tests/uta_generated/test_订单.py",
        "test_file_path": "tests/uta_generated/test_订单.py",
        "mutation_gate": 70.0,
        "ci_diff_mutation_gate": 100,
        "context_abs": "/ctx/订单.md",
        "target_context_abs": "/ctx/订单.md",
        "context_json_abs": "/ctx/订单.json",
        "index_query_command": "/opt/uta/bin/uta-query-index",
        "mutation_repair_context_abs": "",
        "mutation_repair_roi_guided_full": False,
        "mutation_repair_split": False,
        "mutation_repair_group_abs": "",
        "mutation_repair_group": "",
        "mutation_diagnostics": "line 24: 幸存变异",
        "mutation_report": "",
    },
}


# (single length/hash, stable length/hash, volatile length/hash, composed length/hash)
_GOLDEN = {
    # Filled from the pre-migration renderer; changes require an intentional
    # byte-contract review rather than snapshot normalization.
    "plan_tests": (
        (6211, "db2111e94d324d4a1606382bdd84e19c119a8645333bfb4d6b082b2234d55063"),
        (5166, "362cecc092748c3e28d33cff2d8213c67d4d3f9c3058d1c162744b379abb975b"),
        (1044, "65a1c7ea435af9be43d6198ae8b4f2c23940cf817602b7dc32f99ba89f5c2eed"),
        (6210, "8990b9ca92ebe432b2bc14071c1864459163962645133528a61131885d40e61a"),
    ),
    "generate_test": (
        (6750, "5de8354e0be1aceee4c149366f7ab963b20de268035d6b1f59267469b8c84813"),
        (2511, "e9fda7167932112de0592b050f4314ba17fadd27e228cc1f0fe4105962f9c229"),
        (4238, "cf0c6ee2c3a685902c747db4003120bf10548b198ded91e3b7fb4657b309fa89"),
        (6749, "8b69b84855a64714d51b5cd074ff72342f7bb9bd31ca1d48101f53033d29bdd8"),
    ),
    "fix_compile": (
        (2158, "622e6f5dca9264a55b796709ee5318d4916010ff8162e77823c6c4eff761d815"),
        (1836, "e43bcecb2147966b3efff2e83e4d7bed78c80b116df31754b804570351ebc930"),
        (321, "42969c116d6a6489340908df79f47aa5b7d29e42ebaf41a76d62d14e1f3c5dcb"),
        (2157, "0ff07d9b11bc4e18874ae7203702b4e84c4e8e2079020474aa6a8fa87ff693d8"),
    ),
    "fix_coverage": (
        (2827, "c147676bc2da064078c579cfb9c622b09bfa3e45ebb9d4b2c89c0eafca74b161"),
        (2234, "08fae5cf30753fb8f2fadc3ad7a3b0ed435e26f3ee5bf2fa98a14fbce0b312c7"),
        (592, "6f17943256a17603ebbaa0ed39b1ed50b64041a270426745f425a28d6ca95247"),
        (2826, "936c42da587b909da732e9a423a920f50bc4f9901a73d5f311658f4dbceba93f"),
    ),
    "fix_mutations": (
        (4954, "d990c4af23ee0da7eaeeeef0d29b0b66f4db1d73bdf6222b3c5ca026b7a2c8a1"),
        (4438, "91acbd2b86242ec08b720f0fbd5d91e1974ebdd6f2dca97041c9f9998f6a40ed"),
        (515, "c0022641c5b2c56a5465eb6d681c8d4674f3ea724d4648c0e1560b25a28655c7"),
        (4953, "dff4135d292f866ea1e0e6bfb42feb9fbb152fb4188741241a218d962c1266f6"),
    ),
    "python_generate_test": (
        (4947, "458573cdcb35a5d4e401fb04e6ee36b133e546802dc46c35c6ba8b64acfde215"),
        (3449, "73565fd79c3e40df0e9711549da59ef6b0ed838516c23a9c9a359a4b646eae79"),
        (1497, "4d05164dd2c2650783df57cb3251ebc003825ea4735026dcabca57f8766d35a0"),
        (4946, "3b896e2a1a83abd1c91a1c443ab3cd6bc2f1e26bad67b7502c408a8b71e5232c"),
    ),
    "python_fix_compile": (
        (2239, "706bdca41f87de3730990dcf0ad3cdde1a260c0d4c18ed1849c93251b5bfa595"),
        (1668, "27f7f7193d0cf365894598a2879e33600c679faf06c62c2e9947de301acb3126"),
        (570, "3539d0d6bfc4aa3d9a950b438ab255caa00a484743bb0c4d502fb97a399b722c"),
        (2238, "5eca07cca745d9a53237c3e321d43f89a7c61a8f65d02ef7a140b9992ea4cb0e"),
    ),
    "python_fix_coverage": (
        (2781, "ebc8dbac8678d3a66009918f701cf552d7bfdb9ce5bf472900b9e0c4fdc48a41"),
        (2249, "d0faaadb960ababa4a170385d8f5c16124c51665fd8bab19d1bf4084b6ccf6d6"),
        (531, "5f5b2a8a3468cce3c01844e26bad9da7784de838fd4a5b96b37c0e913262a122"),
        (2780, "87536a24944bc2b88fa41c9796a16d3bd09ba9b49eb91ae64f3c048bbe8014dc"),
    ),
    "python_fix_mutations": (
        (5357, "da7ccc3fb52ea4122af1edb803f7b53b7ae35b1d107a796627a1e99b48b21497"),
        (3984, "0cce62ca95aeb6b10665c822671f4c9b9b8a4e46859e9e05128a7b40175cab64"),
        (1372, "336f45533d192491629c51cc481c36eacba5495d1f32d6d39d9ca97ba3ca062e"),
        (5356, "2eb9dd5b4d422e1e07191ab2eb7db64301afc1042f865d9e09d9260922f4d507"),
    ),
}


@pytest.fixture(autouse=True)
def _clear_rdc_context_environment(monkeypatch):
    monkeypatch.delenv("UTA_RDC_CONTEXT_PATH", raising=False)


@pytest.mark.parametrize("name", sorted(_PROMPT_CASES))
def test_all_production_prompt_bytes_are_frozen(name):
    values = _PROMPT_CASES[name]
    single = render_prompt(name, **values)
    stable, volatile = render_prompt_split(name, **values)
    composed = stable + volatile

    assert (_digest(single), _digest(stable), _digest(volatile), _digest(composed)) == _GOLDEN[name]
    assert single == composed[: len(stable)] + "\n" + composed[len(stable) :]


def test_prompt_hashes_match_the_workflow_v2_contract():
    contract = json.loads(
        (Path(__file__).parent / "fixtures/contracts/uta_workflow_v2.json").read_text(
            encoding="utf-8"
        )
    )

    assert {
        name: hashes[0][1] for name, hashes in sorted(_GOLDEN.items())
    } == contract["prompt_sha256"]
