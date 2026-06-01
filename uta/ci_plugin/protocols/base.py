from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from uta.ci_plugin.models import CiTaskRecord, CiTriggerRequest


@dataclass
class CiResult:
    """Protocol-neutral outcome of a CI check, handed to a protocol for reporting."""

    passed: bool
    summary: str
    report_url: str


@dataclass
class CiCallbackOutcome:
    """Result of reporting a :class:`CiResult` back to the originating CI system."""

    succeeded: bool
    history: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ProtocolResponse:
    """The synchronous HTTP reply a protocol returns for an inbound trigger.

    Kept framework-light (status + body) so protocols stay testable; the route
    layer turns it into a ``JSONResponse``.
    """

    status_code: int
    body: Dict[str, Any]


class CiContextProvider(ABC):
    """Supplies the protocol-specific *issue* context used during repair.

    CI integrations can provide issue metadata behind this interface. The generic skeleton is assembled by
    :func:`uta.ci_plugin.context.assemble_base_context`; providers only decide
    what goes in the ``issue`` section.
    """

    name: str

    @abstractmethod
    def build_context(
        self,
        record: CiTaskRecord,
        *,
        user_context: Optional[str] = None,
        commit_messages: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        ...

    def enrich_repair_context(
        self,
        record: CiTaskRecord,
        context: Dict[str, Any],
        repo_task: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Re-enrich a persisted context (e.g. re-fetch the issue description).

        Default is a no-op; protocols that can refresh issue data override it.
        """
        return context


class CiProtocol(ABC):
    """Adapts one CI system's wire format to the protocol-neutral service core.

    Responsibilities: verify the inbound request, parse it into a
    :class:`CiTriggerRequest`, format the synchronous trigger reply, and report
    the final :class:`CiResult` back to the originating system.
    """

    name: str
    context_provider: CiContextProvider

    def verify(self, body: bytes, headers: Mapping[str, str]) -> None:
        """Reject forged/untrusted requests. Default trusts the caller (no-op)."""
        return None

    @abstractmethod
    def parse_trigger(self, body: bytes, headers: Mapping[str, str]) -> Optional[CiTriggerRequest]:
        """Parse a trigger, or return ``None`` for events that should be ignored."""

    @abstractmethod
    def trigger_response(
        self,
        record: CiTaskRecord,
        *,
        task_url: str,
        report_url: str,
    ) -> ProtocolResponse:
        ...

    def ignored_response(self) -> ProtocolResponse:
        """Reply for an accepted-but-ignored event (e.g. an unrelated webhook)."""
        return ProtocolResponse(status_code=200, body={"status": "ignored"})

    @abstractmethod
    def error_response(self, exc: Exception) -> ProtocolResponse:
        """Reply for a verification/parse failure, in this protocol's format."""

    @abstractmethod
    def can_report(self, record: CiTaskRecord) -> bool:
        ...

    def reporting_configured(self) -> bool:
        """Whether this protocol has enough static configuration to report results."""
        return False

    @abstractmethod
    def report_result(self, record: CiTaskRecord, result: CiResult) -> CiCallbackOutcome:
        ...
