# Algorithm — Cross-Chain Deepest Edge Discovery

The search starts from coherent pinned mainnet forks and reports violations with their full causal trace. “Deepest” is operational: greatest global action depth within configured bounds, then impact severity, then canonical trace hash for deterministic ties; it is not a proof that no deeper trace exists.

The algorithm uses one frontier across every configured chain. Each state is immutable and branch-local; source actions, relay transitions, and destination actions remain on one causal lineage. No Cartesian recombination is performed.

---

## Global Search State (Branch-Local)

Each node in the search tree carries an immutable copy of the global state — not a shared mutable reference. This prevents branches from interfering with each other:

```
GlobalState:
    snapshot_set_id: str
    chain_snapshots: Map[ChainId, ForkSnapshot]
    pending_messages: Map[MessageId, MessageState]
    relay_dataset_hash: str
    observation_set_hash: str
    relay_policy_hash: str
    relay_mode: RelayMode
    actor_policy: ActorPolicy
    trace: ActionTrace
    assumptions: list[Assumption]
    liveness_obligations: Map[ObligationId, LivenessObligation]

ForkSnapshot:
    chain_id: ChainId
    base: BaseForkFingerprint
    backend_handle: str                    # canonical executor only; never serialized
    overlay_id: int                        # branch-local checkpoint
    state_diff: list[SlotChange]
    code_diff: list[CodeChange]
    touched_slots_manifest: list[str]
    emitted_logs: list[Event]
    block_number: int
    timestamp: int

BaseForkFingerprint:
    chain_id: ChainId
    block_number: int
    block_hash: str
    state_root: str
    target_code_hashes: Map[ContextId, str]
    proxy_implementations: Map[ContextId, str]
    proxy_kinds: Map[ContextId, str]
    proxy_implementation_code_hashes: Map[ContextId, str]
    artifact_hashes: Map[ContextId, str]
    fork_cache_hash: str

SlotChange:
    context_id: ContextId
    slot: str
    old_value: bytes32
    new_value: bytes32

CodeChange:
    context_id: ContextId
    old_code_hash: bytes32
    new_code_hash: bytes32

MessageState:
    envelope: ProtocolMessageEnvelope
    status: EMITTED | SOURCE_FINALIZED | RELAY_ELIGIBLE | DELIVERED |
            CONSUMED | REJECTED | EXPIRED
    transition_history: list[RelayTransition]

LivenessObligation:
    id: ObligationId
    binding_key: bytes                     # canonical tuple of quantified values
    correlation_value: Optional[bytes]
    clock_chain: ChainId
    start_block: int
    start_timestamp: int
    deadline: Deadline
    status: ACTIVE | SATISFIED | VIOLATED | INCONCLUSIVE
    evidence: list[Evidence]

Call:
    chain_id: ChainId                      # must match the bound context
    context_id: ContextId
    calldata: bytes
    value: int
    actor: Actor
    gas_limit: int

RelayTransition:
    message_id: bytes
    action: FINALIZE | MAKE_ELIGIBLE | DELIVER | CONSUME | REJECT | EXPIRE
    from_status: MessageStatus
    to_status: MessageStatus
    source_chain: ChainId
    destination_chain: ChainId
    relay_mode: RelayMode
    policy_ref: str

EnvironmentTransition:
    chain_id: ChainId
    target_block: int
    target_timestamp: int
    reason: FINALITY | RELAY_DELAY | EXPIRY | OBSERVATION | LIVENESS_DEADLINE
    policy_ref: str

CrossChainStep: Call | RelayTransition | EnvironmentTransition

ActionTrace:
    steps: list[CrossChainStep]             # exact applied calls/lifecycle/clock steps
    base_fingerprints: Map[ChainId, BaseForkFingerprint]
```

Liveness obligations are branch-local state. Initialization and every applied step evaluate the typed trigger over each bounded quantifier tuple, create at most one obligation per `(property, binding_key)`, and advance it on the deadline's declared chain clock. A true predicate marks an obligation `SATISFIED`; a complete observation past the deadline marks it `VIOLATED`; missing relay data or an incomplete observation marks it `INCONCLUSIVE`.

`CanonicalForkExecutor.apply_step()` is the only operation allowed to mutate a child checkpoint:

```
CanonicalExecutionResult:
    outcome: Success | Timeout | ToolError | Partial
    execution_status: Optional[APPLIED | REVERTED]
    revert_data: Optional[bytes]
    revert_kind: Optional[EVM_REVERT | OUT_OF_GAS]
    global_state: Optional[GlobalState]
    events: list[Event]
    constraints: ConstraintSet
    evidence: list[Evidence]
    impact: Optional[Impact]
```

Execution is atomic at the **step** boundary. `global_state` is present only for `Success + APPLIED`. A revert, timeout, tool error, partial backend result, or failed coordinator transition discards that step's child checkpoint. A successful step is appended to `ActionTrace`; then liveness obligations are updated before the state may become another prefix or enter the frontier.

`execute_candidate_prefixes` applies one `CandidateTrace` step by step, using the canonical executor and coordinator for every step:

```
CandidatePrefixResult:
    applied_prefixes: list[AppliedPrefix]   # one entry per successful step
    steps_attempted: int                    # apply_step attempts; global budget units
    terminal_outcome: Completed | Reverted | DepthBound | BudgetExhausted |
                      Timeout | ToolError | Partial

AppliedPrefix:
    prefix: list[CrossChainStep]            # cumulative prefix of the candidate's call_sequence
    executed_step: CrossChainStep           # last step in this prefix
    before_state: GlobalState               # state immediately before executed_step
    after_state: GlobalState                # executor + coordinator + liveness update
    chain: ChainId
    branch_id: str
    parent_branch_id: str
    events: list[Event]                     # events from executed_step only
    constraints: ConstraintSet              # cumulative constraints for this prefix
    evidence: list[Evidence]                # cumulative evidence for this prefix
    impact: Optional[Impact]
```

`Completed` means every candidate step was applied. `Reverted` means the next attempted call reverted; any earlier applied prefixes remain valid results. `DepthBound` and `BudgetExhausted` are configured search limits, not tool failures. `Timeout`, `ToolError`, and `Partial` preserve diagnostics and make the campaign incomplete, while already returned prefixes remain replayable.

```
PropertyEvaluation:
    status: HOLDS | VIOLATED | PENDING | INCONCLUSIVE
    correlation_values: list[bytes]
    evidence: list[Evidence]

MonitorEvaluation:
    status: HOLDS | VIOLATED | INCONCLUSIVE
    violated_rule_ids: list[str]
    evidence: list[Evidence]
```

When a branch expands, the executor checkpoints the backend (EVM snapshot/revert or copy-on-write overlay) and applies one step. Only state/code diffs, relevant logs, touched slots, and clocks are materialized; storage is fetched lazily from the RPC base on first access. Branches are isolated—a message delivered in one branch does not affect another.

---

## Unified Frontier

The frontier is a single priority queue of `SearchState` nodes. Every node contains all configured chains; candidate generation may inspect any chain eligible under the invariant and relay policy. Causal order is enforced within each branch: a destination call that consumes a message is ineligible until that same branch has emitted, finalized, and delivered it.

```
unified_search(targets: TargetSet, snapshot_set, relay_dataset,
               relay_policy, actor_policy, invariant, adapter_registry,
               tool_config, max_depth=8, budget=200,
               max_consecutive_expansions_per_chain=4):
    canonical_executor = CanonicalForkExecutor(
        targets=targets,
        snapshot_set=snapshot_set,
        relay_dataset=relay_dataset,
        relay_policy=relay_policy,
        actor_policy=actor_policy,
    )
    coordinator = MessageCoordinator(
        snapshot_set=snapshot_set,
        relay_dataset=relay_dataset,
        relay_policy=relay_policy,
    )
    candidate_workers = CandidateWorkers(
        registry=adapter_registry,
        tool_config=tool_config,
    )
    base_state = GlobalState.from_snapshot_set(
        snapshot_set=snapshot_set,
        relay_dataset=relay_dataset,
        relay_policy=relay_policy,
        actor_policy=actor_policy,
        observation_set=invariant.observation_set,
        assumptions=invariant.assumptions,
        executor=canonical_executor,
    )
    base_state = initialize_liveness_obligations(base_state, invariant)
    frontier = PriorityQueue()
    initial = SearchState(
        global_state=base_state,
        chain_context=invariant.contexts[invariant.entry_context].chain_id,
        constraints=compile_constraints(base_state.assumptions),
        sequence=[],
        evidence=[],
        depth=0,
        branch_id="root",
        parent_branch_id=None,
    )
    frontier.push(initial, priority=0)
    visited_depth = {canonical_state_hash(base_state): 0}
    deepest = None
    edges = []
    finding_keys = set()
    all_witnesses = []
    state_budget = budget
    had_inconclusive = False

    # STEP 0: evaluate the exact same base state used by the frontier.
    baseline = evaluate_baseline(
        global_state=base_state,
        invariant=invariant,
    )
    # baseline.status: HOLDS | PENDING | VIOLATED | UNOBSERVABLE | INCONCLUSIVE
    if baseline.status in {UNOBSERVABLE, INCONCLUSIVE}:
        had_inconclusive = True
    if baseline.status == VIOLATED:
        baseline_edge = edge_from_baseline(
            baseline=baseline,
            global_state=base_state,
            invariant=invariant,
        )
        record_edge(edges, finding_keys, baseline_edge)
        deepest = baseline_edge

    while frontier is not empty AND state_budget > 0:
        state = frontier.pop_with_chain_fairness(
            max_consecutive=max_consecutive_expansions_per_chain
        )

        if state.depth >= max_depth:
            continue

        branching_cap = adaptive_branching_cap(state.depth)
        chain = state.chain_context

        probe_result = candidate_workers.propose(
            target=targets.for_chain(chain),
            invariant=invariant.sub_invariant(chain),
            constraints=state.constraints,
            projection=project_for_tool(state.global_state, chain, invariant),
            chain=chain,
        )
        if probe_result.outcome in {Timeout, ToolError, Unsupported, Partial}:
            had_inconclusive = True
        tool_candidates = (
            (probe_result.value or [])
            if probe_result.outcome in {Success, Counterexample, Partial}
            else []
        )
        lifecycle_candidates = coordinator.propose_transitions(
            global_state=state.global_state,
            chain=chain,
        )
        candidates = tool_candidates + lifecycle_candidates
        if not candidates:
            continue
        ranked = rank_by_suspicion(candidates)

        # Existing on-chain values and content-addressed relay records seed
        # constraints; a fork alone is not assumed to contain pending attestations.
        for cand in ranked[:branching_cap]:
            if state_budget == 0:
                break
            if not cand.call_sequence:
                continue

            extracted = extract_constraints(cand)
            if not constraints_are_consistent(state.constraints, extracted):
                continue

            candidate_execution = execute_candidate_prefixes(
                executor=canonical_executor,
                coordinator=coordinator,
                base_state=state.global_state,
                candidate=cand,
                invariant=invariant,
                max_steps=max_depth - state.depth,
                budget=state_budget,
                parent_branch_id=state.branch_id,
            )
            state_budget -= candidate_execution.steps_attempted
            if candidate_execution.terminal_outcome in {
                Timeout, ToolError, Partial
            }:
                had_inconclusive = True

            for expansion in candidate_execution.applied_prefixes:
                next_global_state = update_liveness_obligations(
                    expansion.after_state, invariant
                )
                candidate_depth = state.depth + len(expansion.prefix)
                witness = WitnessState(
                    snapshot=next_global_state,
                    correlation_value=extract_correlation(
                        expansion.events, invariant
                    ),
                    chain=expansion.chain,
                    branch_id=expansion.branch_id,
                    parent_branch_id=expansion.parent_branch_id,
                    call_sequence=state.sequence + expansion.prefix,
                    constraints=state.constraints + expansion.constraints,
                    evidence=state.evidence
                    + [cand.evidence]
                    + expansion.evidence,
                    status=REACHABLE,
                )
                all_witnesses.append(witness)

                monitor_evaluation = evaluate_transition_monitors(
                    invariant=invariant,
                    before=expansion.before_state,
                    after=witness.snapshot,
                    executed_step=expansion.executed_step,
                )
                property_evaluation = evaluate_property(
                    invariant=invariant,
                    global_state=witness.snapshot,
                )
                if INCONCLUSIVE in {
                    monitor_evaluation.status,
                    property_evaluation.status,
                }:
                    had_inconclusive = True

                violated_clauses = monitor_evaluation.violated_rule_ids
                if property_evaluation.status == VIOLATED:
                    violated_clauses = violated_clauses + ["global_property"]

                if violated_clauses:
                    confirmations = confirm_trace_segments(
                        workers=candidate_workers,
                        targets=targets,
                        witness=witness,
                        invariant=invariant,
                        violated_clauses=violated_clauses,
                    )
                    strengths = aggregate_evidence(
                        replay_evidence=witness.evidence,
                        confirmations=confirmations,
                        trace=witness.call_sequence,
                    )
                    edge = EdgeCase(
                        depth=candidate_depth,
                        witness=witness,
                        confirmations=confirmations,
                        segment_strengths=strengths.per_segment,
                        aggregate_strength=strengths.aggregate,
                        aggregation_rule=strengths.rule,
                        violated_clauses=violated_clauses,
                        violation_source=classify_violation_source(
                            baseline,
                            witness,
                            monitor_evaluation,
                            property_evaluation,
                        ),
                        impact=expansion.impact or invariant.severity,
                        chains=sorted(
                            witness.snapshot.chain_snapshots.keys()
                        ),
                    )
                    if record_edge(edges, finding_keys, edge):
                        if deepest is None or better_edge(
                            edge,
                            deepest,
                            key=(
                                depth DESC,
                                impact DESC,
                                canonical_trace_hash ASC,
                            ),
                        ):
                            deepest = edge

                state_key = canonical_state_hash(witness.snapshot)
                prior_depth = visited_depth.get(state_key)
                if prior_depth is not None and prior_depth <= candidate_depth:
                    continue
                visited_depth[state_key] = candidate_depth

                next_chain = coordinator.next_chain(
                    current=expansion.chain,
                    global_state=next_global_state,
                    invariant=invariant,
                )
                next_state = SearchState(
                    global_state=next_global_state,
                    chain_context=next_chain or expansion.chain,
                    constraints=state.constraints + expansion.constraints,
                    sequence=state.sequence + expansion.prefix,
                    evidence=state.evidence
                    + [cand.evidence]
                    + expansion.evidence,
                    depth=candidate_depth,
                    branch_id=expansion.branch_id,
                    parent_branch_id=expansion.parent_branch_id,
                )
                frontier.push(
                    next_state,
                    priority=frontier_key(cand.suspicion, next_state),
                )

    return SearchResult(
        baseline=baseline,
        witnesses=all_witnesses,
        edges=edges,
        deepest_edge=deepest,
        budget_exhausted=(state_budget <= 0),
        outcome=(
            VIOLATED if edges else
            INCONCLUSIVE if had_inconclusive else
            NOT_OBSERVED
        ),
    )
```

Lifecycle candidates include the minimal deterministic `EnvironmentTransition` values that reach a finality boundary, relay delay boundary, message expiry, observation deadline, or active liveness deadline. The coordinator does not generate arbitrary timestamps or blocks; every clock jump names the policy boundary it reaches and counts toward global depth and budget.

A violated baseline produces a depth-zero edge with `violated_clauses=["global_property"]`, `violation_source=PRE_EXISTING_AT_SNAPSHOT`, the exact snapshot fingerprints, and no invented action trace. It remains in the result even when a deeper introduced or amplified violation is later selected.

---

## Core Structures

### SearchState

```
SearchState:
    global_state: GlobalState          # immutable branch-local snapshot
    chain_context: ChainId             # chain targeted by the next action
    constraints: list[Constraint]
    sequence: list[CrossChainStep]
    evidence: list[Evidence]
    depth: int                         # global causal action depth
    branch_id: str                     # unique state/lineage identifier
    parent_branch_id: Optional[str]    # ancestor relation
```

### WitnessState

A recorded intermediate state containing the full branch-local state. Reachable witnesses are retained even when no invariant breaks so later actions on the same causal branch can build on them.

```
WitnessState:
    snapshot: GlobalState
    correlation_value: Optional[bytes] # absent for steps without a message
    chain: ChainId
    branch_id: str
    call_sequence: list[CrossChainStep]
    parent_branch_id: Optional[str]
    constraints: list[Constraint]
    evidence: list[Evidence]
    status: REACHABLE | INCONCLUSIVE
```

### EdgeCase

```
EdgeCase:
    depth: int
    witness: WitnessState              # the witness that triggered the edge
    confirmations: list[ToolRunResult[BoundedConfirmation]]
    segment_strengths: Map[TraceSegmentId, EvidenceStrength]
    aggregate_strength: EvidenceStrength
    aggregation_rule: str
    violated_clauses: list[str]
    violation_source: PRE_EXISTING_AT_SNAPSHOT | INTRODUCED_BY_TRACE | AMPLIFIED_BY_TRACE
    impact: Impact
    chains: list[ChainId]
```

### SearchResult

```
SearchResult:
    witnesses: list[WitnessState]      # all recorded witnesses across chains
    edges: list[EdgeCase]              # every distinct violation found
    deepest_edge: Optional[EdgeCase]   # deepest violation found, if any
    baseline: BaselineResult           # invariant status at fork block before any probing
    budget_exhausted: bool
    outcome: VIOLATED | NOT_OBSERVED | INCONCLUSIVE

BaselineResult:
    status: HOLDS | PENDING | VIOLATED | UNOBSERVABLE | INCONCLUSIVE
    violation_kind: PRE_EXISTING_AT_SNAPSHOT | NONE
    reason: Optional[str]              # e.g. "cross-chain snapshot async — 3 messages in-flight"
```

### Constraint

```
Constraint:
    kind:   FUNCTION | STATE_VAR | TIMING | EXTERNAL_CALL | ACCESS | CROSS_CHAIN
    target_ref: str        # context-qualified selector, state reference, or address
    value: Any
    chain: ChainId
    source: str            # tool/adapter that produced it
    provenance_hash: str

ConstraintSet: list[Constraint]
```

### CandidateTrace

```
CandidateTrace:
    call_sequence: list[CrossChainStep]
    actor: Actor
    constraints: ConstraintSet
    suspicion: float
    evidence: Evidence
    originating_chain: ChainId
```
---

## Cross-Chain Witness Correlation

Cross-chain violations are evaluated against a **single** branch-local `GlobalState.snapshot`, not by pairing witnesses from different branches. Every witness carries a `snapshot` containing the full state of all chains at that point in the causal trace. The invariant is checked directly against that snapshot:

```
for witness in reachable_witnesses:
    parent = parent_snapshot(witness)
    last_step = witness.call_sequence[-1] if witness.call_sequence else None
    if last_step is None:
        continue
    monitor = evaluate_transition_monitors(
        invariant=invariant,
        before=parent,
        after=witness.snapshot,
        executed_step=last_step,
    )
    prop = evaluate_property(invariant, witness.snapshot)
    if monitor.status == VIOLATED or prop.status == VIOLATED:
        violations.append((witness, monitor, prop))
    elif monitor.status == INCONCLUSIVE or prop.status == INCONCLUSIVE:
        record_inconclusive(witness, monitor, prop)
```

No witnesses from different branches are combined. `branch_id` identifies a state and `parent_branch_id` records ancestry for trace explanation; the invariant itself is evaluated against a single witness snapshot that already contains every chain. Correlation values select the relevant messages within that snapshot, not another branch's state.

---

## Search Strategy

The algorithm is **best-first search with an adaptive per-parent branching cap**. The global priority queue persists across depths; this is not level-synchronous beam search.

| Global Depth | Branching Cap | Rationale |
|---|---|---|
| 0–1 | 4 | Broad source and setup exploration |
| 2–3 | 3 | Preserve alternative causal paths |
| 4–5 | 2 | Focus after relay/cross-chain interaction |
| 6–7 | 1 | Commit to the deepest supported path |

Depth counts authoritative actions across the whole causal trace, including relay transitions. Eight is an initial default, not a guarantee of coverage; governance, timelock, and multi-hop protocols may require a larger explicit bound.

A hard cap on authoritative candidate executions (`budget`, default 200) spans all chains. One budget unit is consumed each time the canonical executor applies one candidate trace. Tool-internal limits are separate: `echidna.test_limit = 50000` permits up to 50,000 generated sequences during one candidate-generation call and does not consume 50,000 global budget units.

---

## Ranking: `rank_by_suspicion`

```
suspicion(candidate) =
    0.30 × storage_touch_score +
    0.20 × access_gap_score +
    0.20 × cross_chain_interaction_score +
    0.15 × dependency_depth_score +
    0.10 × tool_confidence_score +
    0.05 × prior_violation_proximity
```

**Methodology note:** These weights are initial placeholders, not calibrated constants. Each sub-score is normalized to `[0.0, 1.0]` by its adapter, but the weighting scheme itself has not been validated against real audit data. The weights encode a hypothesis — storage/access modifications are the strongest signal, cross-chain interaction is next, tool confidence is supplementary. This hypothesis must be tested and recalibrated once the harness is operational against known vulnerable contracts. Until then, treat the ranking as a signal to guide exploration, not a precision measurement.

---

## Frontier Ordering

The frontier is a max-priority queue. Its deterministic key is `(suspicion DESC, depth ASC, chain_expansion_count ASC, canonical_state_hash ASC)`. A configurable `max_consecutive_expansions_per_chain` forces selection of the best eligible state from another chain after the limit.

---

## State Deduplication

`record_edge` deduplicates by `(invariant_id, sorted violated_clauses, violation_source, canonical_state_hash, canonical_trace_hash)`. Evidence from a duplicate is merged by artifact hash; it never creates another finding or inflates confirmation strength.

```
canonical_state_hash(state: GlobalState) -> bytes:
    return hash(
        state.snapshot_set_id,
        canonical_base_fingerprints(state.chain_snapshots),
        canonical_state_diffs(state.chain_snapshots),
        canonical_observed_events(
            state.chain_snapshots, state.observation_set_hash
        ),
        canonical_code_diffs(state.chain_snapshots),
        canonical_pending_messages(state.pending_messages),
        state.relay_dataset_hash,
        state.observation_set_hash,
        state.relay_policy_hash,
        state.relay_mode,
        state.actor_policy.id,
        canonical_assumptions(state.assumptions),
        canonical_liveness_obligations(state.liveness_obligations),
    )
```

Opaque handles and action history are excluded because they cannot affect future execution after storage, code, clocks, message lifecycle, policies, and assumptions are captured. `visited_depth` keeps the shortest known route to a future-equivalent state; deterministic frontier ordering selects among equal-depth routes.

---

## Adaptive Branching Cap

```
def adaptive_branching_cap(depth):
    caps = {0: 4, 1: 4, 2: 3, 3: 3, 4: 2, 5: 2, 6: 1, 7: 1}
    return caps.get(depth, 1)
```

---

## Reachability + Execution Combined

The executor distinguishes tool/process completion from EVM execution:

- **`Success + APPLIED`** — the transition completed; record the successor and evaluate the invariant.
- **`Success + REVERTED`** — the call was attempted but produced no successor state. Preserve revert data and `revert_kind` as constraint feedback and do not advance depth. Deterministic EVM out-of-gas is a revert, not a tool timeout.
- **`Timeout | ToolError | Partial`** — inconclusive; preserve diagnostics, discard the atomic child checkpoint, and never treat the branch as unreachable or safe.
