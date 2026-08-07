"""Authoritative cross-chain invariant IR and strict NatSpec loader."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from devil.core.types import ChainId, frozen_mapping
from devil.invariant.expression import Expression, PredicateError, parse_expression


class StateReferenceKind(StrEnum):
    GETTER = "getter"
    STORAGE_PATH = "storage_path"


class BindingReduce(StrEnum):
    IDENTITY = "identity"
    SUM = "sum"
    DIFF = "diff"
    CUSTOM = "custom"


class TransitionEffect(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    SET = "set"
    RESET = "reset"
    DELETE = "delete"
    MAPPING_WRITE = "mapping_write"
    CUSTOM = "custom"


class ObservationKind(StrEnum):
    PER_TRANSACTION = "per_transaction"
    AFTER_FINALITY = "after_finality"
    AFTER_ALL_DELIVERED = "after_all_delivered"
    BLOCK_BOUNDED = "block_bounded"


class DeadlineUnit(StrEnum):
    BLOCKS = "blocks"
    SECONDS = "seconds"


class QuiescenceKind(StrEnum):
    NO_PENDING_MESSAGES = "no_pending_messages"
    NO_ELIGIBLE_MESSAGES = "no_eligible_messages"
    BOUNDED_BY_BLOCK = "bounded_by_block"


class AssumptionKind(StrEnum):
    SIGNER_HONESTY = "signer_honesty"
    MESSAGE_ORDERING = "message_ordering"
    FINALITY_MODEL = "finality_model"
    LIVENESS = "liveness"
    PROTOCOL_SPECIFIC = "protocol_specific"


class QuantifierKind(StrEnum):
    FORALL = "forall"
    EXISTS = "exists"
    FORALL_EXISTS = "forall_exists"


class PropertyKind(StrEnum):
    SAFETY = "safety"
    EVENTUALLY = "eventually"


@dataclass(frozen=True)
class TransformRef:
    function: str
    version: str


@dataclass(frozen=True)
class FunctionSelector:
    context_id: str
    function_signature: str


@dataclass(frozen=True)
class EventSelector:
    context_id: str
    event_signature: str


@dataclass(frozen=True)
class StateReference:
    context_id: str
    kind: StateReferenceKind
    getter: FunctionSelector | None = None
    storage_path: str | None = None
    result_path: str | None = None
    arguments: tuple[str, ...] = ()
    value_type: str = "uint256"

    def __post_init__(self) -> None:
        if (self.getter is None) == (self.storage_path is None):
            raise ValueError("state reference requires exactly one of getter or storage_path")
        if self.getter is not None and self.getter.context_id != self.context_id:
            raise ValueError("state reference getter context does not match context_id")


Monitor = StateReference | EventSelector


@dataclass(frozen=True)
class Context:
    context_id: str
    chain_id: ChainId
    role: str = "source"
    monitors: tuple[Monitor, ...] = ()
    snapshot_ref: str = ""


@dataclass(frozen=True)
class CorrelationExtractor:
    source: EventSelector
    destination: EventSelector
    source_fields: tuple[str, ...]
    destination_fields: tuple[str, ...]
    normalize: TransformRef


@dataclass(frozen=True)
class Binding:
    id: str
    sources: tuple[StateReference, ...]
    reduce: BindingReduce = BindingReduce.IDENTITY
    transform: TransformRef | None = None


@dataclass(frozen=True)
class TransitionRule:
    id: str
    function: FunctionSelector
    effect: TransitionEffect
    guard: Expression | None = None
    affected_bindings: tuple[str, ...] = ()
    custom_effect: TransformRef | None = None


@dataclass(frozen=True)
class TransitionPredicate:
    context_id: str
    binding_id: str
    rules: tuple[TransitionRule, ...]


@dataclass(frozen=True)
class Deadline:
    value: int
    unit: DeadlineUnit
    chain_id: ChainId | None = None

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError("deadline value must be positive")


@dataclass(frozen=True)
class QuiescenceRule:
    kind: QuiescenceKind
    max_pending_age: Deadline | None = None
    exclude_expired: bool = True
    exclude_rejected: bool = True


@dataclass(frozen=True)
class ObservationPolicy:
    kind: ObservationKind
    deadline: Deadline | None = None
    finality_blocks: int | None = None
    quiescence: QuiescenceRule | None = None


@dataclass(frozen=True)
class Assumption:
    kind: AssumptionKind
    value: str


@dataclass(frozen=True)
class ObservationSet:
    touched_message_ids: tuple[str, ...] = ()
    relay_dataset_ids: tuple[str, ...] = ()
    sampled_historical_ids: tuple[str, ...] = ()
    probe_generated_ids: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    max_items: int = 1000


@dataclass(frozen=True)
class QuantifiedPredicate:
    kind: QuantifierKind
    bound_variables: tuple[str, ...]
    predicate: Expression


@dataclass(frozen=True)
class Property:
    kind: PropertyKind
    predicate: QuantifiedPredicate
    trigger: Expression | None = None
    deadline: Deadline | None = None


@dataclass(frozen=True)
class CrossChainInvariant:
    id: str
    contexts: Mapping[str, Context]
    entry_context: str
    correlation_extractor_id: str
    bindings: tuple[Binding, ...]
    observation_policy: ObservationPolicy
    observation_set: ObservationSet
    transition_predicates: tuple[TransitionPredicate, ...]
    property: Property
    correlation_extractor: CorrelationExtractor | None = None
    assumptions: tuple[Assumption, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
    severity: str = "HIGH"
    timeout_seconds: int = 600

    def __post_init__(self) -> None:
        object.__setattr__(self, "contexts", frozen_mapping(self.contexts))

    @property
    def tools(self) -> tuple[str, ...]:
        return self.tool_allowlist

    @property
    def timeout(self) -> int:
        return self.timeout_seconds

    @property
    def correlation_key(self) -> str:
        return self.correlation_extractor_id


def load_invariant(
    path: str | Path,
    *,
    correlations: Mapping[str, CorrelationExtractor] | None = None,
    default_tools: tuple[str, ...] = (),
    default_severity: str = "HIGH",
    default_timeout: int = 600,
) -> CrossChainInvariant:
    """Parse and validate one fully declared ``invariant_`` NatSpec function."""
    source_path = Path(path)
    source = source_path.read_text(encoding="utf-8")
    matches = list(re.finditer(r"\bfunction\s+(invariant_[A-Za-z0-9_]+)\s*\(", source))
    if len(matches) != 1:
        raise ValueError(f"{path}: expected exactly one invariant_ function, found {len(matches)}")
    function_match = matches[0]
    function_id = function_match.group(1)
    metadata = _metadata_before(source, function_match.start())
    if not metadata:
        raise ValueError(f"{path}: invariant function has no NatSpec metadata")

    crosschain = _tag_value(metadata, "crosschain", required=True)
    context_ids = tuple(_option_list(crosschain, "contexts"))
    entry_context = _option(crosschain, "entry")
    contexts: dict[str, Context] = {}
    for index, context_id in enumerate(context_ids):
        chain_name, separator, _ = context_id.partition(".")
        if not separator:
            raise ValueError(f"{path}: context {context_id!r} must be chain.contract")
        contexts[context_id] = Context(
            context_id=context_id,
            chain_id=ChainId(chain_name),
            role="source" if index == 0 else "destination",
            snapshot_ref=chain_name,
        )

    bindings = _parse_bindings(_tags(metadata, "bind"), path)
    value_types = {binding.id: _binding_value_type(binding) for binding in bindings}
    transitions = tuple(
        _parse_transition(value, path, value_types) for value in _tags(metadata, "transition")
    )
    observation = _parse_observation(_tag_value(metadata, "observation", required=True))
    property_spec = _parse_property(metadata, value_types)
    assumptions = tuple(_parse_assumption(value, path) for value in _tags(metadata, "assume"))
    correlation_id = _tag_value(metadata, "correlation", required=True).strip()
    observation_set = _parse_observation_set(_tag_value(metadata, "observe", required=True))
    tools_tag = _tag_value(metadata, "tools", default="").strip()
    tools = tuple(_csv(tools_tag)) if tools_tag else default_tools
    severity = _tag_value(metadata, "severity", default=default_severity).strip().upper()
    timeout_text = _tag_value(metadata, "timeout", default=str(default_timeout)).strip()
    try:
        timeout = int(timeout_text)
    except ValueError as exc:
        raise ValueError(f"{path}: @timeout must be an integer") from exc
    extractor = (correlations or {}).get(correlation_id)

    invariant = CrossChainInvariant(
        id=function_id,
        contexts=contexts,
        entry_context=entry_context,
        correlation_extractor_id=correlation_id,
        correlation_extractor=extractor,
        bindings=bindings,
        observation_policy=observation,
        observation_set=observation_set,
        assumptions=assumptions,
        transition_predicates=transitions,
        property=property_spec,
        tool_allowlist=tools,
        severity=severity,
        timeout_seconds=timeout,
    )
    errors = validate_invariant(invariant, require_resolved_correlation=correlations is not None)
    if errors:
        raise ValueError(f"{path}: invalid invariant: {'; '.join(errors)}")
    _validate_solidity_assert(source, function_match.end(), invariant, path)
    return invariant


def validate_invariant(
    invariant: CrossChainInvariant, *, require_resolved_correlation: bool = False
) -> list[str]:
    errors: list[str] = []
    context_ids = set(invariant.contexts)
    if len(context_ids) < 2:
        errors.append("cross-chain invariant requires at least 2 contexts")
    if invariant.entry_context not in context_ids:
        errors.append("entry_context must reference a declared context")
    if not invariant.correlation_extractor_id:
        errors.append("correlation_extractor_id is required")
    if require_resolved_correlation and invariant.correlation_extractor is None:
        errors.append(f"unknown correlation extractor {invariant.correlation_extractor_id!r}")
    if invariant.correlation_extractor is not None:
        extractor_contexts = {
            invariant.correlation_extractor.source.context_id,
            invariant.correlation_extractor.destination.context_id,
        }
        if not extractor_contexts <= context_ids:
            errors.append("correlation extractor references an undeclared context")
        if len(invariant.correlation_extractor.source_fields) != len(
            invariant.correlation_extractor.destination_fields
        ):
            errors.append("correlation source/destination field arity differs")
    binding_ids = [binding.id for binding in invariant.bindings]
    if not binding_ids:
        errors.append("at least one binding is required")
    if len(binding_ids) != len(set(binding_ids)):
        errors.append("binding IDs must be unique")
    for binding in invariant.bindings:
        if not binding.sources:
            errors.append(f"binding {binding.id!r} has no source")
        if any(source.context_id not in context_ids for source in binding.sources):
            errors.append(f"binding {binding.id!r} references an undeclared context")
        if len(binding.sources) > 1 and binding.reduce is BindingReduce.IDENTITY:
            errors.append(f"binding {binding.id!r} needs an explicit reducer")
        if binding.reduce is BindingReduce.CUSTOM and binding.transform is None:
            errors.append(f"binding {binding.id!r} custom reducer needs a transform")
    transition_bindings = {item.binding_id for item in invariant.transition_predicates}
    missing_bindings = set(binding_ids) - transition_bindings
    if missing_bindings:
        errors.append("missing transition predicate for: " + ", ".join(sorted(missing_bindings)))
    rule_ids: set[str] = set()
    for predicate in invariant.transition_predicates:
        if predicate.context_id not in context_ids:
            errors.append(f"transition references undeclared context {predicate.context_id!r}")
        if predicate.binding_id not in binding_ids:
            errors.append(f"transition references unknown binding {predicate.binding_id!r}")
        if not predicate.rules:
            errors.append(f"transition for {predicate.binding_id!r} has no rules")
        for rule in predicate.rules:
            if rule.id in rule_ids:
                errors.append(f"duplicate transition rule ID {rule.id!r}")
            rule_ids.add(rule.id)
            if rule.function.context_id != predicate.context_id:
                errors.append(f"transition rule {rule.id!r} has mismatched context")
            if rule.effect is TransitionEffect.CUSTOM and rule.custom_effect is None:
                errors.append(f"transition rule {rule.id!r} needs custom_effect")
    policy = invariant.observation_policy
    if policy.kind is ObservationKind.BLOCK_BOUNDED and policy.deadline is None:
        errors.append("block-bounded observation requires a deadline")
    if policy.kind is ObservationKind.AFTER_FINALITY and (policy.finality_blocks or 0) <= 0:
        errors.append("after-finality observation requires finality_blocks")
    if policy.kind is ObservationKind.AFTER_ALL_DELIVERED and policy.quiescence is None:
        errors.append("after-all-delivered observation requires quiescence")
    if invariant.property.kind is PropertyKind.EVENTUALLY:
        if invariant.property.trigger is None or invariant.property.deadline is None:
            errors.append("eventually property requires trigger and deadline")
    if invariant.observation_set.max_items <= 0 or not invariant.observation_set.sources:
        errors.append("observation set must declare sources and positive max_items")
    if invariant.severity not in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
        errors.append("severity must be CRITICAL, HIGH, MEDIUM, or LOW")
    if invariant.timeout_seconds <= 0:
        errors.append("timeout_seconds must be positive")
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
        elif not stripped and not collected:
            continue
        else:
            break
    return "\n".join(reversed(collected))


def _clean_metadata(body: str) -> str:
    return "\n".join(line.strip().lstrip("*").strip() for line in body.splitlines())


def _tags(metadata: str, name: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(rf"^@{re.escape(name)}\s*(.*)$", metadata, re.MULTILINE)
    ]


def _tag_value(metadata: str, name: str, default: str = "", required: bool = False) -> str:
    values = _tags(metadata, name)
    if values:
        if len(values) > 1 and name not in {"transition", "bind", "assume"}:
            raise ValueError(f"duplicate @{name} metadata")
        return values[0]
    if required:
        raise ValueError(f"missing @{name} metadata")
    return default


def _option(value: str, name: str, default: str = "") -> str:
    pattern = rf"(?:^|\s){re.escape(name)}=(\[[^\]]*\]|\"[^\"]*\"|'[^']*'|[^\s]+)"
    match = re.search(pattern, value)
    return _unquote(match.group(1).strip().strip(",")) if match else default


def _option_list(value: str, name: str) -> list[str]:
    return _csv(_option(value, name).strip("[]"))


def _csv(value: str) -> list[str]:
    return [item.strip().strip("\"'") for item in value.split(",") if item.strip()]


def _list_literal(value: str, name: str) -> tuple[str, ...]:
    return tuple(_option_list(value, name))


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_bindings(values: list[str], path: str | Path) -> tuple[Binding, ...]:
    result: list[Binding] = []
    for value in values:
        matches = list(re.finditer(r"(?:^|\s)([A-Za-z_]\w*)=", value))
        if not matches:
            raise ValueError(f"{path}: invalid @bind {value!r}")
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            binding_id = match.group(1)
            specification = value[match.end() : end].strip()
            result.append(_parse_binding(binding_id, specification, path))
    return tuple(result)


def _parse_binding(binding_id: str, value: str, path: str | Path) -> Binding:
    reducer = BindingReduce.IDENTITY
    transform = None
    source_texts = (value,)
    reducer_match = re.fullmatch(r"(?P<reduce>sum|diff|custom):?(?P<name>[\w.@-]+)?\((.*)\)", value)
    if reducer_match:
        reducer = BindingReduce(reducer_match.group("reduce"))
        body = value[value.find("(") + 1 : -1]
        source_texts = tuple(_split_top_level(body))
        if reducer is BindingReduce.CUSTOM:
            transform = _parse_transform(reducer_match.group("name") or "")
    try:
        sources = tuple(_parse_state_reference(item.strip()) for item in source_texts)
    except ValueError as exc:
        raise ValueError(f"{path}: invalid binding {binding_id!r}: {exc}") from exc
    return Binding(binding_id, sources, reducer, transform)


def _parse_state_reference(value: str) -> StateReference:
    storage = re.fullmatch(r"(?P<context>[\w-]+\.[\w.-]+)\.storage:(?P<path>.+)", value)
    if storage:
        return StateReference(
            context_id=storage.group("context"),
            kind=StateReferenceKind.STORAGE_PATH,
            storage_path=storage.group("path"),
        )
    getter = re.fullmatch(
        r"(?P<context>[\w-]+\.[\w.-]+)\.(?P<getter>[A-Za-z_]\w*\([^)]*\))"
        r"(?:\[(?P<args>[^]]*)\])?(?:\.(?P<result>[A-Za-z_]\w*|\d+))?",
        value,
    )
    if getter is None:
        raise ValueError(f"invalid state reference {value!r}")
    context_id = getter.group("context")
    return StateReference(
        context_id=context_id,
        kind=StateReferenceKind.GETTER,
        getter=FunctionSelector(context_id, getter.group("getter")),
        arguments=tuple(_csv(getter.group("args") or "")),
        result_path=getter.group("result"),
    )


def _parse_transition(
    value: str, path: str | Path, value_types: Mapping[str, str]
) -> TransitionPredicate:
    match = re.match(r"(?P<context>[\w.-]+):(?P<binding>[\w.-]+)(?P<rest>.*)", value)
    if not match:
        raise ValueError(f"{path}: invalid @transition {value!r}")
    context_id = match.group("context")
    binding_id = match.group("binding")
    rest = match.group("rest")
    rules: list[TransitionRule] = []
    for effect, option_name in (
        (TransitionEffect.INCREASE, "increase"),
        (TransitionEffect.DECREASE, "decrease"),
    ):
        for index, signature in enumerate(_list_literal(rest, option_name)):
            rules.append(
                TransitionRule(
                    id=f"{binding_id}.{effect.value}.{index}",
                    function=FunctionSelector(context_id, signature),
                    effect=effect,
                    affected_bindings=(binding_id,),
                )
            )
    explicit = _option(rest, "effect")
    if explicit:
        effect = TransitionEffect(explicit.lower())
        signatures = _list_literal(rest, "functions")
        guard_text = _option(rest, "guard")
        guard = parse_expression(guard_text, value_types) if guard_text else None
        custom_effect = (
            _parse_transform(_option(rest, "custom")) if effect is TransitionEffect.CUSTOM else None
        )
        for index, signature in enumerate(signatures):
            rules.append(
                TransitionRule(
                    id=f"{binding_id}.{effect.value}.{index}",
                    function=FunctionSelector(context_id, signature),
                    effect=effect,
                    guard=guard,
                    affected_bindings=tuple(_option_list(rest, "affected") or (binding_id,)),
                    custom_effect=custom_effect,
                )
            )
    return TransitionPredicate(context_id, binding_id, tuple(rules))


def _parse_observation(value: str) -> ObservationPolicy:
    kind_name, _, options = value.partition(" ")
    try:
        kind = ObservationKind(kind_name.lower())
    except ValueError as exc:
        raise ValueError(f"invalid observation kind {kind_name!r}") from exc
    if kind is ObservationKind.PER_TRANSACTION:
        return ObservationPolicy(kind)
    if kind is ObservationKind.AFTER_FINALITY:
        blocks = int(_option(options, "blocks", _option(options, "finality_blocks", "0")))
        return ObservationPolicy(kind, finality_blocks=blocks)
    if kind is ObservationKind.BLOCK_BOUNDED:
        return ObservationPolicy(kind, deadline=_parse_deadline(_option(options, "deadline")))
    quiescence_text = _option(options, "quiescence")
    if not quiescence_text:
        raise ValueError("AFTER_ALL_DELIVERED requires quiescence")
    excludes = set(_csv(_option(options, "exclude")))
    age = _option(options, "max_pending_age")
    return ObservationPolicy(
        kind,
        quiescence=QuiescenceRule(
            kind=QuiescenceKind(quiescence_text.lower()),
            max_pending_age=_parse_deadline(age) if age else None,
            exclude_expired="expired" in excludes,
            exclude_rejected="rejected" in excludes,
        ),
    )


def _parse_property(metadata: str, value_types: Mapping[str, str]) -> Property:
    quantify = _tags(metadata, "quantify")
    eventually = _tags(metadata, "eventually")
    if len(quantify) + len(eventually) != 1:
        raise ValueError("exactly one of @quantify or @eventually is required")
    if quantify:
        return Property(PropertyKind.SAFETY, _parse_quantified(quantify[0], value_types))
    specification = eventually[0]
    trigger_text = _option(specification, "trigger")
    predicate_text = _option(specification, "predicate")
    deadline_text = _option(specification, "deadline")
    if not trigger_text or not predicate_text or not deadline_text:
        raise ValueError("@eventually requires trigger, predicate, and deadline")
    bracket_variables = tuple(
        dict.fromkeys(re.findall(r"\[\s*([A-Za-z_]\w*)\s*\]", trigger_text + " " + predicate_text))
    )
    variables = bracket_variables or tuple(
        name for name in value_types if re.search(rf"\b{re.escape(name)}\b", predicate_text)
    )
    expression_types = dict(value_types)
    for variable in variables:
        expression_types.setdefault(variable, "bytes32")
    quantified = QuantifiedPredicate(
        QuantifierKind.FORALL,
        variables,
        parse_expression(predicate_text, expression_types),
    )
    return Property(
        PropertyKind.EVENTUALLY,
        quantified,
        trigger=parse_expression(trigger_text, expression_types),
        deadline=_parse_deadline(deadline_text),
    )


def _parse_quantified(value: str, value_types: Mapping[str, str]) -> QuantifiedPredicate:
    match = re.match(
        r"(?P<kind>FORALL|EXISTS|FORALL_EXISTS)\s+(?P<vars>[^:]+):\s*(?P<predicate>.+)",
        value,
    )
    if not match:
        raise ValueError(f"invalid @quantify {value!r}")
    variables = tuple(item.strip() for item in match.group("vars").split(","))
    expression_types = dict(value_types)
    for variable in variables:
        expression_types.setdefault(variable, "bytes32")
    expression = parse_expression(match.group("predicate").strip(), expression_types)
    if expression.value_type != "bool":
        raise ValueError("quantified predicate must be boolean")
    return QuantifiedPredicate(QuantifierKind(match.group("kind").lower()), variables, expression)


def _parse_observation_set(value: str) -> ObservationSet:
    max_items = int(_option(value, "max", "0"))
    source_text = re.split(r"\s+max=", value, maxsplit=1)[0]
    sources = tuple(_csv(source_text))
    allowed = {"touched", "relay", "historical", "probe"}
    unknown = set(sources) - allowed
    if unknown:
        raise ValueError("unknown observation sources: " + ", ".join(sorted(unknown)))
    return ObservationSet(sources=sources, max_items=max_items)


def _parse_assumption(value: str, path: str | Path) -> Assumption:
    if ":" not in value:
        raise ValueError(f"{path}: invalid @assume {value!r}")
    name, expression = value.split(":", 1)
    try:
        kind = AssumptionKind(name.strip().lower())
    except ValueError:
        kind = AssumptionKind.PROTOCOL_SPECIFIC
        expression = f"{name.strip()}: {expression.strip()}"
    return Assumption(kind, expression.strip())


def _parse_deadline(value: str) -> Deadline:
    match = re.fullmatch(
        r"(?:(?P<chain>[\w-]+):)?(?P<value>\d+)(?P<unit>blocks?|s|seconds?)", value
    )
    if match is None:
        raise ValueError(f"invalid deadline {value!r}")
    unit = DeadlineUnit.BLOCKS if match.group("unit").startswith("block") else DeadlineUnit.SECONDS
    chain = ChainId(match.group("chain")) if match.group("chain") else None
    return Deadline(int(match.group("value")), unit, chain)


def _parse_transform(value: str) -> TransformRef:
    if not value:
        raise ValueError("transform reference cannot be empty")
    function, separator, version = value.partition("@")
    if not separator or not version:
        raise ValueError("transform reference must use function@version")
    return TransformRef(function, version)


def _binding_value_type(binding: Binding) -> str:
    source_types = {source.value_type for source in binding.sources}
    if len(source_types) != 1:
        raise ValueError(f"binding {binding.id!r} has incompatible source types")
    return next(iter(source_types))


def _split_top_level(value: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, character in enumerate(value):
        if character in "([":
            depth += 1
        elif character in ")]":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _validate_solidity_assert(
    source: str, function_header_end: int, invariant: CrossChainInvariant, path: str | Path
) -> None:
    body = _function_body(source, function_header_end)
    assertions = _balanced_calls(body, "assert")
    if len(assertions) != 1:
        raise ValueError(f"{path}: invariant function must contain exactly one assert(expression)")
    aliases = {
        match.group("name"): match.group("value").strip()
        for match in re.finditer(
            r"\b(?:u?int(?:\d+)?|bool|bytes\d*|address)\s+(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>[^;]+);",
            body,
        )
    }
    getter_to_binding: dict[str, str] = {}
    for binding in invariant.bindings:
        for source_ref in binding.sources:
            if source_ref.getter is not None:
                name = source_ref.getter.function_signature.partition("(")[0]
                if name in getter_to_binding and getter_to_binding[name] != binding.id:
                    raise ValueError(f"{path}: getter {name!r} is ambiguous across bindings")
                getter_to_binding[name] = binding.id
    assert_expression = assertions[0]
    for alias, value in aliases.items():
        getter = re.search(
            r"(?:\b[A-Za-z_]\w*\.)?(?P<name>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)",
            value,
        )
        if getter and getter.group("name") in getter_to_binding:
            replacement = getter_to_binding[getter.group("name")]
            arguments = getter.group("args").strip()
            if arguments:
                replacement += f"[{arguments}]"
            assert_expression = re.sub(rf"\b{re.escape(alias)}\b", replacement, assert_expression)
    for getter, binding_id in sorted(getter_to_binding.items(), key=lambda item: -len(item[0])):
        assert_expression = re.sub(
            rf"(?:\b[A-Za-z_]\w*\.)?{re.escape(getter)}\s*\((?P<args>[^)]*)\)",
            lambda match: (
                f"{binding_id}[{match.group('args').strip()}]"
                if match.group("args").strip()
                else binding_id
            ),
            assert_expression,
        )
    value_types = {binding.id: _binding_value_type(binding) for binding in invariant.bindings}
    for variable in invariant.property.predicate.bound_variables:
        value_types.setdefault(variable, "bytes32")
    declared = invariant.property.predicate.predicate
    for variable in invariant.property.predicate.bound_variables:
        if f"[{variable}]" not in declared.source:
            assert_expression = re.sub(rf"\[\s*{re.escape(variable)}\s*\]", "", assert_expression)
    try:
        rendered = parse_expression(assert_expression, value_types)
    except PredicateError as exc:
        raise ValueError(
            f"{path}: Solidity assert is not in the supported predicate subset: {exc}"
        ) from exc
    if rendered.canonical() != declared.canonical():
        raise ValueError(
            f"{path}: Solidity assert does not match declared predicate "
            f"({rendered.canonical()} != {declared.canonical()})"
        )


def _function_body(source: str, start: int) -> str:
    opening = source.find("{", start)
    if opening < 0:
        raise ValueError("invariant function has no body")
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise ValueError("invariant function body is unbalanced")


def _balanced_calls(source: str, name: str) -> list[str]:
    results: list[str] = []
    for match in re.finditer(rf"\b{re.escape(name)}\s*\(", source):
        depth = 1
        index = match.end()
        start = index
        while index < len(source) and depth:
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
            index += 1
        if depth:
            raise ValueError(f"unbalanced {name} call")
        results.append(source[start : index - 1].strip())
    return results
