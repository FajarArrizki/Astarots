"""Decompose and recombine cross-chain invariant evidence."""

from __future__ import annotations

from dataclasses import dataclass

from devil.core.types import ChainId, WitnessState
from devil.invariant.ir import Binding, Context, CrossChainInvariant, TransitionPredicate


@dataclass(frozen=True)
class SubInvariant:
    """Per-chain monitor retaining the global correlation contract."""

    id: str
    chain: ChainId
    contexts: tuple[Context, ...]
    transitions: tuple[TransitionPredicate, ...]
    bindings: tuple[Binding, ...]
    correlation_key: str


def decompose(invariant: CrossChainInvariant) -> tuple[SubInvariant, ...]:
    """Create one monitor per configured chain without losing cross-chain metadata."""
    result: list[SubInvariant] = []
    for chain, contexts in sorted(invariant.contexts.items(), key=lambda item: item[0].value):
        context_names = {context.contract for context in contexts}
        transitions = tuple(
            transition
            for transition in invariant.transition_predicates
            if transition.chain_id is chain and transition.contract in context_names
        )
        bindings = tuple(
            binding
            for binding in invariant.bindings
            if binding.source.startswith(f"{chain.value}.")
            or binding.destination.startswith(f"{chain.value}.")
        )
        result.append(
            SubInvariant(
                id=f"{invariant.id}:{chain.value}",
                chain=chain,
                contexts=contexts,
                transitions=transitions,
                bindings=bindings,
                correlation_key=invariant.correlation_key,
            )
        )
    return tuple(result)


def recombine(witnesses: tuple[WitnessState, ...]) -> tuple[WitnessState, ...]:
    """Keep only witness segments on one causal branch and correlation identity."""
    if not witnesses:
        return ()
    by_key: dict[tuple[str, str], list[WitnessState]] = {}
    for witness in witnesses:
        by_key.setdefault((witness.branch_id, witness.correlation_value), []).append(witness)
    return tuple(
        witness
        for key in sorted(by_key)
        if len({item.chain for item in by_key[key]}) > 1
        for witness in sorted(by_key[key], key=lambda item: (item.chain.value, item.call_sequence))
    )
