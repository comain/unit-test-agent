"""Product-owned operation ledger and durable result artifacts.

This was one 1,009-line module. The seam is what each part answers:

* `models` -- the values every other part speaks in, so none of them has to
  depend on another just to name a result.
* `cost_gate` -- the spend ceiling a run is held to.
* `artifact_store` -- writing a result to disk so a crash cannot half-write it.
* `reconciliation` -- deciding what actually happened when a run stopped
  without saying.
* `ledger` -- recording an outcome, and composing the two halves.

The import path is unchanged: everything the module used to export is
re-exported here, because the callers and tests that name them are not what
this change is about.
"""

from uta.testgen.operations.artifact_store import OperationArtifactStore
from uta.testgen.operations.cost_gate import WorkflowCostGate
from uta.testgen.operations.ledger import WorkflowOperationLedger
from uta.testgen.operations.models import (
    ArtifactValidationError,
    OperationIdentity,
    OperationResultEnvelope,
    StoredArtifact,
)

__all__ = [
    "ArtifactValidationError",
    "OperationArtifactStore",
    "OperationIdentity",
    "OperationResultEnvelope",
    "StoredArtifact",
    "WorkflowCostGate",
    "WorkflowOperationLedger",
]
