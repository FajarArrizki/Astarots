"""Invariant IR — internal representation of cross-chain invariants.

Parsed from .t.sol NatSpec tags or authored directly. The IR is the
authoritative representation — the .t.sol parser populates it, and
the scheduler consumes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from devil.core.types import ChainId

# ── Observation Policy ──────────────────────────────────────────────────────


class ObservationKind(StrEnum):
    PER_TRANSACTION = "per_transaction"
    AFTER_FINALITY = "after_finality"
    AFTER_ALL_DELIVERED = "after_all_delivered"
    BLOCK_BOUNDED = "block_bounded"


@dataclass(frozen=True)
class QuiescenceRule:
    """When is the system considered quiescent?"""

    kind: str = "no_pending_messages"  # no_pending | no_eligible | bounded_by_block
    max_pending_age: int | None = None
    exclude_expired: bool = True
    exclude_rejected: bool = True


@dataclass(frozen=True)
class ObservationPolicy:
    """Determines when an invariant is checked."""

    kind: ObservationKind = ObservationKind.PER_TRANSACTION
    deadline: int | None = None
    deadline_unit: str | None = None  # "blocks" | "seconds"
    finality_blocks: int | None = None
    quiescence: QuiescenceRule | None = None


# ── Assumption ───────────────────────────────────────────────────────────────


class AssumptionKind(StrEnum):
    GUARDIAN_HONESTY = "guardian_honesty"
    MESSAGE_ORDERING = "message_ordering"
    REORG_DEPTH = "reorg_depth"
    LIVENESS = "liveness"


@dataclass(frozen=True)
class Assumption:
    """Scope assumption — NOT checked, defines invariant boundaries."""

    kind: AssumptionKind
    value: str  # e.g. "at_most_6_malicious"


# ── Context & Binding ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProxyInfo:
    """If the target contract is behind a proxy."""

    kind: str = ""  # transparent | uups | beacon
    implementation_address: str = ""
    implementation_code_hash: str = ""


@dataclass(frozen=True)
class Context:
    """Per-chain contract binding with fork configuration."""

    chain_id: ChainId
    contract: str  # contract name for ABI/source resolution
    address: str  # mainnet deployed address (0x...)
    role: str = "source"  # source | destination | relayer | governance
    monitors: tuple[str, ...] = ()  # state variables or events to observe
    fork_block: int = 0
    abi_artifact: str = ""
    proxy: ProxyInfo | None = None


@dataclass(frozen=True)
class Binding:
    """Variable binding across chains."""

    source: str  # "ethereum.bridgeEth.totalLocked"
    destination: str  # "polygon.bridgePoly.totalMinted"
    relation: str = "equals"  # equals | sum | diff


# ── Correlation ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventSelector:
    """Identifies a cross-chain event."""

    contract: str  # contract name matching a Context
    event_name: str  # event signature name


@dataclass(frozen=True)
class CorrelationExtractor:
    """How to pair source and destination events."""

    source: EventSelector
    destination: EventSelector
    key_field: str  # field shared by both events (e.g. "messageHash")


# ── Transition Predicate ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class TransitionPredicate:
    """Declares valid state transitions for a variable."""

    chain_id: ChainId
    contract: str
    state_var: str  # e.g. "locked"
    on_increase: tuple[str, ...] = ()  # functions that may increase
    on_decrease: tuple[str, ...] = ()  # functions that may decrease
    guard: str = ""  # additional condition


# ── Quantified Predicate ─────────────────────────────────────────────────────


class QuantifierKind(StrEnum):
    FORALL = "forall"
    EXISTS = "exists"
    FORALL_EXISTS = "forall_exists"


@dataclass(frozen=True)
class ObservationSet:
    """Bounds iteration for mainnet invariants (prevents OOG)."""

    touched_message_ids: tuple[str, ...] = ()
    relay_dataset_ids: tuple[str, ...] = ()
    sampled_historical_ids: tuple[str, ...] = ()
    probe_generated_ids: tuple[str, ...] = ()
    max_items: int = 1000


@dataclass(frozen=True)
class QuantifiedPredicate:
    """The invariant property with explicit quantification."""

    kind: QuantifierKind = QuantifierKind.FORALL
    bound_variables: tuple[str, ...] = ()  # from bindings
    predicate: str = ""  # e.g. "locked == minted"


# ── Cross-Chain Invariant ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CrossChainInvariant:
    """Complete cross-chain invariant specification."""

    id: str  # stable identifier
    contexts: dict[ChainId, tuple[Context, ...]] = field(default_factory=dict)
    correlation_key: str = ""
    correlation_extractor: CorrelationExtractor | None = None
    bindings: tuple[Binding, ...] = ()
    observation_policy: ObservationPolicy = field(default_factory=ObservationPolicy)
    assumptions: tuple[Assumption, ...] = ()
    transition_predicates: tuple[TransitionPredicate, ...] = ()
    property: QuantifiedPredicate = field(default_factory=QuantifiedPredicate)
    observation_set: ObservationSet | None = None
    severity: str = "HIGH"
    tools: tuple[str, ...] = ()  # echidna, halmos, slither — if empty = all
    timeout: int = 600


# ── Skeleton Parser ──────────────────────────────────────────────────────────


def load_invariant(path: str) -> CrossChainInvariant:
    """Parse a .t.sol file and extract cross-chain invariant IR.

    Currently a skeleton — returns a placeholder invariant. The full
    parser will:
    1. Compile the Solidity file (forge build or solc directly)
    2. Read the AST / compiled artifact
    3. Extract NatSpec tags from invariant_ functions
    4. Build CrossChainInvariant from the extracted metadata

    Args:
        path: Path to a .t.sol invariant test file.

    Returns:
        A populated CrossChainInvariant.
    """
    # TODO: Implement full .t.sol parser
    return CrossChainInvariant(
        id=path,
    )


def validate_invariant(invariant: CrossChainInvariant) -> list[str]:
    """Validate that an invariant IR is complete.

    Returns a list of error messages. Empty list = valid.
    Required fields for cross-chain invariants:
    - at least 2 contexts (source + destination)
    - correlation_key or correlation_extractor
    - observation_policy
    - at least one transition_predicate per context
    - quantified_predicate with non-empty predicate

    Args:
        invariant: The invariant IR to validate.

    Returns:
        List of validation error messages.
    """
    errors: list[str] = []

    if len(invariant.contexts) < 2:
        errors.append("Cross-chain invariant requires at least 2 contexts")

    if not invariant.correlation_key and invariant.correlation_extractor is None:
        errors.append("Missing correlation_key or correlation_extractor")

    if not invariant.property.predicate:
        errors.append("Missing quantified predicate expression")

    # Validate NatSpec predicate matches Solidity assert
    # TODO: Compare with compiled artifact AST

    return errors
