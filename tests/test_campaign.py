"""End-to-end deterministic campaign from invariant IR to reportable result."""

from devil.core import (
    Call,
    Candidate,
    ChainId,
    ForkSnapshot,
    GlobalState,
    Outcome,
    SearchState,
)
from devil.harness import BranchResult, CampaignScheduler, SearchConfig
from devil.invariant.ir import Context, CrossChainInvariant, QuantifiedPredicate


def test_campaign_evaluates_base_then_finds_trace_violation() -> None:
    invariant = CrossChainInvariant(
        id="locked-equals-minted",
        contexts={
            ChainId.ETHEREUM: (Context(ChainId.ETHEREUM, "Bridge", "0xEth", fork_block=100),),
            ChainId.POLYGON: (Context(ChainId.POLYGON, "Bridge", "0xPoly", fork_block=200),),
        },
        property=QuantifiedPredicate(predicate="locked == minted"),
    )
    initial = SearchState(
        GlobalState(
            chain_snapshots={
                ChainId.ETHEREUM: ForkSnapshot(ChainId.ETHEREUM, 100),
                ChainId.POLYGON: ForkSnapshot(ChainId.POLYGON, 200),
            }
        ),
        ChainId.ETHEREUM,
    )

    def propose(state: SearchState):
        if state.depth == 0:
            yield Candidate(
                "submit(bytes)",
                (Call("submit(bytes)", chain=ChainId.ETHEREUM),),
                chain=ChainId.ETHEREUM,
                suspicion=0.9,
            )

    def execute(state: SearchState, candidate: Candidate) -> BranchResult:
        return BranchResult(
            Outcome.SUCCESS,
            GlobalState(
                chain_snapshots=state.global_state.chain_snapshots,
                trace=state.global_state.trace + (candidate.target_function,),
            ),
            correlation_value="0xmessage",
        )

    def evaluate(state: GlobalState) -> bool | None:
        return "submit(bytes)" not in state.trace

    result = CampaignScheduler(SearchConfig(max_depth=1, max_states=2)).run(
        invariant,
        initial,
        propose=propose,
        execute=execute,
        evaluate=evaluate,
        read_values=lambda _: {"locked": 1, "minted": 1},
    )
    assert result.baseline.status.value == "holds"
    assert result.search.outcome.value == "violated"
    assert result.sub_invariants[0].id == "locked-equals-minted:ethereum"
