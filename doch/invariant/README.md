# Cross-Chain Invariant Specification

Invariants are properties that must hold across a protocol spanning multiple chains, checked against **forked mainnet state** at coherent pinned snapshots. Long-lived protocols contain in-flight messages and accumulated state where naive equality assertions are invalid. On-chain storage is combined with a content-addressed relay dataset; the fork alone is never assumed to contain every pending off-chain attestation.

Invariant files use Solidity and Foundry syntax as a familiar authoring surface, but cross-chain functions are parsed by Astarots rather than executed directly as ordinary single-fork Forge tests. The normalized invariant IR is authoritative and generated per-chain harnesses are what Echidna, Halmos, or Foundry actually execute.

---

## Invariant IR (Internal Representation)

Every invariant, whether parsed from `.t.sol` or authored directly, is normalized into this structure before the unified search engine sees it:

```
CrossChainInvariant:
    id: str
    contexts: Map[ContextId, Context]
    entry_context: ContextId
    correlation_extractor_id: str
    correlation_extractor: CorrelationExtractor
    bindings: list[Binding]
    observation_policy: ObservationPolicy
    observation_set: ObservationSet
    assumptions: list[Assumption]
    transition_predicates: list[TransitionPredicate]
    tool_allowlist: list[str]
    severity: CRITICAL | HIGH | MEDIUM | LOW
    timeout_seconds: int
    property: Property
```

### Context

One chain can have multiple contracts. A context ID resolves to exactly one validated `ChainTarget` in the campaign `TargetSet`; deployment and proxy fingerprints are not duplicated inside the invariant:

```
Context:
    context_id: ContextId             # e.g. "ethereum.bridge"; TargetSet join key
    chain_id: ChainId                 # validated against the target binding
    role: str                         # source, destination, relayer, governance
    monitors: list[StateReference | EventSelector]
    snapshot_ref: str                 # chain entry in the campaign SnapshotSet
```

```
StateReference:
    context_id: ContextId
    kind: GETTER | STORAGE_PATH
    getter: Optional[FunctionSelector]
    storage_path: Optional[str]
    result_path: Optional[str]         # tuple field or array index
    arguments: list[str]              # bound variables or earlier binding IDs
    value_type: str
```

Exactly one of `getter` or `storage_path` is set. Getter references use canonical signatures; arguments are supplied separately and binding dependencies must be acyclic. NatSpec writes a parameterized getter as `context.function(type,...)[arg,...]`. Storage paths are resolved against the validated storage layout; ambiguous or missing references are invalid configuration.

### Correlation Extractor

Defines how to extract the correlation value from a cross-chain event. The harness uses this to pair source and destination events:

```
CorrelationExtractor:
    source: EventSelector
    destination: EventSelector
    source_fields: list[str]          # may be composite: emitter + sequence
    destination_fields: list[str]
    normalize: TransformRef            # adapter-defined canonicalization

EventSelector:
    context_id: ContextId
    event_signature: str              # full signature, not only overloaded name
```

NatSpec names a configured extractor; it does not ask the parser to infer event semantics:

```toml
[correlations.bridge_message]
source_context = "ethereum.bridge"
source_event = "Locked(bytes32,address,uint256)"
source_fields = ["messageHash"]
destination_context = "polygon.bridge"
destination_event = "Minted(bytes32,address,uint256)"
destination_fields = ["messageHash"]
normalize = "bytes32"
```

### Binding

Declares how variables from one chain's state map to variables on another chain. This is what makes quantification over cross-chain state possible:

```
Binding:
    id: str                            # variable name used by Property
    sources: list[StateReference]
    reduce: IDENTITY | SUM | DIFF | CUSTOM
    transform: Optional[TransformRef]  # decimals, fees, or adapter function
```

A single-source binding has the structural reduction `IDENTITY`. Multi-source bindings must declare `SUM`, `DIFF`, or a named `CUSTOM` reducer; dependency cycles and implicit numeric conversions are rejected.

### Transition Predicate

Declares which calls may change observed state and how. The normalized rule supports scalar, mapping, reset, and adapter-defined effects:

```
TransitionPredicate:
    context_id: ContextId
    binding_id: str
    rules: list[TransitionRule]

TransitionRule:
    id: str
    function: FunctionSelector
    effect: INCREASE | DECREASE | SET | RESET | DELETE | MAPPING_WRITE | CUSTOM
    guard: Optional[Expression]
    affected_bindings: list[str]
    custom_effect: Optional[TransformRef]  # required only for CUSTOM

FunctionSelector:
    context_id: ContextId
    function_signature: str          # canonical signature, including parameter types
```

For example:

```
TransitionPredicate(
    context_id="ethereum.bridge",
    binding_id="locked",
    rules=[
        TransitionRule(
            id="locked.deposit",
            function=FunctionSelector("ethereum.bridge", "deposit(uint256,address)"),
            effect=INCREASE,
            affected_bindings=["locked"],
        ),
        TransitionRule(
            id="locked.burn",
            function=FunctionSelector("ethereum.bridge", "burn(uint256,address)"),
            effect=DECREASE,
            affected_bindings=["locked"],
        ),
    ],
)
TransitionPredicate(
    context_id="polygon.bridge",
    binding_id="count",
    rules=[
        TransitionRule(
            id="count.execute",
            function=FunctionSelector("polygon.bridge", "executeMessage(bytes32)"),
            effect=MAPPING_WRITE,
            guard=Binary(EQ, Reference("count"), Literal(0)),
            affected_bindings=["count"],
        ),
    ],
)
```

The NatSpec `increase=[...]` and `decrease=[...]` forms are compact syntax for `TransitionRule` entries. Other effects use `effect=SET|RESET|DELETE|MAPPING_WRITE|CUSTOM` with an explicit function list and guard. The decomposer only validates and materializes these declarations.

### Observation Policy

Determines **when** the invariant is checked. The initial milestone performs fork-state invariant testing against one coherent `SnapshotSet` per campaign; multi-epoch snapshot discovery remains a later milestone.

```
ObservationPolicy:
    kind: PER_TRANSACTION | AFTER_FINALITY | AFTER_ALL_DELIVERED | BLOCK_BOUNDED
    deadline: Optional[Deadline]     # required for BLOCK_BOUNDED
    finality_blocks: Optional[int]   # confirmations required, for AFTER_FINALITY
    quiescence: Optional[QuiescenceRule]  # when is "all delivered" satisfied?

Deadline:
    value: int
    unit: BLOCKS | SECONDS
    chain_id: Optional[ChainId]      # required when chain clocks may diverge
```

### QuiescenceRule

Defines when the system is considered quiescent for `AFTER_ALL_DELIVERED`:

```
QuiescenceRule:
    kind: NO_PENDING_MESSAGES | NO_ELIGIBLE_MESSAGES | BOUNDED_BY_BLOCK
    max_pending_age: Optional[Deadline]
    exclude_expired: bool            # ignore expired messages
    exclude_rejected: bool           # ignore rejected messages
```

### Assumption

```
Assumption:
    kind: SIGNER_HONESTY | MESSAGE_ORDERING | FINALITY_MODEL | LIVENESS | PROTOCOL_SPECIFIC
    value: Any                       # e.g. "at_most_N_malicious: 6"
```

### Property

Safety and bounded liveness are distinct:

```
Property:
    kind: SAFETY | EVENTUALLY
    predicate: QuantifiedPredicate
    trigger: Optional[Expression]      # required for EVENTUALLY
    deadline: Optional[Deadline]     # required for EVENTUALLY

QuantifiedPredicate:
    kind: FORALL | EXISTS | FORALL_EXISTS
    bound_variables: list[str]
    predicate: Expression
```

```
Expression:
    type: str
    node: Literal | Reference | Unary | Binary | AdapterCall

Literal:
    value: Any

Reference:
    name: str                          # binding ID, quantified variable, or call argument

Unary:
    op: NOT | NEGATE
    operand: Expression

Binary:
    op: EQ | NE | LT | LE | GT | GE | AND | OR | ADD | SUB | MUL | DIV | MOD
    left: Expression
    right: Expression

AdapterCall:
    function: TransformRef
    arguments: list[Expression]

TransformRef:
    function: str
    version: str
```

NatSpec expressions are parsed and type-checked into this AST before search. Arithmetic uses Solidity-compatible checked integer semantics for the declared type. Calls are forbidden except versioned, pure adapter functions declared in the effective configuration; unknown names, coercions, and side effects are hard errors.

An `EVENTUALLY` property starts its deadline only when the declared trigger becomes true. Exhausting the deadline without a conclusive observation is a violation; a timeout or missing relay data is `inconclusive`, not a liveness failure.

### Observation Set

Bounds which historical and probe-generated messages may be inspected:

```
ObservationSet:
    touched_message_ids: list[str]
    relay_dataset_ids: list[str]
    sampled_historical_ids: list[str]
    probe_generated_ids: list[str]
    max_items: int
```

Mainnet invariants cannot iterate over an unbounded protocol history. Evaluation is restricted to the declared observation set; full linear scans of long-lived on-chain history are never generated.

Examples:

```
# Safety: for all correlated message pairs, locked == minted
QuantifiedPredicate(
    kind=FORALL,
    bound_variables=["locked", "minted"],
    predicate="locked == minted",
)

# Replay protection: for all messages, consumption count ≤ 1
QuantifiedPredicate(
    kind=FORALL,
    bound_variables=["messageHash"],
    predicate="consumption_count[messageHash] <= 1",
)

# Bounded liveness: every finalized source message is eventually consumed
Property(
    kind=EVENTUALLY,
    trigger="message_status[messageHash] == SourceFinalized",
    deadline=Deadline(value=128, unit=BLOCKS, chain_id="polygon"),
    predicate=QuantifiedPredicate(
        kind=FORALL,
        bound_variables=["messageHash"],
        predicate="consumption_count[messageHash] == 1",
    ),
)
```

---

## Invariant File Convention

```solidity
// test/invariants/BridgeInvariants.t.sol
pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {IBridgeEth} from "../../src/interfaces/IBridgeEth.sol";
import {IBridgePoly} from "../../src/interfaces/IBridgePoly.sol";

contract BridgeInvariants is Test {
    // Mainnet-fork mode: cast to existing deployed addresses.
    // The execution backend forks mainnet at the configured block.
    // These contracts are NOT deployed fresh — they are the real
    // mainnet contracts with years of accumulated state.
    IBridgeEth bridgeEth;
    IBridgePoly bridgePoly;

    function setUp() public {
        // Fork-block addresses from chain registry config.
        // Source paths are used ONLY for ABI artifacts, Slither,
        // and storage layout — not for deployment.
        bridgeEth = IBridgeEth(ETH_BRIDGE_ADDRESS);
        bridgePoly = IBridgePoly(POLY_BRIDGE_ADDRESS);
    }

    /// @crosschain contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge
    /// @observation AFTER_ALL_DELIVERED quiescence=NO_ELIGIBLE_MESSAGES max_pending_age=ethereum:7200s exclude=expired,rejected
    /// @transition ethereum.bridge:locked increase=["deposit(uint256,address)","receiveRefund(bytes32)"] decrease=["burn(uint256,address)","expireMessage(bytes32)"]
    /// @transition polygon.bridge:minted increase=["mint(bytes)"] decrease=["withdraw(uint256,address)"]
    /// @correlation bridge_message
    /// @bind locked=ethereum.bridge.totalLocked() minted=polygon.bridge.totalMinted()
    /// @quantify FORALL locked,minted: locked == minted
    /// @observe touched,relay max=256
    /// @assume signer_honesty: at_most_6_malicious
    /// @tools echidna,halmos,slither
    /// @severity CRITICAL
    /// @timeout 600
    function invariant_locked_equals_minted_after_delivery() public {
        uint ethLocked = bridgeEth.totalLocked();
        uint polyMinted = bridgePoly.totalMinted();
        assert(ethLocked == polyMinted);
    }
}
```

The NatSpec tags declare the full IR. The Solidity `assert` is a checked, human-readable rendering of the supported predicate subset. Astarots parses this function, generates one executable harness per chain, and evaluates the cross-chain predicate against `GlobalState`; the function is not executed directly while two forks are simultaneously active. Address constants are injected from the validated target configuration.

---

## Transition Predicates vs Decomposer

The decomposer does **not** infer which functions may mutate state. For each declared `TransitionRule`, it generates a local monitor over the before/after value, canonical function selector, effect, guard, and affected bindings. A mutation outside those rules violates the local monitor; adapter-defined `CUSTOM` effects require an executable adapter predicate.

---

## Cross-Chain Invariant Patterns (with IR Metadata)

### Balance Conservation

```solidity
/// @transition ethereum.bridge:locked increase=["deposit(uint256,address)","receiveRefund(bytes32)"] decrease=["burn(uint256,address)","expireMessage(bytes32)"]
/// @transition polygon.bridge:minted increase=["mint(bytes)"] decrease=["withdraw(uint256,address)"]
/// @correlation bridge_message
/// @bind locked=ethereum.bridge.totalLocked() minted=polygon.bridge.totalMinted()
/// @quantify FORALL locked,minted: locked == minted
/// @observation AFTER_ALL_DELIVERED quiescence=NO_ELIGIBLE_MESSAGES max_pending_age=ethereum:7200s exclude=expired,rejected
```

### Quorum Threshold

```solidity
/// @transition ethereum.bridge:executed effect=MAPPING_WRITE functions=["executeMessage(bytes32)"] guard="signerCount >= required && !executed"
/// @correlation bridge_message
/// @bind epoch=ethereum.bridge.verifierEpoch(bytes32)[messageHash] executed=ethereum.bridge.executed(bytes32)[messageHash] signerCount=ethereum.bridge.signerCount(bytes32,uint32)[messageHash,epoch] required=ethereum.bridge.requiredThreshold(uint32)[epoch]
/// @quantify FORALL messageHash: !executed || signerCount >= required
/// @observe touched,relay max=256
/// @observation PER_TRANSACTION
```

### Message Replay Protection

```solidity
/// @transition polygon.bridge:count effect=MAPPING_WRITE functions=["executeMessage(bytes32)"] guard="count == 0"
/// @correlation bridge_message
/// @bind count=polygon.bridge.consumptionCount(bytes32)[messageHash]
/// @quantify FORALL messageHash: count <= 1
/// @observe touched,relay max=256
/// @observation PER_TRANSACTION
```

---

## Invariant Metadata (Full NatSpec)

| Tag | Example | Effect |
|---|---|---|
| `@crosschain` | `contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge` | Bind target contexts and initial execution context |
| `@transition` | `context:state increase=["f(uint256)"]` or `effect=MAPPING_WRITE ...` | Canonical selectors normalized to `TransitionRule` |
| `@observation POLICY` | `AFTER_ALL_DELIVERED quiescence=NO_ELIGIBLE_MESSAGES max_pending_age=ethereum:7200s exclude=expired,rejected` | Evaluation point and temporal/quiescence parameters |
| `@correlation NAME` | `bridge_message` | Select a configured `CorrelationExtractor` |
| `@bind` | `locked=ethereum.bridge.totalLocked() minted=polygon.bridge.totalMinted()` | Canonical getter or storage-path bindings across contexts |
| `@quantify` | `FORALL locked,minted: locked == minted` | Quantification and predicate |
| `@eventually` | `trigger="..." deadline=polygon:128blocks predicate="..."` | Bounded-liveness property; mutually exclusive with `@quantify` |
| `@observe` | `touched,relay max=256` | Bounded observation-set sources and maximum items |
| `@assume KIND: VALUE` | `signer_honesty: at_most_6_malicious` | Scope assumption |
| `@tools` | `echidna, halmos` | Restrict tools |
| `@severity` | `CRITICAL` | Impact label |
| `@timeout` | `600` | Per-invariant timeout seconds |

The parser never infers NatSpec semantics from the assertion body. `@crosschain`, transition rules, observation policy, correlation extractor, bindings, one property tag (`@quantify` or `@eventually`), and a bounded `@observe` set are required unless supplied by explicit IR. Assumptions are optional; tools, severity, and timeout may use validated campaign defaults.


---

## References

- [Halmos](https://github.com/a16z/halmos) — symbolic execution engine used for bounded formal verification
- [Echidna](https://github.com/crytic/echidna) — fuzzer used for concrete sequence exploration
- [Foundry](https://getfoundry.sh/) — Solidity development framework; invariant files follow Foundry test conventions
