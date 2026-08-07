"""Deterministic best-first search over one causal cross-chain frontier."""

from __future__ import annotations

import hashlib
import heapq
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from devil.core.types import (
    BaselineResult,
    BaselineStatus,
    Candidate,
    ChainId,
    Constraint,
    EdgeCase,
    Evidence,
    EvidenceStrength,
    GlobalState,
    Impact,
    Outcome,
    SearchResult,
    SearchState,
    Verdict,
    ViolationSource,
    WitnessState,
)
from devil.harness.evaluation import (
    EvaluationStatus,
    ObservationSetEvaluator,
    evaluate_property,
    evaluate_transition_monitors,
    update_liveness_obligations,
)
from devil.harness.executor import (
    AppliedPrefix,
    CanonicalForkExecutor,
    PrefixOutcome,
    canonical_state_hash,
)
from devil.invariant.ir import CrossChainInvariant


@dataclass(frozen=True)
class SearchConfig:
    max_depth: int = 8
    max_states: int = 200
    branching_caps: tuple[int, ...] = (4, 4, 3, 3, 2, 2, 1, 1)
    max_consecutive_expansions_per_chain: int = 4

    def branching_cap(self, depth: int) -> int:
        if depth < len(self.branching_caps):
            return self.branching_caps[depth]
        return self.branching_caps[-1] if self.branching_caps else 0


@dataclass(frozen=True)
class BranchResult:
    """Legacy-free single-step callback result retained as a useful public value."""

    outcome: Outcome
    state: GlobalState | None = None
    correlation_value: str = ""
    evidence: tuple[Evidence, ...] = ()
    impact: Impact | None = None
    reason: str = ""


class UnifiedFrontier:
    """Max-suspicion queue with deterministic ordering and per-chain fairness."""

    def __init__(self, max_consecutive: int = 4) -> None:
        self._heap: list[tuple[tuple[Any, ...], int, SearchState]] = []
        self._counter = 0
        self._max_consecutive = max_consecutive
        self._last_chain: ChainId | None = None
        self._consecutive = 0

    def push(self, state: SearchState, suspicion: float) -> None:
        key = (-suspicion, state.depth, canonical_state_hash(state.global_state))
        heapq.heappush(self._heap, (key, self._counter, state))
        self._counter += 1

    def pop(self) -> SearchState:
        if not self._heap:
            raise IndexError("frontier is empty")
        selected = 0
        if (
            self._last_chain is not None
            and self._consecutive >= self._max_consecutive
            and any(item[2].chain_context is not self._last_chain for item in self._heap)
        ):
            selected = min(
                (
                    index
                    for index, item in enumerate(self._heap)
                    if item[2].chain_context is not self._last_chain
                ),
                key=lambda index: self._heap[index][:2],
            )
        _, _, state = self._heap[selected]
        if selected == len(self._heap) - 1:
            self._heap.pop()
        else:
            self._heap[selected] = self._heap.pop()
            heapq.heapify(self._heap)
        if state.chain_context is self._last_chain:
            self._consecutive += 1
        else:
            self._last_chain = state.chain_context
            self._consecutive = 1
        return state

    def __bool__(self) -> bool:
        return bool(self._heap)


class UnifiedSearch:
    """Replay every candidate step through the canonical executor and evaluate it."""

    def __init__(
        self,
        *,
        invariant: CrossChainInvariant,
        executor: CanonicalForkExecutor,
        propose: Callable[[SearchState], Any],
        config: SearchConfig | None = None,
        observations: ObservationSetEvaluator | None = None,
    ) -> None:
        self.invariant = invariant
        self.executor = executor
        self.propose = propose
        self.config = config or SearchConfig()
        self.observations = observations or ObservationSetEvaluator(
            invariant.observation_set.max_items
        )

    def run(self, initial: SearchState, *, baseline: BaselineResult) -> SearchResult:
        frontier = UnifiedFrontier(self.config.max_consecutive_expansions_per_chain)
        frontier.push(initial, 0.0)
        visited_depth = {canonical_state_hash(initial.global_state): 0}
        witnesses: list[WitnessState] = []
        edges: list[EdgeCase] = []
        finding_keys: set[tuple[Any, ...]] = set()
        deepest: EdgeCase | None = None
        budget_used = 0
        incomplete: list[Outcome] = []

        if baseline.status is BaselineStatus.VIOLATED:
            baseline_witness = WitnessState(
                snapshot=initial.global_state,
                chain=initial.chain_context,
                branch_id=initial.branch_id,
                call_sequence=(),
            )
            baseline_edge = EdgeCase(
                depth=0,
                witness=baseline_witness,
                segment_strengths={"baseline": EvidenceStrength.REPLAYED},
                aggregate_strength=EvidenceStrength.REPLAYED,
                violated_clauses=("global_property",),
                violation_source=ViolationSource.PRE_EXISTING_AT_SNAPSHOT,
                impact=Impact(
                    self.invariant.severity,
                    affected_chains=tuple(sorted(initial.global_state.chain_snapshots, key=str)),
                ),
                chains=tuple(sorted(initial.global_state.chain_snapshots, key=str)),
            )
            edges.append(baseline_edge)
            finding_keys.add(_edge_key(self.invariant.id, baseline_edge))
            deepest = baseline_edge
        elif baseline.status in {
            BaselineStatus.PENDING,
            BaselineStatus.UNOBSERVABLE,
            BaselineStatus.INCONCLUSIVE,
        }:
            incomplete.append(Outcome.PARTIAL)

        while frontier and budget_used < self.config.max_states:
            state = frontier.pop()
            if state.depth >= self.config.max_depth:
                continue
            try:
                self.executor.restore(state.global_state)
            except RuntimeError:
                incomplete.append(Outcome.TOOL_ERROR)
                continue
            proposal = self.propose(state)
            if hasattr(proposal, "outcome") and hasattr(proposal, "value"):
                if proposal.outcome in {
                    Outcome.TIMEOUT,
                    Outcome.TOOL_ERROR,
                    Outcome.UNSUPPORTED,
                    Outcome.PARTIAL,
                }:
                    incomplete.append(proposal.outcome)
                candidates = list(proposal.value or ())
            else:
                candidates = list(proposal)
            candidates.extend(
                self.executor.coordinator.propose_transitions(
                    state.global_state, state.chain_context
                )
            )
            ranked = sorted(
                candidates,
                key=lambda candidate: (-candidate.suspicion, trace_hash(candidate.call_sequence)),
            )[: self.config.branching_cap(state.depth)]
            for candidate in ranked:
                if budget_used >= self.config.max_states:
                    break
                if not candidate.call_sequence:
                    continue
                if not _constraints_consistent(state.constraints, candidate.pre_conditions):
                    continue
                execution = self.executor.execute_candidate_prefixes(
                    state.global_state,
                    candidate,
                    max_steps=self.config.max_depth - state.depth,
                    budget=self.config.max_states - budget_used,
                    parent_branch_id=state.branch_id,
                )
                budget_used += execution.steps_attempted
                terminal_outcome = {
                    PrefixOutcome.TIMEOUT: Outcome.TIMEOUT,
                    PrefixOutcome.TOOL_ERROR: Outcome.TOOL_ERROR,
                    PrefixOutcome.PARTIAL: Outcome.PARTIAL,
                }.get(execution.terminal_outcome)
                if terminal_outcome is not None:
                    incomplete.append(terminal_outcome)
                for expansion in execution.applied_prefixes:
                    candidate_depth = state.depth + len(expansion.prefix)
                    after = update_liveness_obligations(expansion.after_state, self.invariant)
                    witness = _witness(state, candidate, expansion, after)
                    witnesses.append(witness)
                    monitor = evaluate_transition_monitors(
                        self.invariant,
                        expansion.before_state,
                        after,
                        expansion.executed_step,
                    )
                    property_result = evaluate_property(self.invariant, after, self.observations)
                    if EvaluationStatus.INCONCLUSIVE in {monitor.status, property_result.status}:
                        incomplete.append(Outcome.PARTIAL)
                    violated = list(monitor.violated_rule_ids)
                    if property_result.status is EvaluationStatus.VIOLATED:
                        violated.append("global_property")
                    if violated:
                        edge = _edge_from_witness(
                            self.invariant,
                            baseline,
                            candidate_depth,
                            witness,
                            tuple(violated),
                            expansion,
                        )
                        key = _edge_key(self.invariant.id, edge)
                        if key not in finding_keys:
                            finding_keys.add(key)
                            edges.append(edge)
                            if deepest is None or _better_edge(edge, deepest):
                                deepest = edge
                    state_key = canonical_state_hash(after)
                    previous_depth = visited_depth.get(state_key)
                    if previous_depth is not None and previous_depth <= candidate_depth:
                        continue
                    visited_depth[state_key] = candidate_depth
                    next_chain = self.executor.coordinator.next_chain(expansion.chain, after)
                    frontier.push(
                        SearchState(
                            global_state=after,
                            chain_context=next_chain,
                            constraints=state.constraints
                            + candidate.pre_conditions
                            + expansion.constraints,
                            sequence=state.sequence + expansion.prefix,
                            evidence=witness.evidence,
                            depth=candidate_depth,
                            branch_id=expansion.branch_id,
                            parent_branch_id=state.branch_id,
                        ),
                        candidate.suspicion,
                    )

        outcome = (
            Verdict.VIOLATED
            if edges
            else Verdict.INCONCLUSIVE
            if incomplete
            else Verdict.NOT_OBSERVED
        )
        return SearchResult(
            witnesses=tuple(witnesses),
            edges=tuple(edges),
            deepest_edge=deepest,
            baseline=baseline,
            budget_exhausted=budget_used >= self.config.max_states,
            exhausted=not bool(frontier),
            outcome=outcome,
            budget_used=budget_used,
            budget_total=self.config.max_states,
            incomplete_outcomes=tuple(dict.fromkeys(incomplete)),
        )


def state_fingerprint(state: SearchState) -> str:
    return canonical_state_hash(state.global_state)


def trace_hash(sequence: tuple[Any, ...]) -> str:
    payload = [_step_payload(step) for step in sequence]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _step_payload(step: Any) -> Any:
    if hasattr(step, "__dict__"):
        return {
            key: value.value if hasattr(value, "value") else value
            for key, value in vars(step).items()
        }
    return str(step)


def _witness(
    state: SearchState,
    candidate: Candidate,
    expansion: AppliedPrefix,
    after: GlobalState,
) -> WitnessState:
    evidence = state.evidence
    if candidate.evidence is not None:
        evidence += (candidate.evidence,)
    evidence += expansion.evidence
    correlation = ""
    for event in expansion.events:
        for value in event.fields.values():
            if isinstance(value, str) and value.startswith("0x"):
                correlation = value
                break
    return WitnessState(
        snapshot=after,
        correlation_value=correlation,
        chain=expansion.chain,
        branch_id=expansion.branch_id,
        parent_branch_id=expansion.parent_branch_id,
        call_sequence=state.sequence + expansion.prefix,
        constraints=state.constraints + candidate.pre_conditions + expansion.constraints,
        evidence=evidence,
    )


def _edge_from_witness(
    invariant: CrossChainInvariant,
    baseline: BaselineResult,
    depth: int,
    witness: WitnessState,
    violated: tuple[str, ...],
    expansion: AppliedPrefix,
) -> EdgeCase:
    segment_strengths = {
        f"{witness.chain.value}:{index}": EvidenceStrength.REPLAYED
        for index, _ in enumerate(witness.call_sequence)
    }
    source = (
        ViolationSource.AMPLIFIED_BY_TRACE
        if baseline.status is BaselineStatus.VIOLATED
        else ViolationSource.INTRODUCED_BY_TRACE
    )
    return EdgeCase(
        depth=depth,
        witness=witness,
        segment_strengths=segment_strengths,
        aggregate_strength=EvidenceStrength.REPLAYED,
        violated_clauses=tuple(sorted(set(violated))),
        violation_source=source,
        impact=expansion.impact
        or Impact(
            invariant.severity,
            affected_chains=tuple(sorted(witness.snapshot.chain_snapshots, key=str)),
        ),
        chains=tuple(sorted(witness.snapshot.chain_snapshots, key=str)),
    )


def _edge_key(invariant_id: str, edge: EdgeCase) -> tuple[Any, ...]:
    state_hash = canonical_state_hash(edge.witness.snapshot) if edge.witness else ""
    trace = edge.witness.call_sequence if edge.witness else ()
    return (
        invariant_id,
        edge.violated_clauses,
        edge.violation_source.value,
        state_hash,
        trace_hash(trace),
    )


def _better_edge(candidate: EdgeCase, current: EdgeCase) -> bool:
    severity = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
    candidate_trace = candidate.witness.call_sequence if candidate.witness else ()
    current_trace = current.witness.call_sequence if current.witness else ()
    return (
        -candidate.depth,
        -severity.get(candidate.impact.severity, 0),
        trace_hash(candidate_trace),
    ) < (
        -current.depth,
        -severity.get(current.impact.severity, 0),
        trace_hash(current_trace),
    )


def _constraints_consistent(
    existing: tuple[Constraint, ...], incoming: tuple[Constraint, ...]
) -> bool:
    values = {
        (constraint.kind, constraint.target, constraint.chain): constraint.value
        for constraint in existing
    }
    return all(
        values.get((constraint.kind, constraint.target, constraint.chain), constraint.value)
        == constraint.value
        for constraint in incoming
    )
