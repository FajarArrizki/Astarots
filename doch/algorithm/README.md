# Algorithm — Cross-Chain Deepest Edge Discovery

The core algorithm searches for the deepest edge case that violates a cross-chain invariant. "Deepest" means the violation requiring the most specific accumulated constraints across multiple chains — the one that would be invisible to any single-chain analysis.

The algorithm has two layers: an **outer decomposition loop** that splits the cross-chain invariant into per-chain sub-invariants, and an **inner beam search** that probes each chain independently.

---

## Outer Loop: Cross-Chain Decomposition

```
find_crosschain_edge(targets, invariant, max_depth=4):
    # targets = {src_chain: Contract, dst_chain: Contract, ...}

    # STEP 1: Decompose the cross-chain invariant
    sub_invariants = decompose(invariant, targets)
    # Example: "locked_src == minted_dst"
    #   → sub_src: locked_src decreases only on verified burn
    #   → sub_dst: minted_dst increases only on verified lock
    #   → cross_check: locked_src(event_i) == minted_dst(event_i)

    # STEP 2: Run per-chain beam search in parallel
    per_chain_edges = {}
    for chain, sub_inv in sub_invariants.items():
        per_chain_edges[chain] = deepest_edge(
            target=targets[chain],
            invariant=sub_inv,
            chain=chain,
            max_depth=max_depth,
        )

    # STEP 3: Recombine — check cross-chain property
    cross_edges = recombine(
        invariant=invariant.cross_check,
        per_chain_edges=per_chain_edges,
        targets=targets,
    )

    return cross_edges
```

The decomposer is invariant-type-aware. Different cross-chain invariant patterns decompose differently:

| Cross-Chain Invariant Pattern | Decomposition |
|---|---|
| **Balance equality** — `locked_src == minted_dst` | Per-chain: conservation of locked/minted. Cross-check: pair-wise event equality |
| **Quorum threshold** — `valid_signatures >= threshold` | Per-chain: signature verification logic. Cross-check: signer set consistent across chains |
| **Message replay** — `sequence[msg] unique per (src,dst)` | Per-chain: sequence monotonicity. Cross-check: no (src,dst,seq) duplicate across chains |
| **Access control** — `only guardian_set can authorize` | Per-chain: access control per entry point. Cross-check: guardian set identical across chains |

---

## Inner Loop: Per-Chain Beam Search

The per-chain search is identical to the single-chain algorithm but receives a `chain` context. The chain context is passed through to adapters so tools operate on the correct deployment.

### SearchState (Cross-Chain Extension)

```
SearchState:
    constraints: list[Constraint]   # accumulated preconditions
    sequence: list[Call]            # call sequence explored so far
    evidence: list[Evidence]        # tool outputs supporting this path
    depth: int                      # current depth in search tree
    chain: str                      # which chain this search operates on
```

`constraints` are additive — each new depth appends, never replaces. Cross-chain constraints can reference state from the other chain through the mock relayer:

```
depth 1: {function: submit_message}
depth 2: {function: submit_message, guardian_count: 12}         # 1 short of quorum
depth 3: {function: submit_message, guardian_count: 12, rotation: pending}
depth 4: {function: submit_message, guardian_count: 12, rotation: pending, sig_from_old_set: 7}
```

### Search Strategy

The algorithm uses **per-state adaptive beam search**: at each depth, the top N candidates from each parent state are expanded. The beam width starts wide at shallow depths and narrows at deeper depths.

| Depth | Beam Width | Rationale |
|---|---|---|
| 0 → 1 | 4 | Broad exploration — which entry points interact with cross-chain state? |
| 1 → 2 | 3 | Narrow toward suspicious cross-chain interaction chains |
| 2 → 3 | 2 | Focus on two most promising constraint sets |
| 3 → 4 | 1 | Commit to one path, go as deep as constraints allow |

A hard cap on total states (`max_total_states`, default 200) per chain prevents unbounded growth.

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

The `CROSS_CHAIN` constraint kind holds conditions that depend on the other chain's state — for example, "the source chain emitted a `TokensLocked` event with `amount = X`." These constraints are resolved by the harness' mock relayer, not by individual tools.

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

### Main Loop

```
deepest_edge(target, invariant, chain, max_depth=4):
    frontier = PriorityQueue()
    frontier.push(SearchState.empty(chain=chain), priority=0)
    visited = set()
    deepest = None

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

            state_key = hash(next_state)
            if state_key in visited:
                continue
            visited.add(state_key)

            result = execute_sequence(
                target=target,
                sequence=next_state.sequence,
                constraints=next_state.constraints,
                chain=chain,
            )

            if not result.reachable:
                continue

            invariant_broken = not invariant.check(
                result.before_state,
                result.after_state,
            )

            if invariant_broken:
                edge = EdgeCase(
                    depth=next_state.depth,
                    sequence=next_state.sequence,
                    constraints=next_state.constraints,
                    evidence=next_state.evidence,
                    impact=result.impact,
                    chains=[chain],
                    independently_confirmed=(
                        confirm_with_second_tool(target, next_state, chain)
                    ),
                    confidence=(
                        "proven"
                        if edge.independently_confirmed
                        else "reproduced"
                    ),
                )

                if deepest is None or edge.depth > deepest.depth:
                    deepest = edge

            frontier.push(next_state, priority=cand.suspicion)

        state_budget -= 1

    return deepest
```

---

## Cross-Chain Recombination

After per-chain searches complete, the recombiner checks the full cross-chain invariant:

```
recombine(invariant, per_chain_edges, targets):
    cross_edges = []

    # Pair up per-chain findings by event/message correlation
    for src_edge in per_chain_edges[src_chain]:
        for dst_edge in per_chain_edges[dst_chain]:
            # Do they share a cross-chain event?
            if correlated(src_edge, dst_edge):
                # Does the combination violate the cross-chain invariant?
                combined = CrossChainEdgeCase(
                    chains=[src_chain, dst_chain],
                    sequence = src_edge.sequence + dst_edge.sequence,
                    constraints = src_edge.constraints + dst_edge.constraints,
                    cross_violation = not invariant.cross_check(
                        src_edge.effect,
                        dst_edge.effect,
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

---

## Frontier Ordering, Deduplication, Reachability

These mechanisms are identical to the single-chain case: priority queue by suspicion, state deduplication by hash, and combined reachability-check + execution in one tool invocation. The only addition is that the `chain` field is included in the deduplication hash, so identical states on different chains are not treated as duplicates.

---

## Adaptive Beam Width

```
def adaptive_width(depth):
    widths = {0: 4, 1: 3, 2: 2, 3: 1}
    return widths.get(depth, 1)
```

Shallow depths explore broadly; as constraints accumulate, the beam narrows toward the most suspicious paths.
