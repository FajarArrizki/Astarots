# Algorithm — Cross-Chain Deepest Edge Discovery

The core algorithm searches for the deepest edge case that violates a cross-chain invariant — starting from **forked mainnet state** at pinned blocks, not from an empty deployment. "Deepest" means the violation requiring the most specific combination of existing mainnet state + probe-generated call sequences — the edge case that emerges from 5 years of accumulated protocol state interacting with a specific transaction pattern.

The algorithm uses a **unified frontier** across all chains. Each search state is an immutable, branch-local snapshot of both chains. The frontier expands states in causal order: source-chain steps that emit messages precede destination-chain steps that consume them. There is no separate per-chain search followed by Cartesian recombination — that approach can combine states from causally impossible branches.

---

## Global Search State (Branch-Local)

Each node in the search tree carries an immutable copy of the global state — not a shared mutable reference. This prevents branches from interfering with each other:

```
GlobalState:
    chain_snapshots: Map[ChainId, ForkSnapshot]   # per-chain fork state
    pending_messages: OrderedQueue[Message]       # emitted, not yet delivered
    trace: list[CrossChainStep]                   # ordered causal execution trace
    assumptions: list[Assumption]                 # scope assumptions in effect
    budget_used: int                              # states consumed so far

ForkSnapshot:
    chain_id: ChainId
    base_block: int                        # pinned mainnet block
    base_block_hash: str                   # canonical hash at pinned block
    state_root: str                        # state root at pinned block
    backend_handle: str                    # opaque handle (Echidna session, Foundry fork id)
    overlay_id: int                        # snapshot/revert checkpoint within this branch
    state_diff: list[SlotChange]           # only slots TOUCHED during this branch's execution
    touched_slots_manifest: list[str]      # slot keys touched (for cross-tool projection)
    emitted_logs: list[Event]              # logs emitted during this branch
    block_number_delta: int                # blocks advanced from base
    timestamp_delta: int                   # timestamp advanced from base

SlotChange:
    contract: str                          # contract address
    slot: str                              # storage slot key
    old_value: bytes32                     # value at base (lazy-loaded from RPC on first access)
    new_value: bytes32                     # value after branch execution

Mainnet RPCs do not support enumerating all contract storage. Echidna and Foundry fetch slots lazily on first access. Proxy contracts, mappings, dynamic arrays, and transitive calls make full-storage copies infeasible. Branch isolation uses EVM snapshot/revert at the backend level — each branch checkpoints its backend handle and applies a state diff. Cross-tool handoff uses deterministic `ActionTrace` + `BaseForkFingerprint` so each tool can replay the same trace from the same base, rather than sharing internal snapshot objects.
```

When a branch expands, it checkpoints the backend (EVM snapshot/revert or copy-on-write overlay) and applies the new step. Only the state diff and touched slots are tracked — storage is fetched lazily from the RPC base on first access, never fully enumerated. Branches are isolated — a message delivered in one branch does not affect another.

---

## Unified Frontier

The frontier is a single priority queue holding `SearchState` nodes. Each node belongs to exactly one chain context (the chain where the next action executes), but the global state spans all chains. The frontier naturally respects causal order: source-chain states that emit messages are expanded before the destination-chain states that consume them, because the message coordinator enforces `Delivered` status before destination execution.

```
unified_search(targets, invariant, fork_blocks, max_depth=4, budget=200):
    # targets = {chain_id: contract_address}  — mainnet addresses
    # fork_blocks = {chain_id: block_number} — pinned mainnet blocks
    frontier = PriorityQueue()
    # Priority: higher suspicion first. Tie-breaking: shallower depth.
    initial = SearchState(
        global_state=GlobalState.from_forks(targets, fork_blocks),
        chain_context="ethereum",    # start on source chain
        depth=0,
    )
    frontier.push(initial, priority=0)
    visited = set()
    deepest = None
    all_witnesses = []
    state_budget = budget

    # STEP 0: Evaluate baseline invariant at the forked state.
    # The invariant may already be violated at the fork block due to:
    #   - cross-chain snapshot asynchrony
    #   - in-flight messages not yet delivered
    #   - incomplete relay dataset
    #   - genuine historical state inconsistency
    baseline = evaluate_baseline(
        global_state=GlobalState.from_forks(targets, fork_blocks),
        invariant=invariant,
    )
    # baseline.status: holds | violated | unobservable | inconclusive

    while frontier is not empty AND state_budget > 0:
        state = frontier.pop()

        if state.depth >= max_depth:
            continue

        beam_width = adaptive_width(state.depth)
        chain = state.chain_context

        candidates = probe(
            target=targets[chain],
            invariant=invariant.sub_invariant(chain),
            constraints=state.constraints,
            global_state=state.global_state,
            chain=chain,
        )

        ranked = rank_by_suspicion(candidates)

        # Note: at depth 0, extract_constraints reads EXISTING state values
        # from the forked mainnet snapshot — real guardian sets, real pending
        # messages, real token balances accumulated over years of operation.
        # These become the seed constraints that guide subsequent probing.

        for cand in ranked[:beam_width]:
            extracted = extract_constraints(cand)

            if not constraints_are_consistent(state.constraints, extracted):
                continue

            # Apply step to a COPY of the global state
            next_global = state.global_state.copy()
            result = apply_step(
                global_state=next_global,
                step=cand,
                chain=chain,
            )

            if result.outcome == "inconclusive":
                # Timeout or out-of-gas during execution.
                # Record as witness but mark as inconclusive —
                # we don't know if this path is reachable.
                all_witnesses.append(WitnessState(
                    snapshot=next_global,
                    correlation_value=extract_correlation(cand, invariant),
                    chain=chain,
                    status="inconclusive",
                ))
                continue

            if not result.reachable:
                continue

            # Record witness with snapshot and correlation value
            witness = WitnessState(
                snapshot=next_global,
                correlation_value=extract_correlation(cand, invariant),
                chain=chain,
                call_sequence=state.sequence + cand.call_sequence,
                constraints=state.constraints + extracted,
                evidence=state.evidence + [cand.evidence],
                status="reachable",
            )

            state_key = canonical_hash(witness)
            if state_key in visited:
                continue
            visited.add(state_key)

            all_witnesses.append(witness)

            # Check invariant against this witness
            invariant_broken = not invariant.check(
                witness.snapshot,
            )

            if invariant_broken:
                confirmed = confirm_with_second_tool(
                    target, witness, chain
                )
                edge = EdgeCase(
                    depth=state.depth + 1,
                    witnesses=[witness],
                    independently_confirmed=confirmed,
                    evidence_strength=(
                        "symbolically-confirmed" if confirmed
                        else "observed"
                    ),
                )

                if deepest is None or edge.depth > deepest.depth:
                    deepest = edge

            # Determine next chain context
            next_chain = coordinator.next_chain(
                current=chain,
                result=result,
                invariant=invariant,
            )

            next_state = SearchState(
                global_state=next_global,
                chain_context=next_chain or chain,
                constraints=state.constraints + extracted,
                sequence=state.sequence + cand.call_sequence,
                evidence=state.evidence + [cand.evidence],
                depth=state.depth + 1,
            )

            frontier.push(next_state, priority=cand.suspicion)

        state_budget -= 1

    return SearchResult(
        baseline=baseline,
        witnesses=all_witnesses,
        deepest_edge=deepest,
        exhausted=(state_budget <= 0),
        outcome=(
            "violated" if deepest else
            "inconclusive" if state_budget <= 0 else
            "not_observed"
        ),
    )
```

---

## Core Structures

### SearchState

```
SearchState:
    global_state: GlobalState          # immutable branch-local snapshot
    chain_context: ChainId             # which chain the NEXT action targets
    constraints: list[Constraint]      # accumulated preconditions
    sequence: list[Call]               # call sequence explored so far
    evidence: list[Evidence]           # tool outputs supporting this path
    depth: int                         # current depth in search tree
    branch_id: str                     # unique lineage identifier for causal pairing
```

### WitnessState

A recorded intermediate state with its correlation value. The search retains witnesses even when the local invariant does not break — two witnesses from different chains that share a correlation value may together violate the cross-chain property:

```
WitnessState:
    snapshot: GlobalState              # full chain state at this point
    correlation_value: bytes32         # extracted via CorrelationExtractor
    chain: ChainId
    branch_id: str                     # causal lineage — only witnesses on same lineage pair
    call_sequence: list[Call]
    constraints: list[Constraint]
    evidence: list[Evidence]
    status: "reachable" | "inconclusive"
```

### EdgeCase

```
EdgeCase:
    depth: int
    witnesses: list[WitnessState]      # one per chain involved
    independently_confirmed: bool
    evidence_strength: "observed" | "replayed" | "symbolically-confirmed" | "symbolically-confirmed-under-projected-state"
    violation_source: "pre_existing_at_snapshot" | "introduced_by_trace" | "amplified_by_trace" | "inconclusive_due_to_missing_relay_data"
    impact: Impact
    chains: list[ChainId]
```

### SearchResult

```
SearchResult:
    witnesses: list[WitnessState]      # all recorded witnesses across chains
    deepest_edge: Optional[EdgeCase]   # deepest violation found, if any
    baseline: BaselineResult           # invariant status at fork block before any probing
    exhausted: bool
    outcome: "violated" | "not_observed" | "inconclusive"

BaselineResult:
    status: "holds" | "violated" | "unobservable" | "inconclusive"
    violation_kind: "pre_existing_at_snapshot" | "none"
    reason: Optional[str]              # e.g. "cross-chain snapshot async — 3 messages in-flight"
```

### Constraint

```
Constraint:
    kind:   FUNCTION | STATE_VAR | TIMING | EXTERNAL_CALL | ACCESS | CROSS_CHAIN
    target: str            # function name, state variable, contract address
    value:  Any            # the constrained value or range
    chain:  ChainId        # which chain this constraint applies to
    source: str            # which tool produced this constraint
```

### Candidate

```
Candidate:
    target_function: str
    call_sequence: list[Call]
    pre_conditions: list[Constraint]
    suspicion: float          # 0.0–1.0
    evidence: Evidence
    chain: ChainId
```

---

## Cross-Chain Witness Correlation

Cross-chain violations are evaluated against a **single** branch-local `GlobalState.snapshot`, not by pairing witnesses from different branches. Every witness carries a `snapshot` containing the full state of all chains at that point in the causal trace. The invariant is checked directly against that snapshot:

```
for witness in reachable_witnesses:
    if invariant.observation_policy.ready(witness.snapshot):
        if not invariant.check(witness.snapshot):
            violations.append(witness)
```

Two witnesses that share a correlation value but come from **different** branches cannot be paired — they represent causally divergent paths. Only witnesses on the **same causal lineage** (shared `branch_id` and ancestor relation) can be combined. If cross-chain violations must be detected from paired witnesses, each `SearchState` records:

```
SearchState:
    branch_id: str                     # unique lineage identifier
    parent_branch_id: Optional[str]    # for ancestor tracking
```

Witnesses with the same `branch_id` share a causal trace and can be paired safely. Witnesses from different branches never pair.

---

## Search Strategy

The algorithm uses **per-state adaptive beam search**: at each depth, the top N candidates from each parent state are expanded.

| Depth | Beam Width | Rationale |
|---|---|---|
| 0 → 1 | 4 | Broad exploration |
| 1 → 2 | 3 | Narrow toward suspicious paths |
| 2 → 3 | 2 | Focus on best constraint sets |
| 3 → 4 | 1 | Commit to deepest exploration |

**Depth note:** Depth 4 is a reasonable default for most bridge invariants (quorum boundary, replay, guardian consistency), but complex protocols with long dependency chains may need deeper search. Realistic cross-chain attack sequences (governance proposal → timelock → execution → relay → destination action) can span 6+ steps per chain. Use `--max-depth` to increase this for protocols with deep governance or multi-stage message pipelines.

A hard cap on total states (`budget`, default 200) across all chains prevents unbounded growth.

**Budget semantics:** One "state" in the beam search is one node expansion (one probe + execute cycle per candidate). This is distinct from tool-internal test limits — `echidna.test_limit = 50000` means Echidna runs up to 50,000 call sequences per *single* probe invocation, not per beam-search state. The total compute cost is `state_budget × tool_test_limit`, which can be substantial. Budget, tool limits, and timeouts should be tuned together based on the complexity of the target protocol.

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

**Methodology note:** These weights are initial placeholders, not calibrated constants. Each sub-score is normalized to `[0.0, 1.0]` by its adapter, but the weighting scheme itself has not been validated against real audit data. The weights encode a hypothesis — storage/access modifications are the strongest signal, cross-chain interaction is next, tool confidence is supplementary. This hypothesis must be tested and recalibrated once the harness is operational against known vulnerable contracts. Until then, treat the ranking as a signal to guide exploration, not a precision measurement.
```

---

## Frontier Ordering

Priority queue by suspicion. Tie-breaking: shallower depth first.

**Fairness concern:** The unified frontier orders states globally by suspicion score, regardless of chain. If one chain's states systematically score higher (e.g., the destination chain where more storage variables are modified per step), the search may starve the other chain. No per-chain allocation or round-robin policy is enforced at this stage. This is a known design trade-off: the unified frontier prioritizes the most suspicious paths overall, sacrificing chain-level fairness for search efficiency. If starvation becomes observable in practice, a per-chain quota or alternating schedule can be introduced without changing the frontier data structure.

---

## State Deduplication

```
canonical_hash(witness: WitnessState) -> bytes:
    return hash(
        witness.chain,
        canonical_form(witness.snapshot.chain_snapshots),
        tuple(witness.call_sequence),
        witness.correlation_value,
    )
```

---

## Adaptive Beam Width

```
def adaptive_width(depth):
    widths = {0: 4, 1: 3, 2: 2, 3: 1}
    return widths.get(depth, 1)
```

---

## Reachability + Execution Combined

The harness runs the sequence once and interprets the result:

- **Success + no revert** → reachable, record witness, check invariant.
- **Revert with known reason** → reachable. Revert reason becomes additional constraint.
- **Revert with unknown reason** → unreachable under current constraints. Discard.
- **Out-of-gas / timeout** → **inconclusive** (not unreachable). Record witness with `status="inconclusive"`. The search continues but this branch cannot confirm or refute the invariant.
