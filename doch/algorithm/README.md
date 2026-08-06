# Algorithm — Deepest Edge Discovery

The core algorithm searches for the deepest edge case that violates a given invariant. "Deepest" means the violation requiring the most specific accumulated constraints — the one that would be invisible to a shallow scan because its preconditions are too narrow.

---

## Search Strategy

The algorithm uses **per-state adaptive beam search**: at each depth, the top N candidates from each parent state are expanded. The beam width starts wide at shallow depths (exploring broadly for promising directions) and narrows at deeper depths (committing to the most suspicious paths).

This is a deliberate choice. Edge cases in smart contracts are constraint-accumulative — each depth adds a dimension (a specific function, a state variable, a timing condition). Pruning globally across all states (classic beam search) would discard paths that look unremarkable at depth 1 but become critical at depth 4. The adaptive width balances exploration against the exponential growth of per-state branching.

| Depth | Beam Width | Rationale |
|---|---|---|
| 0 → 1 | 4 | Broad exploration — which functions are worth pursuing? |
| 1 → 2 | 3 | Narrow toward suspicious interaction chains |
| 2 → 3 | 2 | Focus on two most promising constraint sets |
| 3 → 4 | 1 | Commit to one path, go as deep as constraints allow |

A hard cap on total states (`max_total_states`, default 200) prevents unbounded growth regardless of beam configuration.

---

## Core Structures

### SearchState

A node in the search tree representing a partially-explored attack path.

```
SearchState:
    constraints: list[Constraint]   # accumulated preconditions
    sequence: list[Call]            # call sequence explored so far
    evidence: list[Evidence]        # tool outputs supporting this path
    depth: int                      # current depth in search tree
```

`constraints` are additive — each new depth appends, never replaces. Example accumulation:

```
depth 1: {function: withdraw}
depth 2: {function: withdraw, delegate: attacker}
depth 3: {function: withdraw, delegate: attacker, oracle_staleness: >60s}
depth 4: {function: withdraw, delegate: attacker, oracle_staleness: >60s, fee: 10000}
```

### Constraint

A named condition on contract state or execution context.

```
Constraint:
    kind:   FUNCTION | STATE_VAR | TIMING | EXTERNAL_CALL | ACCESS
    target: str            # function name, state variable, contract address
    value:  Any            # the constrained value or range
    source: str            # which tool produced this constraint
```

Constraints from different tools may overlap. `constraints_are_consistent` checks that a new constraint does not contradict the existing set before attaching it to a `SearchState`.

### Candidate

A ranked result from probing at a given state.

```
Candidate:
    target_function: str
    pre_conditions: list[Constraint]
    suspicion: float          # 0.0–1.0, higher = more likely to hide edge case
    evidence: Evidence        # raw tool output supporting this candidate
    call_sequence: list[Call]
```

### EdgeCase

The final deliverable — a fully specified attack vector.

```
EdgeCase:
    depth: int
    sequence: list[Call]              # ordered calls that trigger the violation
    constraints: list[Constraint]     # exact preconditions required
    evidence: list[Evidence]          # tool-by-tool supporting data
    impact: Impact                    # what breaks and how badly
    independently_confirmed: bool     # verified by a second tool
    confidence: "proven" | "reproduced"
```

---

## Main Loop

```
deepest_edge(target, invariant, max_depth=4):
    frontier = PriorityQueue()
    frontier.push(SearchState.empty(), priority=0)
    visited = set()              # deduplication key
    deepest = None

    while frontier is not empty AND state_budget > 0:
        state = frontier.pop()   # highest suspicion first

        if state.depth >= max_depth:
            continue

        beam_width = adaptive_width(state.depth)

        # PROBE: ask tools for next-step candidates
        candidates = probe(
            target=target,
            invariant=invariant,
            constraints=state.constraints,
            sequence_prefix=state.sequence,
        )

        ranked = rank_by_suspicion(candidates)

        for cand in ranked[:beam_width]:
            # Extract new constraints from this candidate
            extracted = extract_constraints(cand)

            # Skip if constraints contradict accumulated set
            if not constraints_are_consistent(state.constraints, extracted):
                continue

            next_state = SearchState(
                constraints = state.constraints + extracted,
                sequence    = state.sequence + cand.call_sequence,
                evidence    = state.evidence + [cand.evidence],
                depth       = state.depth + 1,
            )

            # Dedup: skip if we have visited this state before
            state_key = hash(next_state)
            if state_key in visited:
                continue
            visited.add(state_key)

            # EXECUTE + VERIFY REACHABILITY in one run
            result = execute_sequence(
                target=target,
                sequence=next_state.sequence,
                constraints=next_state.constraints,
            )

            if not result.reachable:
                continue

            # Check invariant
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
                    independently_confirmed=(
                        confirm_with_second_tool(target, next_state)
                    ),
                    confidence=(
                        "proven"
                        if edge.independently_confirmed
                        else "reproduced"
                    ),
                )

                if deepest is None or edge.depth > deepest.depth:
                    deepest = edge

                # Continue search — deeper edge cases may still exist
                # at higher depths through other branches.

            # Push for further exploration
            frontier.push(next_state, priority=cand.suspicion)

        state_budget -= 1

    return deepest
```

---

## Ranking: `rank_by_suspicion`

Candidates are scored by how likely they are to lead to an invariant violation. The ranking function combines signals from all tools that have run at the current depth:

```
suspicion(candidate) =
    0.35 × storage_touch_score +     # how many critical state vars modified
    0.25 × access_gap_score +        # missing or weak access control on path
    0.20 × dependency_depth_score +  # length of internal call chain
    0.10 × tool_confidence_score +   # how strongly the tool flagged this
    0.10 × prior_violation_proximity # was a related invariant already broken?
```

Weights are initial estimates and should be tuned against real audit data.

---

## Frontier Ordering: Priority Queue

The frontier is a priority queue ordered by suspicion score, not a BFS queue. This means:

- Paths with high suspicion at depth 2 are explored to depth 3 before medium-suspicion paths at depth 1.
- The search is depth-biased toward promising directions rather than exhausting each depth level.
- The first edge case found is likely one of the more dangerous ones, even if not the absolute deepest.

BFS would guarantee finding the shallowest edge case first — but in this domain, shallow edge cases are usually false positives or well-known patterns. The interesting findings are deep, and a priority queue biases the search toward them.

---

## State Deduplication

Two different search paths may converge to the same state. For example:

```
Path A: deposit(100) → setFee(50) → withdraw(50)
Path B: setFee(50) → deposit(100) → withdraw(50)
```

Both reach the same terminal state. Without deduplication, both would be explored independently, wasting tool invocations.

The deduplication key is:

```
hash(
    target.contract_address,
    frozenset(final_state.storage_values),
    tuple(sequence.call_signatures),
)
```

This is a heuristic approximation — exact state equality is infeasible. False negatives (two states that are actually identical but hash differently) are acceptable and only cause duplicate exploration. False positives (two different states that hash the same) would skip valid exploration paths and must be avoided. The hashing scheme errs on the side of including more information to prevent collisions.

---

## Adaptive Beam Width

```
def adaptive_width(depth):
    widths = {0: 4, 1: 3, 2: 2, 3: 1}
    return widths.get(depth, 1)
```

The rationale: shallow depths have the least information about which direction is promising, so they benefit from broader exploration. As constraints accumulate and the signal becomes clearer, the beam narrows — committing more compute to the most suspicious paths.

---

## Reachability + Execution Combined

Rather than running `verify_reachability` then `execute_sequence` as two separate steps, the harness runs the sequence once and interprets the result:

- **Success + no revert** → reachable, execution complete, check invariant against `after_state`.
- **Revert with known reason** → reachable (the call executed but hit a condition). The revert reason becomes an additional constraint.
- **Revert with unknown reason** → unreachable under current constraints. Discard this branch.
- **Out-of-gas / timeout** → ambiguous. Retry once with higher gas limit; if still fails, treat as unreachable.

This halves the number of tool invocations per candidate.
