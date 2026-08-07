"""Typed tool adapters and candidate-worker aggregation."""

from devil.adapter.protocol import (
    ArtifactRef,
    ArtifactStore,
    BoundedConfirmation,
    ChainProjection,
    Diagnostic,
    ExecutionResult,
    MaterializedCode,
    MaterializedSlot,
    ProjectionManifest,
    ReplayResult,
    StateManifest,
    StaticHint,
    ToolAdapter,
    ToolCapabilities,
    ToolRunResult,
    WitnessProjection,
    project_chain,
)
from devil.adapter.registry import AdapterRegistry, CandidateWorkers

__all__ = [
    "AdapterRegistry",
    "ArtifactRef",
    "ArtifactStore",
    "BoundedConfirmation",
    "CandidateWorkers",
    "ChainProjection",
    "Diagnostic",
    "ExecutionResult",
    "MaterializedCode",
    "MaterializedSlot",
    "ProjectionManifest",
    "project_chain",
    "ReplayResult",
    "StateManifest",
    "StaticHint",
    "ToolAdapter",
    "ToolCapabilities",
    "ToolRunResult",
    "WitnessProjection",
]
