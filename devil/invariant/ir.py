"""Invariant IR — internal representation of cross-chain invariants.

Parsed from .t.sol NatSpec tags or authored directly. The IR is the
authoritative representation — the .t.sol parser populates it, and
the scheduler consumes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

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


# ── NatSpec Parser ────────────────────────────────────────────────────────────


def load_invariant(path: str) -> CrossChainInvariant:
    """Parse the declared cross-chain NatSpec metadata from one ``.t.sol`` file."""
    source_path = Path(path)
    source = source_path.read_text(encoding="utf-8")
    function_match = re.search(r"\bfunction\s+(invariant_[A-Za-z0-9_]+)\s*\(", source)
    if function_match is None:
        raise ValueError(f"{path}: no invariant_ function found")
    function_id = function_match.group(1)
    metadata = _metadata_before(source, function_match.start())
    if not metadata:
        raise ValueError(f"{path}: invariant function has no NatSpec metadata")

    contexts_raw = _tag_value(metadata, "crosschain", required=True)
    contexts: dict[ChainId, tuple[Context, ...]] = {}
    context_names = _option_list(contexts_raw, "contexts")
    for index, context_name in enumerate(context_names):
        chain_name, _, contract = context_name.partition(".")
        if not contract:
            raise ValueError(f"{path}: context {context_name!r} must be chain.contract")
        chain = ChainId(chain_name)
        role = "source" if index == 0 else "destination"
        contexts[chain] = contexts.get(chain, ()) + (
            Context(chain_id=chain, contract=contract, address="", role=role),
        )

    transitions = tuple(_parse_transition(value, path) for value in _tags(metadata, "transition"))
    bindings = tuple(_parse_binding(value, path) for value in _tags(metadata, "bind"))
    observation = _parse_observation(_tag_value(metadata, "observation", required=True))
    property_spec = _parse_quantified(_tag_value(metadata, "quantify", required=True))
    assumes = tuple(_parse_assumption(value, path) for value in _tags(metadata, "assume"))
    correlation = _tag_value(metadata, "correlation", required=True).strip()
    observe = _tag_value(metadata, "observe", default="")
    observation_set = ObservationSet(max_items=int(_option(observe, "max", default="1000")))
    tools = tuple(_option_list(_tag_value(metadata, "tools", default=""), "names"))
    severity = _option(_tag_value(metadata, "severity", default=""), "level", default="HIGH")
    timeout = int(_option(_tag_value(metadata, "timeout", default=""), "seconds", default="600"))
    invariant = CrossChainInvariant(
        id=function_id,
        contexts=contexts,
        correlation_key=correlation,
        bindings=bindings,
        observation_policy=observation,
        assumptions=assumes,
        transition_predicates=transitions,
        property=property_spec,
        observation_set=observation_set,
        severity=severity,
        tools=tools,
        timeout=timeout,
    )
    errors = validate_invariant(invariant)
    if errors:
        raise ValueError(f"{path}: invalid invariant: {'; '.join(errors)}")
    return invariant


def validate_invariant(invariant: CrossChainInvariant) -> list[str]:
    """Validate that an invariant IR is complete and internally consistent."""
    errors: list[str] = []
    context_names = {
        f"{context.chain_id.value}.{context.contract}"
        for contexts in invariant.contexts.values()
        for context in contexts
    }
    if len(context_names) < 2:
        errors.append("Cross-chain invariant requires at least 2 contexts")
    if not invariant.correlation_key and invariant.correlation_extractor is None:
        errors.append("Missing correlation_key or correlation_extractor")
    if not invariant.property.predicate:
        errors.append("Missing quantified predicate expression")
    if not invariant.transition_predicates:
        errors.append("Missing transition predicates")
    transition_contexts = {
        f"{item.chain_id.value}.{item.contract}" for item in invariant.transition_predicates
    }
    missing = context_names - transition_contexts
    if missing:
        errors.append("Missing transition predicate for: " + ", ".join(sorted(missing)))
    if invariant.observation_policy.kind is ObservationKind.BLOCK_BOUNDED:
        if (invariant.observation_policy.deadline or 0) <= 0:
            errors.append("block-bounded observation requires a positive deadline")
    if invariant.timeout <= 0:
        errors.append("timeout must be positive")
    return errors


def _metadata_before(source: str, end: int) -> str:
    prefix = source[:end]
    block = re.search(r"/\*\*(?P<body>.*?)\*/\s*$", prefix, re.DOTALL)
    if block:
        return _clean_metadata(block.group("body"))
    lines = prefix.splitlines()
    collected: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("///"):
            collected.append(stripped[3:].strip())
        elif not stripped:
            if collected:
                break
        else:
            break
    return "\n".join(reversed(collected))


def _clean_metadata(body: str) -> str:
    return "\n".join(line.strip().lstrip("*").strip() for line in body.splitlines())


def _tags(metadata: str, name: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(rf"^@{name}\s*(.*)$", metadata, re.MULTILINE)
    ]


def _tag_value(metadata: str, name: str, default: str | None = None, required: bool = False) -> str:
    values = _tags(metadata, name)
    if values:
        return values[0]
    if required:
        raise ValueError(f"missing @{name} metadata")
    return default or ""


def _option(value: str, name: str, default: str = "") -> str:
    match = re.search(rf"(?:^|\s){re.escape(name)}=([^\s]+)", value)
    return match.group(1).strip().strip(",") if match else default


def _option_list(value: str, name: str) -> list[str]:
    raw = _option(value, name)
    return [item.strip() for item in raw.strip("[]").split(",") if item.strip()]


def _list_literal(value: str, name: str) -> tuple[str, ...]:
    raw = _option(value, name)
    return tuple(item.strip().strip("\"'") for item in raw.strip("[]").split(",") if item.strip())


def _parse_transition(value: str, path: str) -> TransitionPredicate:
    match = re.match(r"(?P<context>[\w.-]+):(?P<state>[\w.-]+)(?P<rest>.*)", value)
    if not match:
        raise ValueError(f"{path}: invalid @transition {value!r}")
    chain_name, _, contract = match.group("context").partition(".")
    if not contract:
        raise ValueError(f"{path}: transition context must be chain.contract")
    return TransitionPredicate(
        chain_id=ChainId(chain_name),
        contract=contract,
        state_var=match.group("state"),
        on_increase=_list_literal(match.group("rest"), "increase"),
        on_decrease=_list_literal(match.group("rest"), "decrease"),
        guard=_option(match.group("rest"), "guard"),
    )


def _parse_binding(value: str, path: str) -> Binding:
    if "=" not in value:
        raise ValueError(f"{path}: invalid @bind {value!r}")
    left, right = value.split("=", 1)
    return Binding(source=left.strip(), destination=right.strip())


def _parse_observation(value: str) -> ObservationPolicy:
    kind_name, _, options = value.partition(" ")
    kind_map = {
        "PER_TRANSACTION": ObservationKind.PER_TRANSACTION,
        "AFTER_ALL_DELIVERED": ObservationKind.AFTER_ALL_DELIVERED,
        "BLOCK_BOUNDED": ObservationKind.BLOCK_BOUNDED,
    }
    try:
        kind = kind_map[kind_name.upper()]
    except KeyError as exc:
        raise ValueError(f"invalid observation kind {kind_name!r}") from exc
    deadline_raw = _option(options, "window", default=_option(options, "deadline", default="0"))
    return ObservationPolicy(
        kind=kind,
        deadline=int(deadline_raw) if deadline_raw else None,
        deadline_unit="blocks" if kind is ObservationKind.BLOCK_BOUNDED else None,
        quiescence=QuiescenceRule() if kind is ObservationKind.AFTER_ALL_DELIVERED else None,
    )


def _parse_quantified(value: str) -> QuantifiedPredicate:
    match = re.match(
        r"(?P<kind>FORALL|EXISTS|FORALL_EXISTS)\s+(?P<vars>[^:]+):\s*(?P<predicate>.+)", value
    )
    if not match:
        raise ValueError(f"invalid @quantify {value!r}")
    return QuantifiedPredicate(
        kind=QuantifierKind(match.group("kind").lower()),
        bound_variables=tuple(item.strip() for item in match.group("vars").split(",")),
        predicate=match.group("predicate").strip(),
    )


def _parse_assumption(value: str, path: str) -> Assumption:
    if ":" not in value:
        raise ValueError(f"{path}: invalid @assume {value!r}")
    name, expression = value.split(":", 1)
    return Assumption(kind=AssumptionKind(name.strip()), value=expression.strip())
