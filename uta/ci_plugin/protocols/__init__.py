"""Pluggable CI protocol adapters for the unit-test-agent CI plugin.

Each protocol parses one CI system's webhook, supplies an issue-context provider,
and reports the check result back in that system's format. The service core stays
protocol-neutral and dispatches by ``CiTaskRecord.protocol``.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

from uta.ci_plugin.protocols.base import (
    CiCallbackOutcome,
    CiContextProvider,
    CiProtocol,
    CiResult,
    ProtocolResponse,
)


class ProtocolRegistry:
    """Name -> :class:`CiProtocol` lookup used by routes and the service."""

    def __init__(self, protocols: Iterable[CiProtocol] = ()) -> None:
        self._protocols: Dict[str, CiProtocol] = {}
        for protocol in protocols:
            self.register(protocol)

    def register(self, protocol: CiProtocol) -> None:
        self._protocols[protocol.name] = protocol

    def get(self, name: str) -> Optional[CiProtocol]:
        return self._protocols.get(name)

    def names(self) -> list[str]:
        return list(self._protocols)

    def values(self) -> list[CiProtocol]:
        return list(self._protocols.values())

    def __contains__(self, name: object) -> bool:
        return name in self._protocols


__all__ = [
    "CiCallbackOutcome",
    "CiContextProvider",
    "CiProtocol",
    "CiResult",
    "ProtocolRegistry",
    "ProtocolResponse",
]
