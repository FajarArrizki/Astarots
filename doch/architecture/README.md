# Architecture

Astarots is a **guided attack-surface explorer** for Solidity contracts. It combines multiple analysis tools under a single harness to discover deep edge cases that no single tool can find alone. The core idea: each tool probes the contract from a different angle, and the results of one tool become the starting point for the next — progressively tightening constraints until an edge case is either proven or exhausted.

---

## Data Flow

```
                       ┌──────────────────────┐
                       │   test/*.t.sol        │
                       │   Invariant definitions│
                       └──────────┬───────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────┐
│                         Harness                              │
│                                                              │
│  ┌──────────┐    ┌──────────────┐    ┌───────────────────┐  │
│  │ Invariant│───▶│   Scheduler  │───▶│  Adapter Registry │  │
│  │  Loader  │    │ (beam search)│    │  (echidna, halmos, │  │
│  └──────────┘    └──────┬───────┘    │   slither, ...)    │  │
│                         │            └─────────┬─────────┘  │
│                         │                      │            │
│                         ▼                      ▼            │
│               ┌─────────────────┐   ┌───────────────────┐   │
│               │  Search Engine  │   │  Tool Adapters    │   │
│               │                 │   │                   │   │
│               │  - beam search  │   │  spawn tool       │   │
│               │  - state dedup  │   │  parse output     │   │
│               │  - priority Q   │   │  normalize →      │   │
│               │  - constraint   │   │  internal structs │   │
│               │    consistency  │   │                   │   │
│               └────────┬────────┘   └───────────────────┘   │
│                        │                                     │
│                        ▼                                     │
│               ┌─────────────────┐                            │
│               │  Report Engine  │                            │
│               │  - console      │                            │
│               │  - JSON         │                            │
│               │  - HTML         │                            │
│               └─────────────────┘                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Component Roles

### Invariant Loader

Parses `.t.sol` test files and extracts invariant function signatures, target contracts, and any metadata annotations. Produces a normalized `Invariant` struct that the rest of the harness consumes. Does not execute anything — just reads and validates.

A single `.t.sol` file may declare multiple invariants. Each invariant may specify which tools to run against it, or default to all registered tools.

### Scheduler

Orchestrates the beam search across tools and depth. Given a target contract and an invariant, the scheduler:

- Initializes the search frontier with an empty `SearchState`.
- At each depth, probes the contract through one or more adapters.
- Ranks candidates by suspicion, applies beam width, checks constraint consistency, deduplicates states, and pushes reachable states back into the frontier.
- Continues until max depth or state budget is exhausted.
- Returns the deepest confirmed `EdgeCase`, or nothing if no violation was found.

The scheduler is tool-agnostic. It only knows about `SearchState`, `EdgeCase`, and the adapter interface.

### Adapter Registry

Maps tool names (`echidna`, `halmos`, `slither`) to their adapter implementations. Each adapter conforms to a shared protocol so the scheduler can call `probe()`, `execute()`, and `confirm()` without knowing which tool is behind the interface.

### Tool Adapters

Each adapter wraps one external tool and is responsible for:

- Translating invariant + constraints into tool-specific input (config files, CLI args, seed state).
- Spawning the tool process and capturing output.
- Parsing tool-specific output into the harness' internal `Finding` and `Candidate` structs.
- Supporting re-entrant execution with narrowed constraints for the enrichment loop.

The first adapters to implement: Echidna (fuzzing), Slither (static analysis), Halmos (symbolic execution).

### Search Engine

The core algorithm implementation. Houses `SearchState`, `EdgeCase`, the priority queue frontier, constraint deduplication, and the main `deepest_edge()` loop. All logic is pure Python with no external process dependencies, making it testable in isolation.

### Report Engine

Takes the final `EdgeCase` (or a list of findings across multiple invariants) and renders them for human consumption. Initial output target is a console table. JSON and HTML formatters follow once the console path is stable.

---

## Key Design Decisions

**Adapters are stateless.** Each call to `probe()` or `execute()` receives the full context as arguments. This means adapters can be parallelized — the scheduler can probe multiple candidates across different tools simultaneously without shared mutable state.

**The harness never modifies Solidity source.** Invariants are defined in `.t.sol` using standard Foundry conventions. The harness reads, spawns tools, and collects results — it does not inject code, rewrite imports, or alter the compilation pipeline.

**Cross-chain is a scheduling concern, not an adapter concern.** Cross-chain invariants are decomposed by the scheduler into per-chain sub-invariants. Each sub-invariant is handed to adapters as a normal single-chain probe. The scheduler recombines the results and checks the cross-chain property in Python.
