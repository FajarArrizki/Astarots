# Architecture

Astarots is a **cross-chain invariant testing harness**. It takes invariants that span multiple chains — bridge balance equality, message replay protection, guardian quorum thresholds — and discovers edge cases through guided search across multiple analysis tools.

The core idea: invariants are checked against **forked mainnet state** at pinned blocks, not against freshly deployed contracts. The Wormhole protocol has operated for 5+ years — its code is battle-tested, but the accumulated state (millions of transactions, dozens of guardian rotations, thousands of cross-chain messages in various states) may harbor edge cases invisible to code-level analysis. The harness forks mainnet at a specific block per chain, probes from that state, and discovers attack vectors that emerge from specific state + specific transaction sequences.

---

## Data Flow

```
                      ┌──────────────────────────┐
                      │    test/*.t.sol            │
                      │    Cross-chain invariants   │
                      │    @crosschain src=eth      │
                      │              dst=poly       │
                      └────────────┬───────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│                          Harness                                   │
│                                                                    │
│  ┌───────────────┐   ┌──────────────────┐   ┌──────────────────┐  │
│  │   Invariant   │──▶│  Cross-Chain     │──▶│  Chain Registry  │  │
│  │   Loader      │   │  Decomposer      │   │  (eth@18.5M,      │  │
│  │               │   │                  │   │   poly@49.8M, ...)│  │
│  └───────────────┘   └────────┬─────────┘   └──────────────────┘  │
│                               │                                    │
│                               ▼                                    │
│                    ┌──────────────────────┐                        │
│                    │  Message Coordinator │                        │
│                    │  - lifecycle tracking│                        │
│                    │  - causal ordering   │                        │
│                    └──────────┬───────────┘                        │
│                               │                                    │
│         ┌─────────────────────┼─────────────────────┐             │
│         ▼                     ▼                     ▼             │
│  ┌────────────┐       ┌────────────┐       ┌────────────┐        │
│  │ Per-Chain  │       │ Per-Chain  │       │ Per-Chain  │        │
│  │ Scheduler  │       │ Scheduler  │       │ Scheduler  │        │
│  │ (eth)      │       │ (poly)     │       │ (arb)      │        │
│  └─────┬──────┘       └─────┬──────┘       └─────┬──────┘        │
│        │                    │                    │                │
│        ▼                    ▼                    ▼                │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │               Search Engine (shared)                      │    │
│  │  - beam search    - state dedup    - priority Q           │    │
│  │  - constraint consistency    - reachability check         │    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │                                     │
│                             ▼                                     │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │               Adapter Registry                             │    │
│  │  echidna ─── halmos ─── slither ─── ...                   │    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │                                     │
│                             ▼                                     │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │               Cross-Chain Recombiner                       │    │
│  │  - merge per-chain findings                                │    │
│  │  - verify cross-chain invariant holds                      │    │
│  │  - detect multi-chain attack vectors                       │    │
│  └──────────────────────────┬───────────────────────────────┘    │
│                             │                                     │
│                             ▼                                     │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │               Report Engine                                │    │
│  │  - console (per-chain trace)    - JSON                     │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Component Roles

### Invariant Loader

Parses `.t.sol` test files and extracts cross-chain invariant function signatures from NatSpec tags. Produces a normalized `CrossChainInvariant` struct. The loader validates that the NatSpec `@quantify` predicate matches the Solidity `assert()` expression — if the two representations diverge, the loader emits a hard error. This prevents the authoritative IR from drifting from the human-readable Solidity rendering.

### Predicate Expression Engine

Evaluates `QuantifiedPredicate.predicate` strings at runtime. The predicate is a mini expression language over bound variables and chain snapshots. It supports equality (`==`, `!=`), inequality (`<`, `>`, `<=`, `>=`), and logical operators (`&&`, `||`, `!`) over integer variables and array lookups. The engine resolves variable references through the invariant's `Binding` declarations and extracts values from `GlobalState.chain_snapshots`.

This component is necessary because `predicate: str` like `"locked == minted"` must be parsed and evaluated against actual chain state during the search. The expression engine is a bounded evaluator — it does not support arbitrary Solidity expressions, only the subset needed for cross-chain predicate checking. It is implemented as a standalone component, not embedded in the invariant loader or scheduler.

### Cross-Chain Decomposer

Breaks a cross-chain invariant into per-chain sub-invariants using the IR metadata. The decomposer reads `TransitionPredicate` declarations to know which state variables change through which functions, `CorrelationExtractor` to pair source/destination events, `Binding` to map variables across chains, and `QuantifiedPredicate` to express the property. It does **not** invent deposit/burn/refund rules — those come from `@transition` tags in the invariant file.

For each chain, the decomposer generates assertions:
- State variable `V` only changes through the functions listed in its `TransitionPredicate`.
- If a probe finds a path where `V` changes through an undeclared function, that is a violation of the local sub-invariant.

The cross-chain check uses `CorrelationExtractor` to pair events and evaluates the `QuantifiedPredicate` against the bound variables.

The decomposer validates that the invariant IR is complete — missing correlation key or observation policy is a hard error, not a silent default.

### Chain Registry

Manages per-chain configuration: RPC endpoints, **pinned fork block numbers**, mainnet contract addresses, and chain-specific tool settings. Each chain is registered with a unique alias and a fork block — the harness never deploys fresh contracts. It forks mainnet at the specified block and operates on the live state accumulated over years of protocol operation. Fork blocks must be archive-node accessible (historical state queries required).

### Cross-Chain Message Coordinator

Cross-chain execution is **causal**: the destination chain's state depends on messages emitted by the source chain. The coordinator manages this dependency through an explicit message lifecycle:

```
Emitted → SourceFinalized → RelayEligible → Delivered → Consumed | Rejected | Expired
```

The coordinator tracks pending messages and determines eligibility based on finality depth, ordering guarantees, and epoch boundaries. It models delay, duplicate delivery, replay, reorg depth, block timestamps, and guardian-set versioning as first-class constraints.

Static analysis (Slither) and initial local probing (Echidna corpus generation) run independently on each chain — they do not require message delivery. Once a source-chain probe emits a cross-chain event, the coordinator injects the corresponding message into the destination chain before the destination probe executes. Execution is causally ordered, not independent.

### Per-Chain Scheduler

One scheduler instance per chain. Each runs beam search on its assigned sub-invariant starting from the **forked mainnet state** at the configured block. The scheduler does not deploy contracts — it connects to the fork and probes from the existing state. Schedulers operate concurrently for independent work (static analysis, initial probing) and serialize through the coordinator when causal dependencies exist. Per-chain schedulers produce `SearchResult` structs containing both candidates and local findings — not just violations.

### Search Engine

The core algorithm shared across all per-chain schedulers. Houses `SearchState`, `SearchResult`, the priority queue frontier, constraint deduplication, and the main search loop. The search engine is chain-agnostic — it receives a chain context from the scheduler and passes it through to adapters.

### Adapter Registry

Maps tool names to adapter implementations. Each adapter exposes its capabilities (`ToolCapabilities`) so the scheduler can select the right tool for each probe type. The registry routes per-chain probe requests to the appropriate adapter with chain-specific configuration.

### Cross-Chain Recombiner

After all per-chain searches complete, the recombiner merges `SearchResult` structs and checks the original cross-chain invariant. It correlates per-chain candidates by the `correlation_key`, evaluates the cross-chain predicate, and produces `CrossChainEdgeCase` findings. A source-chain candidate with a valid-but-suspicious state combined with a destination-chain candidate may violate the cross-chain property even if neither chain's local invariant broke — this is where the deepest cross-chain edge cases emerge.

### Report Engine

Renders findings with per-chain trace annotations. Each call in a cross-chain attack sequence is labeled with the chain it executes on and the tool that discovered it. Output formats: console (for development) and JSON (for CI and programmatic consumption).

---

## Key Design Decisions

**Cross-chain is the primary mode.** The harness is designed around multi-chain invariants from the ground up. Single-chain invariants are supported as a degenerate case and will be elevated to first-class in a future milestone.

**Decomposition is guided by developer-provided metadata, not invention.** The invariant IR requires the developer to specify correlation key and observation policy. The decomposer uses these to produce sub-invariants; it does not guess. Missing metadata is a hard error.

**Message lifecycle is an explicit model.** Delay, ordering, duplicate delivery, replay, finality depth, reorg assumptions, and guardian-set epochs are all modeled as constraints. The harness checks against this model to distinguish bugs from valid transient states.

**Per-chain schedulers are causally coordinated through the message lifecycle.** Static analysis and initial probing run independently. Once cross-chain messages are emitted, the coordinator serializes the causal dependency. Unrelated probes remain parallelizable.

**Adapters expose capabilities, not just a uniform interface.** The adapter protocol includes a `ToolCapabilities` declaration so the scheduler knows which tool can handle which probe type. Slither cannot execute; Echidna cannot symbolically prove. The scheduler routes accordingly.

**The harness operates on forked mainnet state, not fresh deploys.** All execution starts from a pinned mainnet block. The contracts are already deployed at known addresses with years of accumulated state. Constraint extraction reads real storage values, real guardian sets, real pending message queues. The search starts from the actual state of the protocol — not from an empty deployment. Fresh deploys are only used for unit-testing the harness itself, never for production probe runs.

**The harness uses deterministic twin-state for replay, not vm.mockCall.** Cross-chain replay uses separate EVM state databases or Foundry multi-fork with pinned blocks, not string-based mock calls. Since the starting state is already a fork, replay is a fork-of-a-fork — fully deterministic and reproducible.
