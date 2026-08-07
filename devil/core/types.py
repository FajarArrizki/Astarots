"""Fundamental types for cross-chain invariant testing harness.

All types are frozen (immutable) dataclasses. The harness operates on
branch-local copies — structural sharing is used where possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ── Chain & Contract Identification ──────────────────────────────────────────


class ChainId(StrEnum):
    """Chain identifier matching chain registry aliases."""

    ETHEREUM = "ethereum"
    POLYGON = "polygon"
    ARBITRUM = "arbitrum"
    FANTOM = "fantom"


# ── Outcomes ─────────────────────────────────────────────────────────────────


class Verdict(StrEnum):
    """What was observed about the invariant."""

    VIOLATED = "violated"
    NOT_OBSERVED = "not_observed"
    INCONCLUSIVE = "inconclusive"


class EvidenceStrength(StrEnum):
    """How well a finding is supported."""

    OBSERVED = "observed"
    REPLAYED = "replayed"
    SYMBOLICALLY_CONFIRMED = "symbolically_confirmed"
    SYMBOLICALLY_CONFIRMED_UNDER_PROJECTED_STATE = (
        "symbolically_confirmed_under_projected_state"
    )


class Outcome(StrEnum):
    """Typed outcome from a tool invocation."""

    SUCCESS = "success"
    COUNTEREXAMPLE = "counterexample"
    UNSAT_UNDER_BOUNDS = "unsat_under_bounds"
    TIMEOUT = "timeout"
    TOOL_ERROR = "tool_error"
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"


class ViolationSource(StrEnum):
    """Where a violation originated."""

    PRE_EXISTING_AT_SNAPSHOT = "pre_existing_at_snapshot"
    INTRODUCED_BY_TRACE = "introduced_by_trace"
    AMPLIFIED_BY_TRACE = "amplified_by_trace"
    INCONCLUSIVE_DUE_TO_MISSING_RELAY_DATA = "inconclusive_due_to_missing_relay_data"


class RelayMode(StrEnum):
    """How cross-chain relay signatures are handled."""

    HISTORICAL_AUTHENTIC = "historical_authentic"
    PROTOCOL_VALID_SYNTHETIC = "protocol_valid_synthetic"
    MODELED_RELAY = "modeled_relay"
    RAW_PAYLOAD = "raw_payload"


class AttackerModel(StrEnum):
    """Privilege level required to exploit a finding."""

    PERMISSIONLESS = "permissionless"
    COMPROMISED_GUARDIAN = "compromised_guardian"
    COMPROMISED_GOVERNANCE = "compromised_governance"
    PRIVILEGED_OPERATOR = "privileged_operator"
    STATE_ONLY = "state_only"


class BaselineStatus(StrEnum):
    """Invariant status at the fork block before any probing."""

    HOLDS = "holds"
    VIOLATED = "violated"
    UNOBSERVABLE = "unobservable"
    INCONCLUSIVE = "inconclusive"


# ── Core Types ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Constraint:
    """A named condition on contract state or execution context."""

    kind: str  # FUNCTION | STATE_VAR | TIMING | EXTERNAL_CALL | ACCESS | CROSS_CHAIN
    target: str
    value: Any
    chain: ChainId
    source: str  # which tool produced this constraint


@dataclass(frozen=True)
class Call:
    """A single function call in an execution sequence."""

    function_signature: str
    args: tuple[Any, ...] = ()
    chain: ChainId | None = None
    source: str | None = None  # which tool discovered this call


@dataclass(frozen=True)
class Evidence:
    """Tool output supporting a candidate or finding."""

    tool: str
    outcome: Outcome
    raw: str  # raw tool output (or path to artifact)
    raw_hash: str | None = None
    trace: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A ranked result from probing at a given search state."""

    target_function: str
    call_sequence: tuple[Call, ...] = ()
    pre_conditions: tuple[Constraint, ...] = ()
    suspicion: float = 0.0  # 0.0–1.0
    evidence: Evidence | None = None
    chain: ChainId | None = None


@dataclass(frozen=True)
class SlotChange:
    """A single storage slot change within a branch."""

    contract: str
    slot: str
    old_value: str | None = None  # bytes32 hex; None = not yet fetched
    new_value: str = "0x"  # bytes32 hex


@dataclass(frozen=True)
class ForkSnapshot:
    """Forked mainnet state for one chain within a branch."""

    chain_id: ChainId
    base_block: int
    base_block_hash: str = ""
    state_root: str = ""
    backend_handle: str = ""  # opaque (Echidna session id, Foundry fork id)
    overlay_id: int = 0
    state_diff: tuple[SlotChange, ...] = ()
    touched_slots: tuple[str, ...] = ()  # manifest of touched slot keys
    emitted_logs: tuple[Any, ...] = ()
    block_number_delta: int = 0
    timestamp_delta: int = 0


@dataclass(frozen=True)
class GlobalState:
    """Branch-local immutable copy of the full cross-chain state."""

    chain_snapshots: dict[ChainId, ForkSnapshot] = field(default_factory=dict)
    pending_messages: tuple[Any, ...] = ()  # Message objects, frozen
    trace: tuple[Any, ...] = ()  # CrossChainStep objects, frozen
    assumptions: tuple[str, ...] = ()
    budget_used: int = 0

    def with_snapshot(self, chain_id: ChainId, snapshot: ForkSnapshot) -> GlobalState:
        """Return a new GlobalState with an updated snapshot for a chain."""
        snapshots = dict(self.chain_snapshots)
        snapshots[chain_id] = snapshot
        return GlobalState(
            chain_snapshots=snapshots,
            pending_messages=self.pending_messages,
            trace=self.trace,
            assumptions=self.assumptions,
            budget_used=self.budget_used,
        )


@dataclass(frozen=True)
class SearchState:
    """A node in the unified beam search frontier."""

    global_state: GlobalState
    chain_context: ChainId
    constraints: tuple[Constraint, ...] = ()
    sequence: tuple[Call, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    depth: int = 0
    branch_id: str = ""  # unique lineage identifier


@dataclass(frozen=True)
class WitnessState:
    """A recorded intermediate state with correlation value."""

    snapshot: GlobalState
    correlation_value: str  # bytes32 hex
    chain: ChainId
    branch_id: str = ""
    call_sequence: tuple[Call, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    status: str = "reachable"  # "reachable" | "inconclusive"


@dataclass(frozen=True)
class BaselineResult:
    """Invariant evaluation result at the fork block before probing."""

    status: BaselineStatus = BaselineStatus.HOLDS
    reason: str = ""


@dataclass(frozen=True)
class EdgeCase:
    """A fully specified cross-chain attack vector."""

    depth: int
    witnesses: tuple[WitnessState, ...] = ()
    independently_confirmed: bool = False
    evidence_strength: EvidenceStrength = EvidenceStrength.OBSERVED
    violation_source: ViolationSource = ViolationSource.INTRODUCED_BY_TRACE
    impact: str = ""  # CRITICAL | HIGH | MEDIUM | LOW
    chains: tuple[ChainId, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Actor:
    """Who performs an action in a call sequence."""

    address: str  # 0x...
    role: str = "attacker"
    provenance: str = "fork_state"
    privilege_level: str = "none"
    impersonation_allowed: bool = True
    funding_method: str = "from_fork_balance"


@dataclass(frozen=True)
class Impact:
    """The consequence of an edge case violation."""

    severity: str = ""  # CRITICAL | HIGH | MEDIUM | LOW
    description: str = ""
    affected_chains: tuple[ChainId, ...] = ()
    attacker_model: AttackerModel = AttackerModel.PERMISSIONLESS


# ── Relay & Message Types ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RelayMessage:
    """A single Wormhole cross-chain message (VAA)."""

    emitter: str
    sequence: int
    payload: str = ""  # bytes hex
    vaa_bytes: str = ""  # bytes hex
    vaa_hash: str = ""
    guardian_set_index: int = 0
    destination_status: str = "unknown"  # delivered | pending | expired | unknown


@dataclass(frozen=True)
class RelayDataset:
    """Collection of cross-chain relay messages with provenance."""

    source_chain: ChainId
    source_block_range: tuple[int, int] = (0, 0)
    messages: tuple[RelayMessage, ...] = ()
    indexed_by: str = "sequence"
    provenance: str = ""  # indexed-logs | historical-vaas | relayer-api
    provenance_hash: str = ""


# ── Search Result ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SearchResult:
    """Complete result from a unified beam search campaign."""

    witnesses: tuple[WitnessState, ...] = ()
    deepest_edge: EdgeCase | None = None
    baseline: BaselineResult = field(default_factory=BaselineResult)
    exhausted: bool = False
    outcome: Verdict = Verdict.NOT_OBSERVED
    budget_used: int = 0
    budget_total: int = 200
