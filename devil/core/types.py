"""Immutable kernel types for cross-chain mainnet-fork campaigns."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ChainId(StrEnum):
    ETHEREUM = "ethereum"
    POLYGON = "polygon"
    ARBITRUM = "arbitrum"
    OPTIMISM = "optimism"
    FANTOM = "fantom"

    @classmethod
    def _missing_(cls, value: str) -> ChainId:
        obj = str.__new__(cls, value)
        obj._name_ = value
        obj._value_ = value
        return obj


class Verdict(StrEnum):
    VIOLATED = "violated"
    NOT_OBSERVED = "not_observed"
    INCONCLUSIVE = "inconclusive"


class EvidenceStrength(StrEnum):
    OBSERVED = "observed"
    REPLAYED = "replayed"
    SYMBOLICALLY_CONFIRMED = "symbolically_confirmed"
    SYMBOLICALLY_CONFIRMED_UNDER_PROJECTED_STATE = "symbolically_confirmed_under_projected_state"


class Outcome(StrEnum):
    SUCCESS = "success"
    COUNTEREXAMPLE = "counterexample"
    UNSAT_UNDER_BOUNDS = "unsat_under_bounds"
    TIMEOUT = "timeout"
    TOOL_ERROR = "tool_error"
    UNSUPPORTED = "unsupported"
    PARTIAL = "partial"


class ViolationSource(StrEnum):
    PRE_EXISTING_AT_SNAPSHOT = "pre_existing_at_snapshot"
    INTRODUCED_BY_TRACE = "introduced_by_trace"
    AMPLIFIED_BY_TRACE = "amplified_by_trace"
    INCONCLUSIVE_DUE_TO_MISSING_RELAY_DATA = "inconclusive_due_to_missing_relay_data"


class RelayMode(StrEnum):
    HISTORICAL_AUTHENTIC = "historical-authentic"
    PROTOCOL_VALID_SYNTHETIC = "protocol-valid-synthetic"
    MODELED_RELAY = "modeled-relay"
    RAW_PAYLOAD = "raw-payload"


class AttackerModel(StrEnum):
    PERMISSIONLESS = "permissionless"
    COMPROMISED_SIGNER = "compromised_signer"
    COMPROMISED_GOVERNANCE = "compromised_governance"
    PRIVILEGED_OPERATOR = "privileged_operator"
    STATE_ONLY = "state_only"


class BaselineStatus(StrEnum):
    HOLDS = "holds"
    PENDING = "pending"
    VIOLATED = "violated"
    UNOBSERVABLE = "unobservable"
    INCONCLUSIVE = "inconclusive"


class ExecutionStatus(StrEnum):
    APPLIED = "applied"
    REVERTED = "reverted"


class RevertKind(StrEnum):
    EVM_REVERT = "evm_revert"
    OUT_OF_GAS = "out_of_gas"


class MessageStatus(StrEnum):
    EMITTED = "emitted"
    SOURCE_FINALIZED = "source_finalized"
    RELAY_ELIGIBLE = "relay_eligible"
    DELIVERED = "delivered"
    CONSUMED = "consumed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class RelayAction(StrEnum):
    FINALIZE = "finalize"
    MAKE_ELIGIBLE = "make_eligible"
    DELIVER = "deliver"
    CONSUME = "consume"
    REJECT = "reject"
    EXPIRE = "expire"


class EnvironmentReason(StrEnum):
    FINALITY = "finality"
    RELAY_DELAY = "relay_delay"
    EXPIRY = "expiry"
    OBSERVATION = "observation"
    LIVENESS_DEADLINE = "liveness_deadline"


class LivenessStatus(StrEnum):
    ACTIVE = "active"
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    INCONCLUSIVE = "inconclusive"


def deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value


def frozen_mapping[K, V](value: Mapping[K, V] | None = None) -> Mapping[K, V]:
    """Return an immutable defensive deep copy of a mapping."""
    return MappingProxyType({key: deep_freeze(item) for key, item in (value or {}).items()})


@dataclass(frozen=True)
class Constraint:
    kind: str
    target: str
    value: Any
    chain: ChainId
    source: str
    provenance_hash: str = ""


@dataclass(frozen=True)
class Actor:
    address: str
    role: str = "attacker"
    provenance: str = "fork_state"
    privilege_level: str = "none"
    impersonation_allowed: bool = True
    funding_method: str = "from_fork_balance"


@dataclass(frozen=True)
class ActorPolicy:
    id: str
    policy_hash: str
    actors: tuple[Actor, ...] = ()

    def permits(self, actor: Actor) -> bool:
        return actor.impersonation_allowed and any(
            configured.address.lower() == actor.address.lower()
            and configured.role == actor.role
            and configured.privilege_level == actor.privilege_level
            for configured in self.actors
        )


@dataclass(frozen=True)
class Call:
    function_signature: str
    args: tuple[Any, ...] = ()
    chain: ChainId | None = None
    context_id: str = ""
    calldata: str = ""
    value: int = 0
    actor: Actor | None = None
    gas_limit: int = 0
    source: str | None = None


@dataclass(frozen=True)
class RelayTransition:
    message_id: str
    action: RelayAction
    from_status: MessageStatus
    to_status: MessageStatus
    source_chain: ChainId
    destination_chain: ChainId
    relay_mode: RelayMode
    policy_ref: str


@dataclass(frozen=True)
class EnvironmentTransition:
    chain: ChainId
    target_block: int
    target_timestamp: int
    reason: EnvironmentReason
    policy_ref: str


CrossChainStep = Call | RelayTransition | EnvironmentTransition


@dataclass(frozen=True)
class Evidence:
    tool: str
    outcome: Outcome
    raw: str
    raw_hash: str | None = None
    trace: str | None = None
    artifact_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    target_function: str
    call_sequence: tuple[CrossChainStep, ...] = ()
    pre_conditions: tuple[Constraint, ...] = ()
    suspicion: float = 0.0
    evidence: Evidence | None = None
    chain: ChainId | None = None
    actor: Actor | None = None


@dataclass(frozen=True)
class SlotChange:
    contract: str
    slot: str
    old_value: str | None = None
    new_value: str = "0x"


@dataclass(frozen=True)
class CodeChange:
    context_id: str
    old_code_hash: str
    new_code_hash: str


@dataclass(frozen=True)
class Event:
    context_id: str
    signature: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    transaction_hash: str = ""
    log_index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", frozen_mapping(self.fields))


@dataclass(frozen=True)
class ForkSnapshot:
    chain_id: ChainId
    base_block: int
    base_block_hash: str = ""
    state_root: str = ""
    base_timestamp: int = 0
    backend_handle: str = ""
    overlay_id: int = 0
    state_diff: tuple[SlotChange, ...] = ()
    code_diff: tuple[CodeChange, ...] = ()
    touched_slots: tuple[str, ...] = ()
    emitted_logs: tuple[Event, ...] = ()
    block_number_delta: int = 0
    timestamp_delta: int = 0

    @property
    def block_number(self) -> int:
        return self.base_block + self.block_number_delta

    @property
    def timestamp(self) -> int:
        return self.base_timestamp + self.timestamp_delta


@dataclass(frozen=True)
class RelayMessage:
    emitter: str
    sequence: int
    source_chain: ChainId
    destination_chain: ChainId
    destination_context: str
    payload: str = ""
    payload_hash: str = ""
    attestation: str = ""
    attestation_hash: str = ""
    message_id: str = ""
    correlation_value: str = ""
    source_block_number: int = 0
    source_block_hash: str = ""
    source_log_index: int = 0
    source_event_hash: str = ""
    emitted_timestamp: int = 0
    guardian_set_index: int = 0
    destination_status: str = "unknown"
    status_evidence_hash: str = ""
    protocol_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "protocol_metadata", frozen_mapping(self.protocol_metadata))

    @property
    def identity(self) -> str:
        return self.message_id or f"{self.emitter}:{self.sequence}"

    @property
    def vaa_bytes(self) -> str:
        return self.attestation

    @property
    def vaa_hash(self) -> str:
        return self.attestation_hash


@dataclass(frozen=True)
class RelayDataset:
    schema_version: str
    dataset_hash: str
    protocol: str
    source_block_ranges: Mapping[ChainId, tuple[int, int]]
    messages: tuple[RelayMessage, ...] = ()
    provenance: str = ""
    provenance_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_block_ranges", frozen_mapping(self.source_block_ranges))
        identities = [message.identity for message in self.messages]
        if len(identities) != len(set(identities)):
            raise ValueError("relay dataset contains duplicate message identities")


@dataclass(frozen=True)
class RelayTransitionRecord:
    action: RelayAction
    from_status: MessageStatus
    to_status: MessageStatus
    block_number: int
    timestamp: int
    evidence_hash: str = ""


@dataclass(frozen=True)
class MessageState:
    envelope: RelayMessage
    status: MessageStatus = MessageStatus.EMITTED
    transition_history: tuple[RelayTransitionRecord, ...] = ()


@dataclass(frozen=True)
class LivenessObligation:
    id: str
    binding_key: tuple[Any, ...]
    correlation_value: str | None
    clock_chain: ChainId
    start_block: int
    start_timestamp: int
    deadline_value: int
    deadline_unit: str
    status: LivenessStatus = LivenessStatus.ACTIVE
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class GlobalState:
    chain_snapshots: Mapping[ChainId, ForkSnapshot] = field(default_factory=dict)
    snapshot_set_id: str = ""
    pending_messages: Mapping[str, MessageState] = field(default_factory=dict)
    relay_dataset_hash: str = ""
    observation_set_hash: str = ""
    relay_policy_hash: str = ""
    relay_mode: RelayMode = RelayMode.MODELED_RELAY
    actor_policy: ActorPolicy | None = None
    trace: tuple[CrossChainStep, ...] = ()
    assumptions: tuple[Any, ...] = ()
    liveness_obligations: Mapping[str, LivenessObligation] = field(default_factory=dict)
    observed_values: Mapping[str, Any] = field(default_factory=dict)
    budget_used: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain_snapshots", frozen_mapping(self.chain_snapshots))
        object.__setattr__(self, "pending_messages", frozen_mapping(self.pending_messages))
        object.__setattr__(self, "liveness_obligations", frozen_mapping(self.liveness_obligations))
        object.__setattr__(self, "observed_values", frozen_mapping(self.observed_values))

    def with_snapshot(self, chain_id: ChainId, snapshot: ForkSnapshot) -> GlobalState:
        snapshots = dict(self.chain_snapshots)
        snapshots[chain_id] = snapshot
        return replace(self, chain_snapshots=snapshots)

    def with_messages(self, messages: Mapping[str, MessageState]) -> GlobalState:
        return replace(self, pending_messages=messages)

    def with_observed_values(self, values: Mapping[str, Any]) -> GlobalState:
        return replace(self, observed_values=values)


@dataclass(frozen=True)
class SearchState:
    global_state: GlobalState
    chain_context: ChainId
    constraints: tuple[Constraint, ...] = ()
    sequence: tuple[CrossChainStep, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    depth: int = 0
    branch_id: str = "root"
    parent_branch_id: str | None = None


@dataclass(frozen=True)
class WitnessState:
    snapshot: GlobalState
    correlation_value: str = ""
    chain: ChainId = ChainId.ETHEREUM
    branch_id: str = ""
    parent_branch_id: str | None = None
    call_sequence: tuple[CrossChainStep, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    status: str = "reachable"


@dataclass(frozen=True)
class BaselineResult:
    status: BaselineStatus = BaselineStatus.HOLDS
    reason: str = ""
    violation_source: ViolationSource | None = None


@dataclass(frozen=True)
class Impact:
    severity: str = ""
    description: str = ""
    affected_chains: tuple[ChainId, ...] = ()
    attacker_model: AttackerModel = AttackerModel.PERMISSIONLESS


@dataclass(frozen=True)
class EdgeCase:
    depth: int
    witness: WitnessState | None = None
    confirmations: tuple[Any, ...] = ()
    segment_strengths: Mapping[str, EvidenceStrength] = field(default_factory=dict)
    aggregate_strength: EvidenceStrength = EvidenceStrength.OBSERVED
    aggregation_rule: str = "weakest-full-trace-segment"
    violated_clauses: tuple[str, ...] = ()
    violation_source: ViolationSource = ViolationSource.INTRODUCED_BY_TRACE
    impact: Impact = field(default_factory=Impact)
    chains: tuple[ChainId, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "segment_strengths", frozen_mapping(self.segment_strengths))

    @property
    def evidence_strength(self) -> EvidenceStrength:
        return self.aggregate_strength

    @property
    def witnesses(self) -> tuple[WitnessState, ...]:
        return (self.witness,) if self.witness is not None else ()


@dataclass(frozen=True)
class CanonicalExecutionResult:
    outcome: Outcome
    execution_status: ExecutionStatus | None = None
    revert_data: str | None = None
    revert_kind: RevertKind | None = None
    global_state: GlobalState | None = None
    events: tuple[Event, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    impact: Impact | None = None
    reason: str = ""


@dataclass(frozen=True)
class SearchResult:
    witnesses: tuple[WitnessState, ...] = ()
    edges: tuple[EdgeCase, ...] = ()
    deepest_edge: EdgeCase | None = None
    baseline: BaselineResult = field(default_factory=BaselineResult)
    budget_exhausted: bool = False
    exhausted: bool = False
    outcome: Verdict = Verdict.NOT_OBSERVED
    budget_used: int = 0
    budget_total: int = 200
    incomplete_outcomes: tuple[Outcome, ...] = ()
