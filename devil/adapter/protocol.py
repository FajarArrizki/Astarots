"""Adapter protocol — uniform interface for tool adapters.

Each adapter wraps one external analysis tool. The harness calls
adapters through this protocol — never calls tools directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from devil.core.types import (
    Call,
    Candidate,
    ChainId,
    Constraint,
    Evidence,
    ForkSnapshot,
    GlobalState,
    Outcome,
    WitnessState,
)

# ── Capabilities ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolCapabilities:
    """Declared capabilities of a tool adapter."""

    name: str
    static_analysis: bool = False
    stateful_fuzzing: bool = False
    symbolic_execution: bool = False
    concrete_replay: bool = False
    shrinking: bool = False
    supported_targets: tuple[str, ...] = ("solidity",)
    supported_artifacts: tuple[str, ...] = ()


# ── Execution Result ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExecutionResult:
    """Result of executing a concrete call sequence."""

    reachable: bool
    outcome: Outcome = Outcome.SUCCESS
    revert_reason: str = ""
    before_snapshot: ForkSnapshot | None = None
    after_snapshot: ForkSnapshot | None = None
    events: tuple[dict, ...] = ()  # emitted events
    correlation_value: str = ""  # bytes32 hex, if a cross-chain event was emitted


# ── Adapter Protocol ─────────────────────────────────────────────────────────


class ToolAdapter(Protocol):
    """Interface every tool adapter must implement."""

    capabilities: ToolCapabilities

    def probe(
        self,
        target: str,
        invariant_id: str,
        constraints: tuple[Constraint, ...],
        global_state: GlobalState,
        chain: ChainId,
    ) -> Outcome:
        """Run the tool in exploration mode on one chain.

        Returns a typed Outcome. On SUCCESS or COUNTEREXAMPLE, the
        outcome carries candidate data (via the adapter's internal
        result store).
        """
        ...

    def execute(
        self,
        target: str,
        sequence: tuple[Call, ...],
        constraints: tuple[Constraint, ...],
        chain: ChainId,
    ) -> ExecutionResult:
        """Execute a concrete call sequence on one chain."""
        ...

    def confirm(
        self,
        target: str,
        witness: WitnessState,
        chain: ChainId,
    ) -> Outcome:
        """Verify a witness found by another tool.

        Must use a different analysis method than the original probe.
        """
        ...


# ── Artifact Registry ────────────────────────────────────────────────────────


@dataclass
class ArtifactStore:
    """In-memory store for tool exchange artifacts during a campaign."""

    static_hints: list[tuple[str, float]] = field(default_factory=list)
    seed_corpus: list[dict] = field(default_factory=list)
    candidate_traces: list[Candidate] = field(default_factory=list)
    constraint_sets: list[tuple[Constraint, ...]] = field(default_factory=list)
    replay_results: list[ExecutionResult] = field(default_factory=list)
    confirmations: list[Evidence] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
