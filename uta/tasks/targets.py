from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union


DEFAULT_LANGUAGE = "java"
DEFAULT_JAVA_GRANULARITY = "class"


@dataclass(frozen=True)
class TargetIdentity:
    """Language-neutral identity for one unit-test generation target."""

    language: str
    target_id: str
    display_name: str
    source_path: Optional[str] = None
    symbol: Optional[str] = None
    granularity: str = DEFAULT_JAVA_GRANULARITY

    @classmethod
    def java_class(cls, class_fqn: str) -> "TargetIdentity":
        return cls(
            language="java",
            target_id=str(class_fqn),
            display_name=str(class_fqn),
            symbol=str(class_fqn),
            granularity=DEFAULT_JAVA_GRANULARITY,
        )

    def as_selection(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "language": self.language,
            "target_id": self.target_id,
            "display_name": self.display_name,
            "granularity": self.granularity,
        }
        if self.source_path:
            payload["source_path"] = self.source_path
        if self.symbol:
            payload["symbol"] = self.symbol
        return payload


TargetRef = TargetIdentity


def coerce_target(value: Union[str, Mapping[str, Any], TargetIdentity]) -> TargetIdentity:
    if isinstance(value, TargetIdentity):
        return value
    if isinstance(value, str):
        return TargetIdentity.java_class(value)
    language = str(value.get("language") or DEFAULT_LANGUAGE)
    raw_target_id = value.get("target_id") or value.get("class_fqn") or value.get("symbol") or value.get("source_path")
    if not raw_target_id:
        raise ValueError("target_id, class_fqn, symbol, or source_path is required")
    target_id = str(raw_target_id)
    display_name = str(value.get("display_name") or value.get("class_fqn") or value.get("symbol") or target_id)
    return TargetIdentity(
        language=language,
        target_id=target_id,
        display_name=display_name,
        source_path=value.get("source_path"),
        symbol=value.get("symbol") or value.get("class_fqn"),
        granularity=str(value.get("granularity") or value.get("target_granularity") or DEFAULT_JAVA_GRANULARITY),
    )


def coerce_targets(values: Iterable[Union[str, Mapping[str, Any], TargetIdentity]]) -> List[TargetIdentity]:
    targets: List[TargetIdentity] = []
    seen = set()
    for value in values or []:
        target = coerce_target(value)
        key = (target.language, target.target_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)
    return targets


def legacy_class_fqn_for_storage(target: TargetIdentity) -> str:
    return target.target_id


def result_key(target: Union[str, Mapping[str, Any], TargetIdentity]) -> str:
    return coerce_target(target).target_id


def display_target(value: Union[Mapping[str, Any], TargetIdentity]) -> str:
    target = coerce_target(value) if not isinstance(value, Mapping) else target_identity_from_row(value)
    return target.display_name or target.target_id


def target_identity_from_row(row: Mapping[str, Any]) -> TargetIdentity:
    language = str(row.get("language") or DEFAULT_LANGUAGE)
    target_id = str(row.get("target_id") or row.get("class_fqn") or "")
    class_fqn = row.get("class_fqn")
    display_name = str(row.get("display_name") or class_fqn or target_id)
    symbol = row.get("symbol") or (class_fqn if language == "java" else None)
    return TargetIdentity(
        language=language,
        target_id=target_id,
        display_name=display_name,
        source_path=row.get("source_path"),
        symbol=symbol,
        granularity=str(row.get("target_granularity") or DEFAULT_JAVA_GRANULARITY),
    )


def event_payload_for_targets(targets: Iterable[Union[str, Mapping[str, Any], TargetIdentity]]) -> Dict[str, Any]:
    coerced = coerce_targets(targets)
    payload: Dict[str, Any] = {
        "targets": [target.as_selection() for target in coerced],
        "target_ids": [target.target_id for target in coerced],
    }
    java_fqns = [target.target_id for target in coerced if target.language == "java"]
    if java_fqns:
        payload["class_fqns"] = java_fqns
    return payload


def target_count(selection: Optional[Mapping[str, Any]]) -> int:
    if not selection:
        return 0
    targets = selection.get("targets")
    if isinstance(targets, list):
        return len(targets)
    class_fqns = selection.get("class_fqns")
    if isinstance(class_fqns, list):
        return len(class_fqns)
    return 0
