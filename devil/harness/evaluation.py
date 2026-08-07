"""Baseline and bounded observation-set evaluation helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from devil.core.types import BaselineResult, BaselineStatus
from devil.invariant.expression import PredicateError, evaluate_predicate
from devil.invariant.ir import CrossChainInvariant


@dataclass
class ObservationSetEvaluator:
    """Bounded, deduplicated observation universe for one invariant run."""

    max_items: int = 1000
    touched_message_ids: set[str] = field(default_factory=set)
    relay_dataset_ids: set[str] = field(default_factory=set)
    sampled_historical_ids: set[str] = field(default_factory=set)
    probe_generated_ids: set[str] = field(default_factory=set)

    def add(self, category: str, identity: str) -> bool:
        if self.total >= self.max_items and identity not in self._category(category):
            return False
        self._category(category).add(identity)
        return True

    @property
    def total(self) -> int:
        return sum(len(values) for values in self._categories())

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
    """Evaluate an invariant at the pinned fork base before probing."""

    def __init__(self, read_values: Callable[[CrossChainInvariant], Mapping[str, Any]]) -> None:
        self._read_values = read_values

    def evaluate(self, invariant: CrossChainInvariant) -> BaselineResult:
        try:
            values = self._read_values(invariant)
            holds = evaluate_predicate(invariant.property.predicate, values)
        except (PredicateError, KeyError, ValueError, RuntimeError) as exc:
            return BaselineResult(BaselineStatus.INCONCLUSIVE, str(exc))
        if holds:
            return BaselineResult(BaselineStatus.HOLDS)
        return BaselineResult(BaselineStatus.VIOLATED, "predicate is false at the pinned fork base")


def evaluate_with_observations(
    invariant: CrossChainInvariant,
    values: Mapping[str, Any],
    observations: ObservationSetEvaluator,
) -> bool | None:
    """Return None when bounded observation collection is incomplete."""
    try:
        result = evaluate_predicate(invariant.property.predicate, values)
    except (PredicateError, KeyError, ValueError, RuntimeError):
        return None
    return result if not observations.exhausted else None
