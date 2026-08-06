# Architecture

Astarots is a **cross-chain invariant testing harness**. It takes invariants that span multiple chains — bridge balance equality, message replay protection, guardian quorum thresholds — decomposes them into per-chain sub-probes, runs guided search through multiple analysis tools, and recombines the results to verify the full cross-chain property.

The core idea: a cross-chain invariant is broken into single-chain assertions, each probed independently by the best tool for that chain's context. The harness then checks that no combination of per-chain violations violates the overall cross-chain property. This decomposition is what makes deep cross-chain edge cases discoverable — no single tool can simulate two chains simultaneously at the depth required.

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
│  │   Loader      │   │  Decomposer      │   │  (eth, poly, ...)│  │
│  └───────────────┘   └────────┬─────────┘   └──────────────────┘  │
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
│  │  - console (per-chain trace)    - JSON    - HTML           │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Component Roles

### Invariant Loader

Parses `.t.sol` test files and extracts cross-chain invariant function signatures. Identifies the `@crosschain` NatSpec tag specifying source and destination chains. Produces a normalized `CrossChainInvariant` struct containing per-chain contract references and the cross-chain assertion. Also supports single-chain invariants for completeness, but they are treated as a degenerate case (one chain, trivial recomposition).

### Cross-Chain Decomposer

Breaks a cross-chain invariant into per-chain sub-invariants. For a bridge invariant like `locked_src == minted_dst`, the decomposer produces:

- **Sub-invariant for source chain:** `locked_src` never decreases without a corresponding message event.
- **Sub-invariant for destination chain:** `minted_dst` never increases without a corresponding message event.
- **Cross-chain check (Python):** for every message event pair, `locked_src(event) == minted_dst(event)`.

Each sub-invariant is a standard single-chain assertion that adapters can handle natively. The decomposer also determines which tool-adapter combinations are most effective for each sub-invariant based on the property type.

### Chain Registry

Manages per-chain configuration: RPC endpoints, contract deployments, mock relayer setup, and chain-specific tool settings. Each chain is registered with a unique alias (`eth`, `poly`) used throughout the harness. The chain registry also handles mock cross-chain communication — simulating message passing between chains for tools that execute concrete sequences.

### Per-Chain Scheduler

One scheduler instance per chain. Each runs the beam search independently on its assigned sub-invariant, using the shared search engine. Schedulers can run in parallel — they operate on separate chains and their search states are independent. The per-chain schedulers produce per-chain `EdgeCase` lists.

### Search Engine

The core algorithm shared across all per-chain schedulers. Houses `SearchState`, `EdgeCase`, the priority queue frontier, constraint deduplication, and the main `deepest_edge()` loop. The search engine is chain-agnostic — it receives a chain context from the scheduler and passes it through to adapters.

### Adapter Registry

Maps tool names to adapter implementations. Each adapter conforms to a shared protocol. The registry routes per-chain probe requests to the appropriate adapter, passing chain-specific configuration. Adapters for cross-chain-relevant tools are prioritized: Echidna for concrete sequence fuzzing, Halmos for formal verification of threshold logic, Slither for static detection of missing access control on cross-chain entry points.

### Cross-Chain Recombiner

After all per-chain searches complete, the recombiner merges findings and checks the original cross-chain invariant. It correlates per-chain `EdgeCase` lists: a source-chain finding about `locked` state combined with a destination-chain finding about `minted` state may together violate the cross-chain property even if neither chain's sub-invariant broke in isolation. This is where the deepest cross-chain edge cases emerge — they are invisible to per-chain probing but detectable at the recombination layer.

### Report Engine

Renders findings with per-chain trace annotations. Each call in a cross-chain attack sequence is labeled with the chain it executes on and the tool that discovered it. The report distinguishes between per-chain findings and true cross-chain violations.

---

## Key Design Decisions

**Cross-chain is the primary mode.** The harness is designed around multi-chain invariants from the ground up. Single-chain invariants are supported as a degenerate case (one chain, identity recomposition) and will be elevated to first-class in a future milestone.

**Decomposition happens at the invariant level, not the tool level.** Tools never see "two chains." They see one chain with a sub-invariant. The recombiner handles the multi-chain property. This means all existing single-chain tools work without modification — no tool needs to understand cross-chain semantics.

**Per-chain schedulers are independent and parallelizable.** The source chain search and destination chain search share no state. They run concurrently, each benefiting from the full state budget. Only at recombination do results merge.

**Adapters are stateless.** Each call receives full chain + constraint context. Adapters can be reused across chains without shared mutable state.

**The harness mocks cross-chain communication for concrete execution.** When Echidna replays a sequence on the source chain, the harness captures emitted message events and feeds them to the destination chain's mock relayer. This allows concrete replay of cross-chain attack sequences without running a real relayer or two live chains.
