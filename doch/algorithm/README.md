# Algorithm — Cross-Chain Deepest Edge Discovery

The core algorithm searches for the deepest edge case that violates a cross-chain invariant. "Deepest" means the violation requiring the most specific accumulated constraints across multiple chains — the one that would be invisible to any single-chain analysis.

The algorithm has two layers: an **outer decomposition loop** that splits the cross-chain invariant into per-chain sub-invariants, and an **inner beam search** that probes each chain independently. The outer loop uses a **message coordinator** to manage causal dependencies between chains.

---

## Global Search State

A cross-chain search operates on a shared global state that tracks both chains' snapshots and in-flight messages:

```
GlobalState:
    chain_snapshots: Map[ChainId, Snapshot]   # per-chain storage state
    pending_messages: OrderedQueue[Message]   # emitted, not yet delivered
    trace: list[CrossChainStep]               # ordered execution trace
    assumptions: list[Assumption]             # scope assumptions in effect
    budget_used: int                          # states consumed so far
```

The global state is the single source of truth for what has happened across both chains. It serializes the causal order: source-chain steps that emit messages appear before the destination-chain steps that consume them.

---

## Outer Loop: Cross-Chain Decomposition

```
find_crosschain_edge(targets, invariant, max_depth=4, budget=200):
    # targets = {src_chain: Contract, dst_chain: Contract, ...}

    # STEP 1: Decompose the cross-chain invariant
    sub_invariants = decompose(invariant, targets)
    # Uses invariant IR metadata:
    #   correlation_key: messageHash
    #   observation_policy: AFTER_ALL_DELIVERED
    #   assumptions: guardian_honesty(at_most_6_malicious)

    # STEP 2: Run per-chain beam search with causal coordination
    per_chain_results: Map[ChainId, SearchResult] = {}
    coordinator = MessageCoordinator(invariant.assumptions)

    for chain, sub_inv in sub_invariants.items():
        per_chain_results[chain] = deepest_edge(
            target=targets[chain],
            invariant=sub_inv,
            chain=chain,
            max_depth=max_depth,
            budget=budget,
            coordinator=coordinator,
        )

    # STEP 3: Recombine — merge per-chain SearchResults
    cross_edges = recombine(
        invariant=invariant,
        per_chain_results=per_chain_results,
        targets=targets,
    )

    return cross_edges
```

The decomposer is guided by the invariant IR's metadata. It does **not** invent semantics — it uses the developer-provided correlation key, observation policy, and assumptions. Missing metadata is a hard error.

---

## Inner Loop: Per-Chain Beam Search

The per-chain search receives a `chain` context and access to the global coordinator. Constraints that reference the other chain's state are resolved through the coordinator's message lifecycle.

### SearchState (Cross-Chain Extension)

```
SearchState:
    constraints: list[Constraint]   # accumulated preconditions
    sequence: list[Call]            # call sequence explored so far
    evidence: list[Evidence]        # tool outputs supporting this path
    depth: int                      # current depth in search tree
    chain: str                      # which chain this search operates on
```

`constraints` are additive — each new depth appends, never replaces. Cross-chain constraints can reference the other chain's state through `CROSS_CHAIN` constraints, resolved by the coordinator.

### Search Strategy

The algorithm uses **per-state adaptive beam search**: at each depth, the top N candidates from each parent state are expanded. This is a branching cap per parent over a persistent best-first frontier — not classic global beam search. Each parent produces up to `beam_width` children.

| Depth | Beam Width | Rationale |
|---|---|---|
| 0 → 1 | 4 | Broad exploration — which entry points interact with cross-chain state? |
| 1 → 2 | 3 | Narrow toward suspicious cross-chain interaction chains |
| 2 → 3 | 2 | Focus on two most promising constraint sets |
| 3 → 4 | 1 | Commit to one path, go as deep as constraints allow |

A hard cap on total states (`budget`, default 200) per chain prevents unbounded growth.

### Core Structures

**Constraint:**

```
Constraint:
    kind:   FUNCTION | STATE_VAR | TIMING | EXTERNAL_CALL | ACCESS | CROSS_CHAIN
    target: str            # function name, state variable, contract address
    value:  Any            # the constrained value or range
    chain:  str            # which chain this constraint applies to
    source: str            # which tool produced this constraint
```

**Candidate:**

```
Candidate:
    target_function: str
    pre_conditions: list[Constraint]
    suspicion: float          # 0.0–1.0
    evidence: Evidence
    call_sequence: list[Call]
    chain: str
```

**EdgeCase:**

```
EdgeCase:
    depth: int
    sequence: list[Call]              # ordered calls that trigger the violation
    constraints: list[Constraint]     # exact preconditions required
    evidence: list[Evidence]          # tool-by-tool supporting data
    impact: Impact                    # what breaks and how badly
    independently_confirmed: bool     # verified by a second tool
    confidence: "proven" | "reproduced"
    chains: list[str]                 # which chains are involved
```

**SearchResult:**

The per-chain scheduler returns a `SearchResult`, not a single `EdgeCase`. The recombiner needs all reachable candidates, not just violations — a state that is valid in isolation may combine with another chain's state to violate the cross-chain property.

```
SearchResult:
    candidates: list[Candidate]       # all reachable candidates explored
    local_findings: list[EdgeCase]    # violations found on this chain
    exhausted: bool                   # true if search hit budget/depth limit
    outcome: Outcome                  # violated | not_observed | inconclusive
```

### Main Loop

```
deepest_edge(target, invariant, chain, max_depth=4, budget=200,
             coordinator=None):
    state_budget = budget
    frontier = PriorityQueue()
    # Priority ordering: higher suspicion first.
    # Tie-breaking: shallower depth first (shallower violations
    # are easier to reproduce and understand).
    frontier.push(SearchState.empty(chain=chain), priority=0)
    visited = set()
    deepest = None
    all_candidates = []
    all_findings = []

    while frontier is not empty AND state_budget > 0:
        state = frontier.pop()

        if state.depth >= max_depth:
            continue

        beam_width = adaptive_width(state.depth)

        candidates = probe(
            target=target,
            invariant=invariant,
            constraints=state.constraints,
            sequence_prefix=state.sequence,
            chain=chain,
            coordinator=coordinator,
        )

        ranked = rank_by_suspicion(candidates)

        for cand in ranked[:beam_width]:
            extracted = extract_constraints(cand)

            if not constraints_are_consistent(state.constraints, extracted):
                continue

            next_state = SearchState(
                constraints = state.constraints + extracted,
                sequence    = state.sequence + cand.call_sequence,
                evidence    = state.evidence + [cand.evidence],
                depth       = state.depth + 1,
                chain       = chain,
            )

            # Canonical state key for deduplication.
            # Minimizes storage values to canonical form before hashing.
            state_key = canonical_hash(next_state)
            if state_key in visited:
                continue
            visited.add(state_key)

            result = execute_sequence(
                target=target,
                sequence=next_state.sequence,
                constraints=next_state.constraints,
                chain=chain,
                coordinator=coordinator,
            )

            if not result.reachable:
                continue

            invariant_broken = not invariant.check(
                result.before_state,
                result.after_state,
            )

            if invariant_broken:
                # Resolve confirmation BEFORE constructing EdgeCase.
                # The original code referenced edge.independently_confirmed
                # while edge was still being constructed (use-before-def).
                confirmed = confirm_with_second_tool(
                    target, next_state, chain
                )
                edge = EdgeCase(
                    depth=next_state.depth,
                    sequence=next_state.sequence,
                    constraints=next_state.constraints,
                    evidence=next_state.evidence,
                    impact=result.impact,
                    chains=[chain],
                    independently_confirmed=confirmed,
                    confidence="proven" if confirmed else "reproduced",
                )

                all_findings.append(edge)
                all_candidates.append(edge)
                if deepest is None or edge.depth > deepest.depth:
                    deepest = edge
            else:
                # Record candidate even if invariant didn't break.
                # Valid intermediate states may still contribute to
                # cross-chain violations at recombination.
                all_candidates.append(cand)

            # Always push — continue exploring even after finding
            # a violation. Deeper edge cases may exist.
            frontier.push(next_state, priority=cand.suspicion)

        state_budget -= 1

    return SearchResult(
        candidates=all_candidates,
        local_findings=all_findings,
        exhausted=(state_budget <= 0),
        outcome=(
            "violated" if deepest else
            "inconclusive" if state_budget <= 0 else
            "not_observed"
        ),
    )
```

---

## Cross-Chain Recombination

After per-chain searches complete, the recombiner checks the full cross-chain invariant. It operates on `SearchResult` structs — pairing per-chain candidates, not just per-chain violations:

```
recombine(invariant, per_chain_results, targets):
    cross_edges = []

    # Pair per-chain candidates by the invariant's correlation_key
    for src_cand in per_chain_results[src_chain].candidates:
        for dst_cand in per_chain_results[dst_chain].candidates:
            if not correlated(src_cand, dst_cand, invariant.correlation_key):
                continue

            combined = CrossChainEdgeCase(
                chains=[src_chain, dst_chain],
                sequence = src_cand.call_sequence + dst_cand.call_sequence,
                constraints = (
                    src_cand.pre_conditions + dst_cand.pre_conditions
                ),
                cross_violation = not invariant.cross_check(
                    src_cand, dst_cand,
                ),
            )

            if combined.cross_violation:
                cross_edges.append(combined)

    return cross_edges
```

A cross-chain edge case is typically deeper than any per-chain finding alone — it emerges from the interaction between two chains, each of which appeared safe in isolation.

---

## Ranking: `rank_by_suspicion` (Cross-Chain Weighting)

The suspicion score for cross-chain probing adds weights for cross-chain-specific signals:

```
suspicion(candidate) =
    0.30 × storage_touch_score +
    0.20 × access_gap_score +
    0.20 × cross_chain_interaction_score +  # touches cross-chain state or events
    0.15 × dependency_depth_score +
    0.10 × tool_confidence_score +
    0.05 × prior_violation_proximity
```

The `cross_chain_interaction_score` increases when a candidate function emits cross-chain events, reads message state, or interacts with the relayer — these are the highest-value targets.

Weights are initial estimates and should be tuned against real audit data.

---

## Frontier Ordering

The frontier is a priority queue ordered by suspicion score. Tie-breaking: shallower depth first (shallower violations are generally easier to reproduce and explain). This means:

- Paths with high suspicion at depth 2 are explored before medium-suspicion paths at depth 1.
- Among equally suspicious states, shallower ones are expanded first.
- The search is depth-biased toward promising directions.

---

## State Deduplication

Two different search paths may converge to the same state. The deduplication key uses a canonical state hash:

```
hash(
    target.contract_address,
    canonical_form(final_state.storage_values),  # sorted, minimized
    tuple(sequence.call_signatures),
    chain,
)
```

The `chain` field prevents identical states on different chains from being treated as duplicates. The `canonical_form` normalizes storage values to prevent semantically identical but structurally different states from evading dedup.

---

## Adaptive Beam Width

```
def adaptive_width(depth):
    widths = {0: 4, 1: 3, 2: 2, 3: 1}
    return widths.get(depth, 1)
```

Shallow depths explore broadly; as constraints accumulate, the beam narrows toward the most suspicious paths.

---

## Reachability + Execution Combined

Rather than running `verify_reachability` then `execute_sequence` as two separate steps, the harness runs the sequence once and interprets the result:

- **Success + no revert** → reachable, execution complete, check invariant against `after_state`.
- **Revert with known reason** → reachable (the call executed but hit a condition). The revert reason becomes an additional constraint.
- **Revert with unknown reason** → unreachable under current constraints. Discard this branch.
- **Out-of-gas / timeout** → ambiguous. Retry once with higher gas limit; if still fails, treat as unreachable.

This halves the number of tool invocations per candidate.
