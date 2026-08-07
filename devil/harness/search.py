"""Unified causal frontier over immutable cross-chain search states."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from devil.core.types import (
    Candidate,
    EdgeCase,
    Evidence,
    GlobalState,
    Outcome,
    SearchResult,
    SearchState,
    Verdict,
    ViolationSource,
    WitnessState,
)


@dataclass(frozen=True)
class SearchConfig:
    """Bounds for one deterministic campaign."""

    max_depth: int = 8
    max_states: int = 200
    max_consecutive_expansions_per_chain: int = 4
    branching_caps: tuple[int, ...] = (4, 4, 3, 3, 2, 2, 1, 1)

    def cap(self, depth: int) -> int:
        if depth < len(self.branching_caps):
            return self.branching_caps[depth]
        return self.branching_caps[-1] if self.branching_caps else 0


@dataclass(frozen=True)
class BranchResult:
    """Canonical executor result for one candidate prefix."""

    outcome: Outcome
    state: GlobalState | None = None
    correlation_value: str = ""
    evidence: tuple[Evidence, ...] = ()
    reason: str = ""


class UnifiedFrontier:
    """A deterministic priority queue that deduplicates branch fingerprints."""

    def __init__(self) -> None:
        self._heap: list[tuple[tuple[float, int, str], SearchState]] = []
        self._seen: set[str] = set()
        self._last_chain = None
        self._consecutive = 0

    def push(self, state: SearchState, suspicion: float = 0.0) -> bool:
        fingerprint = state_fingerprint(state)
        if fingerprint in self._seen:
            return False
        self._seen.add(fingerprint)
        priority = (-suspicion, -state.depth, trace_hash(state.sequence))
        heapq.heappush(self._heap, (priority, state))
        return True

    def pop(self) -> SearchState:
        return heapq.heappop(self._heap)[1]

    def pop_fair(self, max_consecutive: int = 4) -> SearchState:
        """Pop the best eligible chain, avoiding starvation by one chain."""
        if not self._heap:
            raise IndexError("pop from empty frontier")
        eligible = [
            index
            for index, (_, state) in enumerate(self._heap)
            if self._last_chain != state.chain_context or self._consecutive < max_consecutive
        ]
        index = min(eligible or range(len(self._heap)), key=lambda item: self._heap[item][0])
        _, state = self._heap.pop(index)
        heapq.heapify(self._heap)
        if state.chain_context == self._last_chain:
            self._consecutive += 1
        else:
            self._last_chain = state.chain_context
            self._consecutive = 1
        return state

    def __bool__(self) -> bool:
        return bool(self._heap)


class UnifiedSearch:
    """Explore source, relay, and destination actions in one causal lineage."""

    def __init__(
        self,
        *,
        config: SearchConfig,
        propose: Callable[[SearchState], Iterable[Candidate]],
        execute: Callable[[SearchState, Candidate], BranchResult],
        evaluate: Callable[[GlobalState], bool | None],
    ) -> None:
        self.config = config
        self.propose = propose
        self.execute = execute
        self.evaluate = evaluate

    def run(self, initial: SearchState, baseline: object | None = None) -> SearchResult:
        frontier = UnifiedFrontier()
        frontier.push(initial)
        witnesses: list[WitnessState] = []
        deepest: EdgeCase | None = None
        budget_used = 0
        had_inconclusive = False

        while frontier and budget_used < self.config.max_states:
            state = frontier.pop_fair(self.config.max_consecutive_expansions_per_chain)
            if state.depth >= self.config.max_depth:
                continue
            candidates = sorted(
                self.propose(state),
                key=lambda candidate: (-candidate.suspicion, candidate.target_function),
            )[: self.config.cap(state.depth)]
            for candidate in candidates:
                if budget_used >= self.config.max_states:
                    break
                budget_used += 1
                branch = self.execute(state, candidate)
                if branch.outcome in {Outcome.TIMEOUT, Outcome.TOOL_ERROR, Outcome.PARTIAL}:
                    had_inconclusive = True
                    continue
                if branch.outcome in {Outcome.UNSUPPORTED, Outcome.UNSAT_UNDER_BOUNDS}:
                    continue
                if branch.state is None:
                    had_inconclusive = True
                    continue
                next_sequence = state.sequence + candidate.call_sequence
                next_constraints = state.constraints + candidate.pre_conditions
                next_state = SearchState(
                    global_state=branch.state,
                    chain_context=candidate.chain or state.chain_context,
                    constraints=next_constraints,
                    sequence=next_sequence,
                    evidence=state.evidence + branch.evidence,
                    depth=state.depth + 1,
                    branch_id=_child_branch_id(state.branch_id, candidate, state.depth),
                )
                status = self.evaluate(branch.state)
                if status is None:
                    had_inconclusive = True
                elif status is False:
                    witness = WitnessState(
                        snapshot=branch.state,
                        correlation_value=branch.correlation_value,
                        chain=candidate.chain or state.chain_context,
                        branch_id=next_state.branch_id,
                        call_sequence=next_sequence,
                        constraints=next_constraints,
                        evidence=next_state.evidence,
                    )
                    witnesses.append(witness)
                    edge = EdgeCase(
                        depth=next_state.depth,
                        witnesses=(witness,),
                        evidence_strength=_evidence_strength(next_state.evidence),
                        violation_source=ViolationSource.INTRODUCED_BY_TRACE,
                        chains=tuple(
                            sorted(branch.state.chain_snapshots, key=lambda item: item.value)
                        ),
                        description=candidate.target_function,
                    )
                    if _edge_key(edge) < _edge_key(deepest) if deepest else True:
                        deepest = edge
                if next_state.depth < self.config.max_depth:
                    frontier.push(next_state, candidate.suspicion)

        outcome = (
            Verdict.VIOLATED
            if deepest
            else Verdict.INCONCLUSIVE
            if had_inconclusive
            else Verdict.NOT_OBSERVED
        )
        return SearchResult(
            witnesses=tuple(witnesses),
            deepest_edge=deepest,
            baseline=baseline if baseline is not None else SearchResult().baseline,
            exhausted=not frontier,
            outcome=outcome,
            budget_used=budget_used,
            budget_total=self.config.max_states,
        )


def state_fingerprint(state: SearchState) -> str:
    payload = {
        "chain_context": state.chain_context.value,
        "snapshots": {
            chain.value: {
                "block": snapshot.base_block,
                "overlay": snapshot.overlay_id,
                "diff": [
                    (item.contract, item.slot, item.new_value) for item in snapshot.state_diff
                ],
            }
            for chain, snapshot in sorted(
                state.global_state.chain_snapshots.items(), key=lambda item: item[0].value
            )
        },
        "pending": [str(message) for message in state.global_state.pending_messages],
        "sequence": [
            (call.chain.value if call.chain else "", call.function_signature, call.args)
            for call in state.sequence
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def trace_hash(sequence: tuple) -> str:
    payload = [
        (getattr(call.chain, "value", ""), call.function_signature, call.args) for call in sequence
    ]
    return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()


def _child_branch_id(parent: str, candidate: Candidate, depth: int) -> str:
    material = f"{parent}|{depth}|{candidate.target_function}|{trace_hash(candidate.call_sequence)}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _edge_key(edge: EdgeCase | None) -> tuple[int, str]:
    if edge is None:
        return (10**9, "")
    sequence = tuple(call for witness in edge.witnesses for call in witness.call_sequence)
    return (-edge.depth, trace_hash(sequence))


def _evidence_strength(evidence: tuple[Evidence, ...]):
    from devil.core.types import EvidenceStrength

    if any(item.outcome is Outcome.COUNTEREXAMPLE for item in evidence):
        return EvidenceStrength.OBSERVED
    return EvidenceStrength.OBSERVED
