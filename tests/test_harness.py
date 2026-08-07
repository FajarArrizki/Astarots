"""Tests for the unified branch-local search frontier."""

from devil.core import Call, Candidate, ChainId, ForkSnapshot, GlobalState, Outcome, SearchState
from devil.harness import BranchResult, SearchConfig, UnifiedSearch


def test_unified_search_keeps_causal_branch_local_state() -> None:
    initial_global = GlobalState(
        chain_snapshots={ChainId.ETHEREUM: ForkSnapshot(ChainId.ETHEREUM, 100)},
    )
    initial = SearchState(initial_global, ChainId.ETHEREUM, branch_id="root")

    def propose(state: SearchState):
        if state.depth == 0:
            return [
                Candidate(
                    "safe()",
                    (Call("safe()", chain=ChainId.ETHEREUM),),
                    suspicion=0.2,
                    chain=ChainId.ETHEREUM,
                ),
                Candidate(
                    "exploit()",
                    (Call("exploit()", chain=ChainId.ETHEREUM),),
                    suspicion=0.9,
                    chain=ChainId.ETHEREUM,
                ),
            ]
        return []

    def execute(state: SearchState, candidate: Candidate) -> BranchResult:
        call = candidate.call_sequence[0]
        next_global = GlobalState(
            chain_snapshots=state.global_state.chain_snapshots,
            trace=state.global_state.trace + (call.function_signature,),
        )
        return BranchResult(outcome=Outcome.SUCCESS, state=next_global)

    def evaluate(state: GlobalState) -> bool | None:
        return "exploit()" not in state.trace

    result = UnifiedSearch(
        config=SearchConfig(max_depth=1, max_states=4),
        propose=propose,
        execute=execute,
        evaluate=evaluate,
    ).run(initial)
    assert result.outcome.value == "violated"
    assert result.deepest_edge is not None
    assert result.deepest_edge.witnesses[0].snapshot.trace == ("exploit()",)
    assert initial_global.trace == ()
