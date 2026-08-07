"""Campaign orchestration around baseline evaluation and canonical search."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from devil.core.types import BaselineResult, Candidate, SearchResult, SearchState
from devil.harness.decomposition import SubInvariant, decompose
from devil.harness.evaluation import (
    BaselineEvaluator,
    ObservationSetEvaluator,
    initialize_liveness_obligations,
)
from devil.harness.executor import CanonicalForkExecutor
from devil.harness.search import SearchConfig, UnifiedSearch
from devil.invariant.ir import CrossChainInvariant


@dataclass(frozen=True)
class CampaignResult:
    invariant_id: str
    baseline: BaselineResult
    search: SearchResult
    sub_invariants: tuple[SubInvariant, ...]
    observations: ObservationSetEvaluator


class CampaignScheduler:
    """Run one invariant over the exact base state used to seed its frontier."""

    def __init__(self, config: SearchConfig | None = None) -> None:
        self.config = config or SearchConfig()

    def run(
        self,
        invariant: CrossChainInvariant,
        executor: CanonicalForkExecutor,
        *,
        propose: Callable[[SearchState], Iterable[Candidate]],
        read_values: Callable[[CrossChainInvariant], Mapping[str, Any]] | None = None,
        observations: ObservationSetEvaluator | None = None,
    ) -> CampaignResult:
        base_state = executor.initial_state()
        base_state = initialize_liveness_obligations(base_state, invariant)
        values = read_values or (lambda _: base_state.observed_values)
        baseline = BaselineEvaluator(values).evaluate(invariant)
        observation_set = observations or ObservationSetEvaluator(
            invariant.observation_set.max_items
        )
        entry_chain = invariant.contexts[invariant.entry_context].chain_id
        initial = SearchState(base_state, entry_chain, branch_id="root")
        search = UnifiedSearch(
            invariant=invariant,
            executor=executor,
            propose=propose,
            config=self.config,
            observations=observation_set,
        ).run(initial, baseline=baseline)
        return CampaignResult(
            invariant_id=invariant.id,
            baseline=baseline,
            search=search,
            sub_invariants=decompose(invariant),
            observations=observation_set,
        )
