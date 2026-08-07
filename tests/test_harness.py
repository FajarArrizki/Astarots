from __future__ import annotations

from dataclasses import replace

from devil.core.relay import MessageCoordinator, relay_policy_hash
from devil.core.types import (
    BaselineResult,
    BaselineStatus,
    Call,
    Candidate,
    ChainId,
    ExecutionStatus,
    GlobalState,
    MessageStatus,
    Outcome,
    RelayAction,
    RelayDataset,
    RelayMode,
    SearchState,
    Verdict,
)
from devil.harness.evaluation import (
    EvaluationStatus,
    evaluate_transition_monitors,
)
from devil.harness.executor import BackendCallResult, CanonicalForkExecutor
from devil.harness.search import SearchConfig, UnifiedSearch, canonical_state_hash
from devil.invariant.expression import parse_expression
from devil.invariant.ir import (
    Binding,
    BindingReduce,
    Context,
    CrossChainInvariant,
    FunctionSelector,
    ObservationKind,
    ObservationPolicy,
    ObservationSet,
    Property,
    PropertyKind,
    QuantifiedPredicate,
    QuantifierKind,
    StateReference,
    StateReferenceKind,
    TransitionEffect,
    TransitionPredicate,
    TransitionRule,
)


class EmptyCoordinator:
    policy_hash = "sha256:" + "9" * 64

    class Policy:
        mode = RelayMode.MODELED_RELAY

    policy = Policy()

    def initial_states(self):
        return {}

    def propose_transitions(self, state, chain=None):
        return ()

    def next_chain(self, current, state):
        chains = tuple(state.chain_snapshots)
        return chains[(chains.index(current) + 1) % len(chains)]

    def validate_environment(self, state, transition):
        raise AssertionError("not used")

    def apply_transition(self, state, transition):
        raise AssertionError("not used")


class FakeBackend:
    def __init__(self) -> None:
        self.reverted: list[tuple[ChainId, str]] = []
        self.calls = 0
        self.restores = 0

    def checkpoint(self, chain: ChainId) -> str:
        return f"checkpoint-{self.calls}"

    def revert(self, chain: ChainId, checkpoint: str) -> None:
        self.reverted.append((chain, checkpoint))

    def restore(self, state, targets) -> None:
        self.restores += 1

    def apply_call(self, snapshot, call, target_address):
        self.calls += 1
        if call.function_signature == "revert()":
            return BackendCallResult(ExecutionStatus.REVERTED, revert_data="0x08c379a0")
        return BackendCallResult(
            ExecutionStatus.APPLIED,
            block_number=snapshot.block_number + 1,
            timestamp=snapshot.timestamp + 1,
            evidence_raw=f"call-{self.calls}",
        )

    def advance(self, snapshot, transition):
        return replace(
            snapshot,
            block_number_delta=transition.target_block - snapshot.base_block,
            timestamp_delta=transition.target_timestamp - snapshot.base_timestamp,
        )


def _invariant() -> CrossChainInvariant:
    eth_ref = StateReference(
        "ethereum.bridge",
        StateReferenceKind.GETTER,
        getter=FunctionSelector("ethereum.bridge", "locked()"),
    )
    poly_ref = StateReference(
        "polygon.bridge",
        StateReferenceKind.GETTER,
        getter=FunctionSelector("polygon.bridge", "minted()"),
    )
    return CrossChainInvariant(
        id="locked-equals-minted",
        contexts={
            "ethereum.bridge": Context("ethereum.bridge", ChainId.ETHEREUM),
            "polygon.bridge": Context("polygon.bridge", ChainId.POLYGON, "destination"),
        },
        entry_context="ethereum.bridge",
        correlation_extractor_id="bridge_message",
        bindings=(
            Binding("locked", (eth_ref,), BindingReduce.IDENTITY),
            Binding("minted", (poly_ref,), BindingReduce.IDENTITY),
        ),
        observation_policy=ObservationPolicy(ObservationKind.PER_TRANSACTION),
        observation_set=ObservationSet(sources=("touched",), max_items=32),
        transition_predicates=(
            TransitionPredicate(
                "ethereum.bridge",
                "locked",
                (
                    TransitionRule(
                        "locked.increase",
                        FunctionSelector("ethereum.bridge", "first()"),
                        TransitionEffect.INCREASE,
                    ),
                ),
            ),
            TransitionPredicate(
                "polygon.bridge",
                "minted",
                (
                    TransitionRule(
                        "minted.increase",
                        FunctionSelector("polygon.bridge", "second()"),
                        TransitionEffect.INCREASE,
                    ),
                ),
            ),
        ),
        property=Property(
            PropertyKind.SAFETY,
            QuantifiedPredicate(
                QuantifierKind.FORALL,
                (),
                parse_expression("locked == minted", {"locked": "uint256", "minted": "uint256"}),
            ),
        ),
    )


def _executor(
    snapshot_set, actor_policy, relay_dataset
) -> tuple[CanonicalForkExecutor, FakeBackend]:
    backend = FakeBackend()
    executor = CanonicalForkExecutor(
        snapshot_set,
        backend,
        relay_dataset=RelayDataset(
            relay_dataset.schema_version,
            relay_dataset.dataset_hash,
            relay_dataset.protocol,
            relay_dataset.source_block_ranges,
            (),
        ),
        actor_policy=actor_policy,
        coordinator=EmptyCoordinator(),
        observe=lambda state: {
            "locked": 0,
            "minted": int(len(state.trace) >= 2),
        },
    )
    return executor, backend


def test_relay_lifecycle_requires_finality_delay_and_exact_order(
    snapshot_set, relay_dataset, relay_config
) -> None:
    coordinator = MessageCoordinator(relay_dataset, relay_config)
    state = GlobalState(
        chain_snapshots={
            ChainId.ETHEREUM: replace(
                snapshot_set.snapshot(ChainId.ETHEREUM), block_number_delta=2
            ),
            ChainId.POLYGON: snapshot_set.snapshot(ChainId.POLYGON),
        },
        pending_messages=coordinator.initial_states(),
        relay_dataset_hash=relay_dataset.dataset_hash,
        relay_policy_hash=coordinator.policy_hash,
        relay_mode=relay_config.mode,
    )
    statuses = []
    for expected_action in (
        RelayAction.FINALIZE,
        RelayAction.MAKE_ELIGIBLE,
        RelayAction.DELIVER,
    ):
        candidate = coordinator.propose_transitions(state)[0]
        transition = candidate.call_sequence[0]
        assert transition.action is expected_action
        state, evidence = coordinator.apply_transition(state, transition)
        statuses.append(state.pending_messages["message-1"].status)
        assert evidence[0].raw_hash.startswith("sha256:")
    assert statuses == [
        MessageStatus.SOURCE_FINALIZED,
        MessageStatus.RELAY_ELIGIBLE,
        MessageStatus.DELIVERED,
    ]
    assert state.pending_messages["message-1"].transition_history[
        -1
    ].evidence_hash == relay_policy_hash(relay_config)


def test_canonical_executor_rolls_back_reverts_and_preserves_parent(
    snapshot_set, actor, actor_policy, relay_dataset
) -> None:
    executor, backend = _executor(snapshot_set, actor_policy, relay_dataset)
    initial = executor.initial_state()
    call = Call(
        "revert()",
        chain=ChainId.ETHEREUM,
        context_id="ethereum.bridge",
        calldata="0x1234",
        actor=actor,
    )
    result = executor.apply_step(initial, call)
    assert result.outcome is Outcome.SUCCESS
    assert result.execution_status is ExecutionStatus.REVERTED
    assert result.global_state is None
    assert initial.trace == ()
    assert backend.reverted


def test_transition_monitor_rejects_undeclared_binding_change(actor) -> None:
    invariant = _invariant()
    before = GlobalState(observed_values={"locked": 0, "minted": 0})
    after = GlobalState(observed_values={"locked": 0, "minted": 1})
    wrong_call = Call(
        "unknown()",
        chain=ChainId.POLYGON,
        context_id="polygon.bridge",
        calldata="0x12",
        actor=actor,
    )
    monitor = evaluate_transition_monitors(invariant, before, after, wrong_call)
    assert monitor.status is EvaluationStatus.VIOLATED
    assert monitor.violated_rule_ids == ("unexpected_transition:minted",)


def test_search_counts_each_authoritative_step_and_finds_depth_two_violation(
    snapshot_set, actor, actor_policy, relay_dataset
) -> None:
    invariant = _invariant()
    executor, backend = _executor(snapshot_set, actor_policy, relay_dataset)
    first = Call(
        "first()",
        chain=ChainId.ETHEREUM,
        context_id="ethereum.bridge",
        calldata="0x1111",
        actor=actor,
    )
    second = Call(
        "second()",
        chain=ChainId.POLYGON,
        context_id="polygon.bridge",
        calldata="0x2222",
        actor=actor,
    )

    def propose(state: SearchState):
        if state.depth:
            return ()
        return (Candidate("second()", (first, second), suspicion=1.0),)

    initial_global = executor.initial_state()
    search = UnifiedSearch(
        invariant=invariant,
        executor=executor,
        propose=propose,
        config=SearchConfig(max_depth=2, max_states=2, branching_caps=(2, 2)),
    ).run(
        SearchState(initial_global, ChainId.ETHEREUM),
        baseline=BaselineResult(BaselineStatus.HOLDS),
    )
    assert search.outcome is Verdict.VIOLATED
    assert search.deepest_edge is not None and search.deepest_edge.depth == 2
    assert search.budget_used == 2
    assert len(search.deepest_edge.witness.call_sequence) == 2
    assert canonical_state_hash(initial_global) != canonical_state_hash(
        search.deepest_edge.witness.snapshot
    )
    assert backend.restores >= 2


def test_baseline_violation_is_depth_zero_edge(snapshot_set, actor_policy, relay_dataset) -> None:
    invariant = _invariant()
    executor, _ = _executor(snapshot_set, actor_policy, relay_dataset)
    initial = SearchState(executor.initial_state(), ChainId.ETHEREUM)
    result = UnifiedSearch(
        invariant=invariant,
        executor=executor,
        propose=lambda state: (),
    ).run(initial, baseline=BaselineResult(BaselineStatus.VIOLATED, "base mismatch"))
    assert result.outcome is Verdict.VIOLATED
    assert result.deepest_edge is not None and result.deepest_edge.depth == 0


def test_pinned_two_chain_workflow_is_deterministic_across_two_runs(
    snapshot_set, actor, actor_policy, relay_dataset
) -> None:
    first = Call(
        "first()",
        chain=ChainId.ETHEREUM,
        context_id="ethereum.bridge",
        calldata="0x1111",
        actor=actor,
    )
    second = Call(
        "second()",
        chain=ChainId.POLYGON,
        context_id="polygon.bridge",
        calldata="0x2222",
        actor=actor,
    )

    def run_once() -> tuple[str, int, int, str]:
        executor, _ = _executor(snapshot_set, actor_policy, relay_dataset)
        initial = SearchState(executor.initial_state(), ChainId.ETHEREUM)
        result = UnifiedSearch(
            invariant=_invariant(),
            executor=executor,
            propose=lambda state: (
                (Candidate("second()", (first, second), suspicion=1.0),) if state.depth == 0 else ()
            ),
            config=SearchConfig(max_depth=2, max_states=2, branching_caps=(2, 2)),
        ).run(initial, baseline=BaselineResult(BaselineStatus.HOLDS))
        assert result.deepest_edge is not None
        return (
            result.outcome.value,
            result.deepest_edge.depth,
            result.budget_used,
            canonical_state_hash(result.deepest_edge.witness.snapshot),
        )

    assert run_once() == run_once()
