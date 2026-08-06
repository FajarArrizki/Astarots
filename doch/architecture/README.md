# Architecture

Astarots is a **cross-chain invariant testing harness** for properties such as bridge accounting, message replay protection, and verifier quorum boundaries.

The core idea: invariants are checked against **forked mainnet state** at pinned blocks, not against freshly deployed contracts. Long-lived cross-chain protocols accumulate upgrades, governance epochs, balances, and message state that can expose edge cases invisible to code-only analysis. The harness forks each chain at a coherent snapshot, probes from that state, and discovers attack vectors that require a specific state plus a specific transaction sequence.

---

## Data Flow

```
                      ┌──────────────────────────┐
                      │ test/invariants/*.t.sol    │
                      │ Cross-chain invariant IR  │
                      │ contexts + entry declared │
                      │ correlation + observation │
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
│                    ┌────────────────────────────────────────┐     │
│                    │  Unified Causal Search Engine          │     │
│                    │  - one branch-local GlobalState        │     │
│                    │  - message lifecycle + invariant eval  │     │
│                    │  - frontier, ranking, dedup, budgets   │     │
│                    └───────────────┬────────────────────────┘     │
│                                    │                              │
│             ┌──────────────────────┼──────────────────────┐       │
│             ▼                      ▼                      ▼       │
│      ┌────────────┐         ┌────────────┐         ┌────────────┐ │
│      │  Slither   │         │  Echidna   │         │   Halmos   │ │
│      │ static hint│         │ candidates │         │ projection │ │
│      └────────────┘         └────────────┘         └────────────┘ │
│                                    │                              │
│                                    ▼                              │
│                    ┌────────────────────────────────────────┐     │
│                    │  Canonical Fork Executor               │     │
│                    │  - pinned base fingerprints            │     │
│                    │  - branch-local overlays + replay      │     │
│                    │  - authoritative events/state diffs    │     │
│                    └───────────────┬────────────────────────┘     │
│                                    │                              │
│                                    ▼                              │
│                    ┌────────────────────────────────────────┐     │
│                    │  Report Engine                         │     │
│                    │  - console trace    - JSON evidence    │     │
│                    └────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────────┘
```

---

## Component Roles

### Invariant Loader

Parses `.t.sol` test files and extracts cross-chain invariant function signatures from NatSpec tags. Produces a normalized `CrossChainInvariant` struct. The loader validates that the NatSpec `@quantify` predicate matches the Solidity `assert()` expression — if the two representations diverge, the loader emits a hard error. This prevents the authoritative IR from drifting from the human-readable Solidity rendering.

### Predicate Expression Engine

Evaluates `QuantifiedPredicate.predicate` strings at runtime. The predicate is a mini expression language over bound variables and chain snapshots. It supports equality (`==`, `!=`), inequality (`<`, `>`, `<=`, `>=`), and logical operators (`&&`, `||`, `!`) over integer variables and array lookups. The engine resolves variable references through the invariant's `Binding` declarations and extracts values from `GlobalState.chain_snapshots`.

This component is necessary because predicates such as `"locked == minted"` must be evaluated against actual cross-chain state. The bounded expression engine supports only the documented subset and remains separate from the loader and unified search engine.

### Cross-Chain Decomposer

Breaks the invariant into local monitors from explicit `TransitionPredicate` rules while preserving the global `Property`, bindings, correlation extractor, and observation semantics. No business rule is inferred from the Solidity assertion body.

For each context, the decomposer generates local monitors:
- Each observed state change must match a declared function selector, effect, guard, and affected binding.
- An unmatched mutation is a local monitor violation; a matched transition remains a candidate for the global property.

The global check uses the configured `CorrelationExtractor`, resolves bindings, and evaluates the declared safety or bounded-liveness `Property` against one branch-local `GlobalState`.

The decomposer rejects missing context bindings, correlation extractors, transition rules, or observation semantics rather than applying silent defaults.

### Chain Registry

Manages per-chain configuration: RPC endpoints, **pinned fork block numbers**, mainnet contract addresses, and chain-specific tool settings. Each chain is registered with a unique alias and a fork block.

```
TargetSet:
    contexts: Map[ContextId, ChainTarget]

ChainTarget:
    context_id: ContextId
    chain_id: ChainId
    address: str
    artifact_path: str
    artifact_hash: str
    source_target: Optional[str]
    role: str
    expected_runtime_code_hash: str
    proxy: Optional[ProxyBinding]

ProxyBinding:
    kind: TRANSPARENT | UUPS | BEACON | NONE
    implementation_address: Optional[str]
    expected_implementation_code_hash: Optional[str]
```

Context IDs are the join key across target configuration, invariant IR, traces, and evidence. Dry-run rejects duplicate IDs, chain mismatches, missing artifacts, source-required adapters without source targets, or fingerprint mismatches before any search state is created.

### Fork Snapshot Coherence

Choosing independent blocks does not guarantee a coherent cross-chain snapshot. Chains may differ in time, finality, protocol epoch, upgrade state, observed message cutoff, or delivery status; a `SnapshotSet` makes those relationships explicit.

```
SnapshotSet:
    id: str
    schema_version: str
    snapshots: Map[ChainId, ForkSnapshot]
    anchor_timestamp: int              # reference timestamp for alignment
    finality_policy: str               # "probabilistic" | "checkpoint" | "instant"
    protocol_epochs: Map[str, str]      # adapter-defined epochs, if applicable
    message_cutoffs: Map[EmitterId, int] # last included message per emitter
    coherence_checks: list[CoherenceCheck]
    coherence_checks_hash: str

CoherenceCheck:
    kind: str                           # timestamp_delta, finality, epoch, cutoff, ...
    observed: Any
    relation: str                       # <=, ==, contains, ...
    expected: Any
    evidence_hash: str
```

The set is validated before probing. Block hashes, state roots, target code hashes, proxy implementations, and every structured coherence check are recorded. Protocol-specific adapters may add checks without hard-coding one bridge's epoch model into the core.

### Relay Dataset

Forked EVM state does not contain a universal list of emitted-but-undelivered cross-chain messages. A separate, content-addressed **RelayDataset** supplies the message and attestation material required by the selected protocol adapter:

```
RelayDataset:
    schema_version: str
    dataset_hash: str
    protocol: str
    source_block_ranges: Map[ChainId, (int, int)]
    messages: list[ProtocolMessageEnvelope]
    provenance: str                    # indexed-logs, historical-attestations, API
    provenance_hash: str

ProtocolMessageEnvelope:
    message_id: bytes
    correlation_value: bytes
    source_chain: ChainId
    source_block_hash: bytes32
    source_log_index: int
    emitter: str
    destination_chain: ChainId
    destination_context: ContextId
    payload: bytes
    attestation: Optional[bytes]
    destination_status: Delivered | Pending | Expired | Unknown
    status_evidence_hash: str
    protocol_metadata: Map[str, Any]
```

Each protocol adapter maps its native message identifier, proof bytes, verifier epoch, and delivery metadata into `ProtocolMessageEnvelope`; fetched artifacts are pinned by content hash.

### Cross-Chain Message Coordinator

Cross-chain execution is **causal**: the destination chain's state depends on messages emitted by the source chain. The coordinator manages this dependency through an explicit message lifecycle:

```
Emitted → SourceFinalized → RelayEligible → Delivered → Consumed | Rejected | Expired
```

```
RelayPolicy:
    mode: HistoricalAuthentic | ProtocolValidSynthetic | ModeledRelay | RawPayload
    protocol_adapter: str
    adapter_config_hash: str
    finality_blocks: Map[ChainId, int]
    delay_model: NONE | FIXED | BOUNDED | DATASET
    min_delay_seconds: Map[ChainId, int]
    max_delay_seconds: Map[ChainId, int]
    ordering: FIFO_PER_EMITTER | UNORDERED | PROTOCOL_DEFINED
    duplicate_delivery: REJECT | ALLOW_FOR_TEST
    reorg_assumption: NO_REORG_AFTER_FINALITY
    delivery_deadline: Optional[Deadline]
    protocol_epoch_rules: Map[str, Any]
```

Each lifecycle transition has a guard derived from this policy and the protocol adapter. Invalid transitions are rejected rather than added to the frontier; rejected and expired messages are handled by the invariant's quiescence rule.

Mode controls proof availability. `HistoricalAuthentic` can deliver only an exact dataset envelope whose message ID, payload hash, source event, and attestation all match; a newly fuzzed source event without such an envelope remains ineligible. `ProtocolValidSynthetic` asks a declared local signer/verifier fixture to create valid test proofs, `ModeledRelay` applies the configured abstraction, and `RawPayload` deliberately explores unauthenticated input after the trust-boundary label is recorded.

The coordinator tracks eligibility and models delay, ordering, duplicate delivery, replay, chain clocks, and protocol-defined epoch changes as branch-local constraints. `finality_blocks` defines the confirmations required before `SourceFinalized`; the milestone assumes no reorg after that boundary and records the assumption in evidence rather than pretending to simulate arbitrary alternate chain histories.

Static analysis (Slither) and initial local probing (Echidna corpus generation) can run independently on each chain because they do not mutate shared state. Once canonical replay emits a cross-chain event, the coordinator records it and schedules only lifecycle transitions allowed by the selected relay mode. Destination execution is causally ordered after eligible delivery, never produced by merging independent per-chain snapshots.

### Candidate Workers

Tool adapters perform parallelizable local work: static analysis, corpus generation, fuzzing, or bounded symbolic confirmation. They return typed hints and candidate traces; they do not own the global frontier or merge independent chain states.

### Unified Causal Search Engine

One search engine owns the global priority queue and every immutable branch-local `GlobalState`. It chooses the next chain action, asks candidate workers for proposals, sends each proposal to the canonical executor, advances the message lifecycle, and evaluates the invariant directly against the resulting global snapshot. There is one `SearchResult` for the campaign, not one result per chain.

### Canonical Fork Executor

The canonical executor is the sole source of truth for reachability, events, and state diffs. It initializes pinned forks from a `SnapshotSet`, manages branch-local checkpoints or copy-on-write overlays, applies relay transitions, and exports deterministic `ActionTrace` artifacts. Tool-internal snapshots never become global state.

### Adapter Registry

Maps tool names to adapter implementations and capabilities. The registry routes candidate-generation or confirmation requests with chain-specific configuration; unsupported operations return `Unsupported` and fall back to another capable tool.

### Evidence Aggregator

Collects per-step evidence from the single causal trace, applies explicit evidence-strength aggregation rules, and emits `CrossChainEdgeCase` findings. It never combines witnesses from unrelated branches.

### Actor & Privilege Model

The unified search engine can impersonate addresses on local forks, but unconstrained impersonation creates false positives. Every action therefore carries an explicit actor:

```
Actor:
    address: str                      # 0x...
    role: str                         # "attacker" | "signer" | "governance" | "relayer" | "user" | "admin"
    provenance: str                   # "fork_state" | "generated" | "derived_from_event"
    privilege_level: str              # "none" | "basic" | "operator" | "signer" | "governance"
    impersonation_allowed: bool       # permitted by the campaign actor policy?
    funding_method: str               # "from_fork_balance" | "deal" | "transfer_from_whale"
```

Findings are classified by the attacker model they require:
- **permissionless** — any external actor with no special privileges
- **compromised_signer** — requires control of one or more protocol signing keys
- **compromised_governance** — requires governance execution privileges
- **privileged_operator** — requires operator/admin role
- **state_only** — invariant violation that exists in the forked state without any attacker action

Only `permissionless` findings represent true zero-privilege exploits. Other classifications document the trust assumptions under which the violation occurs.

### Report Engine

Renders findings with per-chain trace annotations. Each call in a cross-chain attack sequence is labeled with the chain it executes on and the tool that discovered it. Output formats: console (for development) and JSON (for CI and programmatic consumption).

---

## Key Design Decisions

**Cross-chain is the primary mode.** The harness is designed around multi-chain invariants from the ground up. Single-chain invariants are supported as a degenerate case and will be elevated to first-class in a future milestone.

**Decomposition validates declared semantics; it does not invent them.** Contexts, transition predicates, correlation extractors, bindings, observation policy, and bounded observation sets are all explicit inputs.

**Message lifecycle is an explicit model.** Delay, ordering, duplicate delivery, replay, finality, reorg assumptions, and protocol-defined epochs are modeled as constraints so valid transient states are not mislabeled as bugs.

**The unified frontier is causal.** Static analysis and initial candidate generation may run in parallel, but one search engine owns the frontier. Once a message is emitted, the coordinator and canonical executor serialize its lifecycle within that branch-local trace.

**Adapters expose capabilities.** Slither cannot execute, Echidna cannot symbolically prove, and Halmos currently confirms only projected state; the unified search engine routes each operation accordingly.

**The harness operates on forked mainnet state, not fresh deploys.** Execution starts from pinned blocks and deployed addresses. Constraint extraction reads lazy-loaded on-chain storage plus messages supplied by a content-addressed `RelayDataset`; a fork alone is never assumed to contain all pending cross-chain attestations. Fresh deploys are reserved for harness unit tests.

**Replay starts from recorded base fingerprints.** Fresh forks are created from the same RPC blocks and the recorded `ActionTrace`—including environment transitions—is replayed deterministically. No in-memory fork is itself forked, and tool-internal snapshot handles are never shared.
