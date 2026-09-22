"""Java class eligibility policy.

This module answers one language-specific question: which parsed Java classes
contain useful unit-test behavior.  Workflow orchestration consumes the
answer; cost estimation reuses it without importing the generation pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, List


logger = logging.getLogger("uta")

_ACCESSOR_METHOD_NAMES = {"equals", "hashCode", "toString", "canEqual"}


def _method_simple_name(method_node) -> str:
    return method_node.fqn.rsplit(".", 1)[-1]


def is_accessor_like_method_name(name: str) -> bool:
    if name in _ACCESSOR_METHOD_NAMES:
        return True
    return (
        (name.startswith("get") and len(name) > 3 and name[3].isupper())
        or (name.startswith("set") and len(name) > 3 and name[3].isupper())
        or (name.startswith("is") and len(name) > 2 and name[2].isupper())
    )


def is_accessor_like_method(method_node) -> bool:
    name = _method_simple_name(method_node)
    if name in _ACCESSOR_METHOD_NAMES:
        return True
    if not is_accessor_like_method_name(name):
        return False

    complexity = method_node.metadata.get("complexity") or {}
    cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
    body_lines = int(complexity.get("body_lines", 0) or 0)
    external_calls = int(complexity.get("external_calls", 0) or 0)
    control_nodes = sum(
        int(complexity.get(key, 0) or 0)
        for key in ("branches", "loops", "catches", "ternaries", "switch_cases")
    )
    return cyclomatic <= 1 and body_lines <= 6 and control_nodes == 0 and external_calls <= 2


def _is_data_like_class_name(name: str) -> bool:
    data_suffixes = (
        "DTO", "Dto", "VO", "Vo", "AO", "Ao", "BO", "Bo", "DO", "Do", "PO", "Po",
        "Param", "Params", "Request", "Response", "Result", "Message", "Context",
        "Item", "Info", "Detail", "Entity", "Model", "Data", "Key", "Query", "Form",
    )
    return name.endswith(data_suffixes)


def _is_data_like_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    data_paths = (
        "/model/", "/bean/", "/beans/", "/param/", "/params/", "/dto/", "/vo/",
        "/entity/", "/entities/", "/query/", "/form/",
    )
    return any(part in normalized for part in data_paths)


def _is_thin_event_class_name(name: str) -> bool:
    return name.endswith(("Actor", "Listener", "Schedule", "Task"))


def _is_thin_event_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(part in normalized for part in ("/actor/", "/listener/", "/wschedule/", "/workflow/"))


def _is_business_like_class_name(name: str) -> bool:
    if _is_thin_event_class_name(name):
        return False
    business_suffixes = (
        "Biz", "BizImpl", "Service", "ServiceImpl", "Handler", "Processor", "Manager",
        "Checker", "Validator", "Strategy", "Executor", "Factory", "Rule",
        "WriterBack", "WriteBack",
    )
    return name.endswith(business_suffixes)


def _is_business_like_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if _is_thin_event_path(normalized):
        return False
    business_paths = (
        "/biz/", "/handler/", "/processor/", "/manager/",
        "/service/impl/", "/checkservice/", "/validate/", "/validator/",
    )
    return any(part in normalized for part in business_paths)


def _is_complex_single_method(method_node) -> bool:
    complexity = method_node.metadata.get("complexity") or {}
    cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
    body_lines = int(complexity.get("body_lines", 0) or 0)
    control_nodes = sum(
        int(complexity.get(key, 0) or 0)
        for key in ("branches", "loops", "catches", "ternaries", "switch_cases")
    )
    return cyclomatic >= 4 or body_lines >= 30 or control_nodes >= 3


def _is_trivial_delegate_method(method_node) -> bool:
    complexity = method_node.metadata.get("complexity") or {}
    cyclomatic = int(complexity.get("cyclomatic_approx", 1) or 1)
    body_lines = int(complexity.get("body_lines", 0) or 0)
    control_nodes = sum(
        int(complexity.get(key, 0) or 0)
        for key in ("branches", "loops", "catches", "ternaries", "switch_cases")
    )
    return cyclomatic <= 1 and body_lines <= 6 and control_nodes == 0


def _has_listener_registration_hint(name: str, methods: List[Any]) -> bool:
    if name.endswith(("Register", "Registrar", "Registry")):
        return True
    registration_annotations = {
        "WMQConsumer", "RabbitListener", "KafkaListener", "JmsListener",
        "EventListener", "Scheduled",
    }
    return any(
        str(annotation).lstrip("@").split(".")[-1] in registration_annotations
        for method in methods
        for annotation in (method.metadata.get("annotations") or [])
    )


def _is_delegate_only_registration_wrapper(name: str, behavior_methods: List[Any]) -> bool:
    if len(behavior_methods) < 2 or not _has_listener_registration_hint(name, behavior_methods):
        return False
    trivial_methods = [method for method in behavior_methods if _is_trivial_delegate_method(method)]
    return len(trivial_methods) / len(behavior_methods) >= 0.8


def is_testable_class(fqn: str, graph) -> bool:
    """Whether a parsed Java class has behavior worth generating tests for."""
    node = graph.nodes.get(fqn)
    if not node or node.kind != "class":
        return False

    annotations = node.metadata.get("annotations", [])
    name = fqn.split(".")[-1]
    path = node.file_path.lower().replace("\\", "/") if node.file_path else ""

    entry_annotations = {"Controller", "RestController", "DubboService", "RequestMapping"}
    if any(annotation in entry_annotations for annotation in annotations):
        logger.debug("Skipping %s — entry-level wrapper (%s)", fqn, annotations)
        return False

    skip_suffixes = (
        "Controller", "Facade", "Endpoint", "Resource",
        "RemoteWrapper", "Wrapper", "Adapter", "Proxy",
        "Script", "Tool", "Migration", "Patch", "Fix",
        "Config", "Configuration", "Properties", "Constant", "Constants", "Enum",
    )
    if any(name.endswith(suffix) for suffix in skip_suffixes):
        logger.debug("Skipping %s — name pattern match", fqn)
        return False

    if any(keyword in name.lower() for keyword in ("backdoor", "script", "migration", "patch", "tool", "demo", "test")):
        logger.debug("Skipping %s — name contains excluded keyword", fqn)
        return False

    if any(part in path for part in ("adapter/", "wrapper/", "controller/", "facade/", "endpoint/", "script/", "tool/", "backdoor/", "migration/")):
        logger.debug("Skipping %s — path contains excluded dir", fqn)
        return False

    modifiers = node.metadata.get("modifiers", [])
    if "abstract" in modifiers or "interface" in [node.kind]:
        logger.debug("Skipping %s — abstract/interface", fqn)
        return False

    methods = [
        candidate
        for candidate_fqn, candidate in graph.nodes.items()
        if candidate.kind == "method"
        and candidate.metadata.get("parent_fqn") == fqn
        and "private" not in candidate.metadata.get("modifiers", [])
    ]
    if not methods:
        logger.debug("Skipping %s — no public methods", fqn)
        return False

    behavior_methods = [method for method in methods if not is_accessor_like_method(method)]
    if not behavior_methods:
        logger.debug("Skipping %s — only accessor/object methods", fqn)
        return False

    data_like = _is_data_like_class_name(name) or _is_data_like_path(path)
    business_like = _is_business_like_class_name(name) or _is_business_like_path(path)
    thin_event_like = _is_thin_event_class_name(name) or _is_thin_event_path(path)
    accessor_count = len(methods) - len(behavior_methods)
    accessor_ratio = accessor_count / len(methods)
    if data_like and (not business_like or accessor_ratio >= 0.5):
        logger.debug(
            "Skipping %s — data-like class/path with weak behavior (%d behavior, %.0f%% accessors)",
            fqn,
            len(behavior_methods),
            accessor_ratio * 100,
        )
        return False

    if len(behavior_methods) == 1 and accessor_ratio >= 0.5 and _is_trivial_delegate_method(behavior_methods[0]):
        logger.debug("Skipping %s — accessor-backed thin delegator", fqn)
        return False

    if _is_delegate_only_registration_wrapper(name, behavior_methods):
        logger.debug("Skipping %s — delegate-only listener registration wrapper", fqn)
        return False

    if len(methods) == 1 and not business_like:
        if thin_event_like and _is_complex_single_method(behavior_methods[0]):
            return True
        logger.debug("Skipping %s — single behavior method without business class/path hint", fqn)
        return False

    return True


__all__ = ["is_accessor_like_method", "is_accessor_like_method_name", "is_testable_class"]
