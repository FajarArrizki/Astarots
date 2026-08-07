"""Campaign orchestration around baseline evaluation and unified search."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from devil.core.types import BaselineResult, Candidate, GlobalState, SearchResult, SearchState
from devil.harness.decomposition import SubInvariant, decompose
from devil.harness.evaluation import BaselineEvaluator, ObservationSetEvaluator
from devil.harness.search import BranchResult, SearchConfig, UnifiedSearch
from devil.invariant.ir import CrossChainInvariant


@dataclass(frozen=True)
class CampaignResult:
    """Complete output of one invariant campaign."""

    invariant_id: str
    baseline: BaselineResult
    search: SearchResult
    sub_invariants: tuple[SubInvariant, ...]
    observations: ObservationSetEvaluator


class CampaignScheduler:
    """Run campaigns in a fixed order with one global frontier per invariant."""

    def __init__(self, config: SearchConfig | None = None) -> None:
        self.config = config or SearchConfig()

    def run(
        self,
        invariant: CrossChainInvariant,
        initial: SearchState,
        *,
        propose: Callable[[SearchState], Iterable[Candidate]],
        execute: Callable[[SearchState, Candidate], BranchResult],
        evaluate: Callable[[GlobalState], bool | None],
        read_values: Callable[[CrossChainInvariant], Mapping[str, Any]],
        observations: ObservationSetEvaluator | None = None,
    ) -> CampaignResult:
        baseline = BaselineEvaluator(read_values).evaluate(invariant)
        search = UnifiedSearch(
            config=self.config,
            propose=propose,
            execute=execute,
            evaluate=evaluate,
        ).run(initial, baseline=baseline)
        return CampaignResult(
            invariant_id=invariant.id,
            baseline=baseline,
            search=search,
            sub_invariants=decompose(invariant),
            observations=observations or ObservationSetEvaluator(),
        )
