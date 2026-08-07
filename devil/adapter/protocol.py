"""Capability-gated, serialized adapter boundary and normalized result envelope."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from devil.core.snapshot import BaseForkFingerprint
from devil.core.types import (
    Call,
    Candidate,
    ChainId,
    Constraint,
    CrossChainStep,
    Event,
    Evidence,
    Outcome,
    SlotChange,
    frozen_mapping,
)


@dataclass(frozen=True)
class ToolCapabilities:
    name: str
    static_analysis: bool = False
    stateful_fuzzing: bool = False
    symbolic_execution: bool = False
    concrete_replay: bool = False
    shrinking: bool = False
    supported_targets: tuple[str, ...] = ("solidity",)
    supported_artifacts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    digest: str
    path: str = ""


@dataclass(frozen=True)
class Diagnostic:
    message: str
    code: str = ""
    severity: str = "warning"


@dataclass(frozen=True)
class ToolRunResult[T]:
    outcome: Outcome
    value: T | None = None
    evidence: tuple[Evidence, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    bounds: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.bounds is not None:
            object.__setattr__(self, "bounds", frozen_mapping(self.bounds))
        if (
            self.outcome in {Outcome.TIMEOUT, Outcome.TOOL_ERROR, Outcome.UNSUPPORTED}
            and not self.diagnostics
        ):
            raise ValueError(f"{self.outcome.value} result requires a diagnostic")


def artifact_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StaticHint:
    context_id: str
    selector: str
    kind: str
    source_locations: tuple[str, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    suspicion: float = 0.0
    producer: str = ""


@dataclass(frozen=True)
class MaterializedSlot:
    context_id: str
    address: str
    slot: str
    value: str


@dataclass(frozen=True)
class MaterializedCode:
    context_id: str
    address: str
    bytecode: str
    code_hash: str


@dataclass(frozen=True)
class StateManifest:
    slots: tuple[MaterializedSlot, ...] = ()
    code: tuple[MaterializedCode, ...] = ()


@dataclass(frozen=True)
class ChainProjection:
    chain_id: ChainId
    base_fingerprint: BaseForkFingerprint
    materialized_state: StateManifest
    relevant_message_ids: tuple[str, ...] = ()
    block_number_delta: int = 0
    timestamp_delta: int = 0
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectionManifest:
    chain: ChainId
    target_context: str
    target_address: str
    state: StateManifest
    block_number: int
    timestamp: int
    omitted_state: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain.value,
            "target_context": self.target_context,
            "target_address": self.target_address,
            "slots": [vars(item) for item in self.state.slots],
            "code": [vars(item) for item in self.state.code],
            "block_number": self.block_number,
            "timestamp": self.timestamp,
            "omitted_state": list(self.omitted_state),
        }


@dataclass(frozen=True)
class WitnessProjection:
    chain_projection: ChainProjection
    trace_segment: tuple[CrossChainStep, ...]
    violated_clause: str
    projection_manifest: ProjectionManifest


@dataclass(frozen=True)
class ReplayResult:
    applied: bool
    state_diff: tuple[SlotChange, ...] = ()
    events: tuple[Event, ...] = ()
    revert_data: str = ""


ExecutionResult = ReplayResult


@dataclass(frozen=True)
class BoundedConfirmation:
    reproduced: bool
    projection_manifest_hash: str
    assumptions: tuple[str, ...]
    omitted_state: tuple[str, ...]


ProbeArtifact = StaticHint | Candidate | BoundedConfirmation


class ToolAdapter(Protocol):
    capabilities: ToolCapabilities

    def probe(
        self,
        target: str,
        invariant_id: str,
        constraints: tuple[Constraint, ...],
        projection: ChainProjection,
        chain: ChainId,
        **options: Any,
    ) -> ToolRunResult[list[ProbeArtifact]]: ...

    def execute(
        self,
        target: str,
        trace: tuple[CrossChainStep, ...],
        base_fingerprint: BaseForkFingerprint,
        chain: ChainId,
        **options: Any,
    ) -> ToolRunResult[ReplayResult]: ...

    def confirm(
        self,
        target: str,
        witness_projection: WitnessProjection,
        chain: ChainId,
        **options: Any,
    ) -> ToolRunResult[BoundedConfirmation]: ...


@dataclass
class ArtifactStore:
    values: dict[str, object] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def put(self, value: object) -> ArtifactRef:
        digest = artifact_digest(value)
        self.values[digest] = value
        return ArtifactRef(type(value).__name__, digest)

    def get(self, digest: str) -> object:
        try:
            return self.values[digest]
        except KeyError as exc:
            raise KeyError(f"unknown artifact {digest}") from exc


def candidate_from_hint(hint: StaticHint, chain: ChainId) -> Candidate:
    """Convert only selectors already represented by a validated static hint."""
    return Candidate(
        target_function=hint.selector,
        call_sequence=(
            Call(
                hint.selector,
                chain=chain,
                context_id=hint.context_id,
                source=hint.producer,
            ),
        ),
        pre_conditions=hint.constraints,
        suspicion=hint.suspicion,
        chain=chain,
    )


def project_chain(
    state: Any,
    base_fingerprint: BaseForkFingerprint,
    chain: ChainId,
    *,
    code: tuple[MaterializedCode, ...] = (),
) -> ChainProjection:
    """Serialize one branch's future-relevant chain state for a tool."""
    snapshot = state.chain_snapshots[chain]
    slots = tuple(
        MaterializedSlot(change.contract, change.contract, change.slot, change.new_value)
        for change in snapshot.state_diff
    )
    relevant = tuple(
        sorted(
            identity
            for identity, message in state.pending_messages.items()
            if chain
            in {
                message.envelope.source_chain,
                message.envelope.destination_chain,
            }
        )
    )
    return ChainProjection(
        chain,
        base_fingerprint,
        StateManifest(slots, code),
        relevant,
        snapshot.block_number_delta,
        snapshot.timestamp_delta,
        tuple(str(item) for item in state.assumptions),
    )
