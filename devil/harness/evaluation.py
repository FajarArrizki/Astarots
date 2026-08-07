"""Property, transition-monitor, baseline, and bounded-liveness evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from devil.core.types import (
    BaselineResult,
    BaselineStatus,
    Call,
    CrossChainStep,
    Evidence,
    GlobalState,
    LivenessObligation,
    LivenessStatus,
    Outcome,
)
from devil.invariant.expression import PredicateError, evaluate_predicate
from devil.invariant.ir import (
    CrossChainInvariant,
    DeadlineUnit,
    PropertyKind,
    QuantifierKind,
    TransitionEffect,
)


class EvaluationStatus(StrEnum):
    HOLDS = "holds"
    VIOLATED = "violated"
    PENDING = "pending"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class PropertyEvaluation:
    status: EvaluationStatus
    correlation_values: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class MonitorEvaluation:
    status: EvaluationStatus
    violated_rule_ids: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    reason: str = ""


@dataclass
class ObservationSetEvaluator:
    max_items: int = 1000
    touched_message_ids: set[str] = field(default_factory=set)
    relay_dataset_ids: set[str] = field(default_factory=set)
    sampled_historical_ids: set[str] = field(default_factory=set)
    probe_generated_ids: set[str] = field(default_factory=set)

    def add(self, category: str, identity: str) -> bool:
        known = set().union(*self._categories())
        if identity not in known and len(known) >= self.max_items:
            return False
        self._category(category).add(identity)
        return True

    @property
    def total(self) -> int:
        return len(set().union(*self._categories()))

    @property
    def exhausted(self) -> bool:
        return self.total >= self.max_items

    def _category(self, category: str) -> set[str]:
        try:
            return {
                "touched": self.touched_message_ids,
                "relay": self.relay_dataset_ids,
                "historical": self.sampled_historical_ids,
                "probe": self.probe_generated_ids,
            }[category]
        except KeyError as exc:
            raise ValueError(f"unknown observation category: {category}") from exc

    def _categories(self) -> tuple[set[str], ...]:
        return (
            self.touched_message_ids,
            self.relay_dataset_ids,
            self.sampled_historical_ids,
            self.probe_generated_ids,
        )


class BaselineEvaluator:
    def __init__(self, read_values: Callable[[CrossChainInvariant], Mapping[str, Any]]) -> None:
        self._read_values = read_values

    def evaluate(self, invariant: CrossChainInvariant) -> BaselineResult:
        try:
            values = self._read_values(invariant)
            holds = _evaluate_quantified(invariant, values)
            if invariant.property.kind is PropertyKind.EVENTUALLY:
                trigger = evaluate_predicate(invariant.property.trigger, values)
                if trigger and not holds:
                    return BaselineResult(BaselineStatus.PENDING, "liveness trigger active at base")
        except (PredicateError, KeyError, ValueError, RuntimeError) as exc:
            return BaselineResult(BaselineStatus.INCONCLUSIVE, str(exc))
        if holds:
            return BaselineResult(BaselineStatus.HOLDS)
        return BaselineResult(BaselineStatus.VIOLATED, "predicate is false at the pinned fork base")


def evaluate_property(
    invariant: CrossChainInvariant,
    state: GlobalState,
    observations: ObservationSetEvaluator | None = None,
) -> PropertyEvaluation:
    if observations is not None and observations.exhausted:
        return PropertyEvaluation(EvaluationStatus.INCONCLUSIVE, reason="observation set exhausted")
    try:
        if invariant.property.kind is PropertyKind.EVENTUALLY:
            obligations = tuple(state.liveness_obligations.values())
            if any(item.status is LivenessStatus.VIOLATED for item in obligations):
                return PropertyEvaluation(
                    EvaluationStatus.VIOLATED, reason="liveness deadline elapsed"
                )
            if any(item.status is LivenessStatus.INCONCLUSIVE for item in obligations):
                return PropertyEvaluation(
                    EvaluationStatus.INCONCLUSIVE, reason="liveness observation incomplete"
                )
            if any(item.status is LivenessStatus.ACTIVE for item in obligations):
                return PropertyEvaluation(EvaluationStatus.PENDING)
        holds = _evaluate_quantified(invariant, state.observed_values)
    except (PredicateError, KeyError, ValueError, RuntimeError) as exc:
        return PropertyEvaluation(EvaluationStatus.INCONCLUSIVE, reason=str(exc))
    return PropertyEvaluation(EvaluationStatus.HOLDS if holds else EvaluationStatus.VIOLATED)


def evaluate_transition_monitors(
    invariant: CrossChainInvariant,
    before: GlobalState,
    after: GlobalState,
    executed_step: CrossChainStep,
) -> MonitorEvaluation:
    if not isinstance(executed_step, Call):
        return MonitorEvaluation(EvaluationStatus.HOLDS)
    changed = {
        binding.id
        for binding in invariant.bindings
        if before.observed_values.get(binding.id) != after.observed_values.get(binding.id)
    }
    if not changed:
        return MonitorEvaluation(EvaluationStatus.HOLDS)
    violations: list[str] = []
    for binding_id in sorted(changed):
        predicates = [
            item
            for item in invariant.transition_predicates
            if item.binding_id == binding_id and item.context_id == executed_step.context_id
        ]
        matching = [
            rule
            for predicate in predicates
            for rule in predicate.rules
            if rule.function.function_signature == executed_step.function_signature
        ]
        if not matching:
            violations.append(f"unexpected_transition:{binding_id}")
            continue
        valid = any(_rule_holds(rule, binding_id, before, after) for rule in matching)
        if not valid:
            violations.extend(rule.id for rule in matching)
    if not violations:
        return MonitorEvaluation(EvaluationStatus.HOLDS)
    payload = json.dumps({"violated_rules": violations}, sort_keys=True)
    evidence = Evidence(
        "transition_monitor",
        Outcome.COUNTEREXAMPLE,
        payload,
        "sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
    )
    return MonitorEvaluation(EvaluationStatus.VIOLATED, tuple(violations), (evidence,))


def initialize_liveness_obligations(
    state: GlobalState, invariant: CrossChainInvariant
) -> GlobalState:
    if invariant.property.kind is not PropertyKind.EVENTUALLY:
        return state
    return update_liveness_obligations(state, invariant)


def update_liveness_obligations(state: GlobalState, invariant: CrossChainInvariant) -> GlobalState:
    if invariant.property.kind is not PropertyKind.EVENTUALLY:
        return state
    property_spec = invariant.property
    if property_spec.trigger is None or property_spec.deadline is None:
        raise ValueError("eventually property is missing trigger or deadline")
    obligations = dict(state.liveness_obligations)
    tuples = _quantifier_tuples(invariant, state.observed_values)
    if not tuples:
        tuples = [((), dict(state.observed_values))]
    for binding_key, values in tuples:
        identity = _obligation_id(invariant.id, binding_key)
        try:
            triggered = evaluate_predicate(property_spec.trigger, values)
        except (PredicateError, KeyError, ValueError):
            existing = obligations.get(identity)
            if existing is not None:
                obligations[identity] = replace(existing, status=LivenessStatus.INCONCLUSIVE)
            continue
        if triggered and identity not in obligations:
            chain = (
                property_spec.deadline.chain_id
                or invariant.contexts[invariant.entry_context].chain_id
            )
            snapshot = state.chain_snapshots[chain]
            obligations[identity] = LivenessObligation(
                identity,
                binding_key,
                _correlation_value(binding_key),
                chain,
                snapshot.block_number,
                snapshot.timestamp,
                property_spec.deadline.value,
                property_spec.deadline.unit.value,
            )
        obligation = obligations.get(identity)
        if obligation is None or obligation.status is not LivenessStatus.ACTIVE:
            continue
        try:
            holds = evaluate_predicate(property_spec.predicate.predicate, values)
        except (PredicateError, KeyError, ValueError):
            if _deadline_reached(state, obligation):
                obligations[identity] = replace(obligation, status=LivenessStatus.INCONCLUSIVE)
            continue
        if holds:
            obligations[identity] = replace(obligation, status=LivenessStatus.SATISFIED)
        elif _deadline_reached(state, obligation):
            obligations[identity] = replace(obligation, status=LivenessStatus.VIOLATED)
    return replace(state, liveness_obligations=obligations)


def evaluate_with_observations(
    invariant: CrossChainInvariant,
    values: Mapping[str, Any],
    observations: ObservationSetEvaluator,
) -> bool | None:
    if observations.exhausted:
        return None
    try:
        return _evaluate_quantified(invariant, values)
    except (PredicateError, KeyError, ValueError, RuntimeError):
        return None


def _evaluate_quantified(invariant: CrossChainInvariant, values: Mapping[str, Any]) -> bool:
    tuples = _quantifier_tuples(invariant, values)
    if not tuples:
        return evaluate_predicate(invariant.property.predicate.predicate, values)
    results = [
        evaluate_predicate(invariant.property.predicate.predicate, tuple_values)
        for _, tuple_values in tuples
    ]
    kind = invariant.property.predicate.kind
    if kind is QuantifierKind.FORALL:
        return all(results)
    if kind is QuantifierKind.EXISTS:
        return any(results)
    return all(results)


def _quantifier_tuples(
    invariant: CrossChainInvariant, values: Mapping[str, Any]
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    variables = invariant.property.predicate.bound_variables
    collections: list[tuple[str, tuple[Any, ...]]] = []
    for variable in variables:
        value = values.get(variable)
        if isinstance(value, Mapping):
            collections.append((variable, tuple(value)))
        elif isinstance(value, (tuple, list, set, frozenset)):
            collections.append((variable, tuple(value)))
    if not collections:
        mapped_values = [value for value in values.values() if isinstance(value, Mapping)]
        keys = (
            sorted(set().union(*(set(value) for value in mapped_values)), key=str)
            if mapped_values
            else []
        )
        return [((key,), _bind_key(values, key, variables)) for key in keys]
    result: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for variable, items in collections:
        for item in items:
            bound = _bind_key(values, item, variables)
            bound[variable] = item
            result.append(((item,), bound))
    return result


def _bind_key(values: Mapping[str, Any], key: Any, variables: tuple[str, ...]) -> dict[str, Any]:
    bound: dict[str, Any] = {}
    for name, value in values.items():
        if isinstance(value, Mapping):
            if key not in value:
                raise KeyError(f"observation key {key!r} is missing from binding {name!r}")
            bound[name] = value[key]
        else:
            bound[name] = value
    for variable in variables:
        bound.setdefault(variable, key)
    return bound


def _rule_holds(rule: Any, binding_id: str, before: GlobalState, after: GlobalState) -> bool:
    previous = before.observed_values.get(binding_id)
    current = after.observed_values.get(binding_id)
    if rule.guard is not None:
        try:
            if not evaluate_predicate(rule.guard, before.observed_values):
                return False
        except PredicateError:
            return False
    if rule.effect is TransitionEffect.INCREASE:
        return isinstance(previous, int) and isinstance(current, int) and current > previous
    if rule.effect is TransitionEffect.DECREASE:
        return isinstance(previous, int) and isinstance(current, int) and current < previous
    if rule.effect is TransitionEffect.RESET:
        return current == 0
    if rule.effect is TransitionEffect.DELETE:
        return current in {None, 0, False, "0x"}
    return previous != current


def _deadline_reached(state: GlobalState, obligation: LivenessObligation) -> bool:
    snapshot = state.chain_snapshots[obligation.clock_chain]
    if obligation.deadline_unit == DeadlineUnit.BLOCKS.value:
        return snapshot.block_number >= obligation.start_block + obligation.deadline_value
    return snapshot.timestamp >= obligation.start_timestamp + obligation.deadline_value


def _obligation_id(invariant_id: str, binding_key: tuple[Any, ...]) -> str:
    encoded = json.dumps([invariant_id, binding_key], sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _correlation_value(binding_key: tuple[Any, ...]) -> str | None:
    return str(binding_key[0]) if binding_key else None
