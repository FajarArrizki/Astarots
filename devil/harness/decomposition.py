"""Derive local transition monitors without inventing invariant semantics."""

from __future__ import annotations

from dataclasses import dataclass

from devil.core.types import ChainId
from devil.invariant.ir import (
    Binding,
    Context,
    CrossChainInvariant,
    Property,
    TransitionPredicate,
)


@dataclass(frozen=True)
class SubInvariant:
    id: str
    chain: ChainId
    contexts: tuple[Context, ...]
    transitions: tuple[TransitionPredicate, ...]
    bindings: tuple[Binding, ...]
    correlation_extractor_id: str
    property: Property


def decompose(invariant: CrossChainInvariant) -> tuple[SubInvariant, ...]:
    """Materialize one local monitor bundle per chain while preserving global property."""
    result: list[SubInvariant] = []
    chains = sorted({context.chain_id for context in invariant.contexts.values()}, key=str)
    for chain in chains:
        contexts = tuple(
            context for context in invariant.contexts.values() if context.chain_id == chain
        )
        context_ids = {context.context_id for context in contexts}
        transitions = tuple(
            transition
            for transition in invariant.transition_predicates
            if transition.context_id in context_ids
        )
        bindings = tuple(
            binding
            for binding in invariant.bindings
            if any(source.context_id in context_ids for source in binding.sources)
        )
        result.append(
            SubInvariant(
                id=f"{invariant.id}:{chain.value}",
                chain=chain,
                contexts=contexts,
                transitions=transitions,
                bindings=bindings,
                correlation_extractor_id=invariant.correlation_extractor_id,
                property=invariant.property,
            )
        )
    return tuple(result)
