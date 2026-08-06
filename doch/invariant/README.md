# Cross-Chain Invariant Specification

Invariants are properties that must hold across a protocol spanning multiple chains, checked against **forked mainnet state** at pinned blocks. The Wormhole protocol has operated for 5+ years — its code is battle-tested, but the accumulated state may harbor edge cases. A cross-chain invariant differs from a single-chain invariant in one critical way: the protocol has in-flight state — messages emitted but not yet delivered across years of activity — where naive equality assertions are invalid. The invariant is evaluated against real mainnet storage, real guardian sets, and real pending message queues.

The harness accepts invariants defined in `.t.sol` using standard Foundry test conventions. A `.t.sol` file declares *what* must hold; the harness' invariant IR (defined below) carries the *when, how, and under what assumptions* the property is checked. The IR is the authoritative representation — the `.t.sol` parser populates it, but a developer may also write the IR directly for properties that cannot be expressed in Solidity assertions alone.

---

## Invariant IR (Internal Representation)

Every invariant, whether parsed from `.t.sol` or authored directly, is normalized into this structure before the scheduler sees it:

```
CrossChainInvariant:
    id: str
    contexts: Map[ChainId, list[Context]]      # may include multiple contracts
    correlation_key: str                       # event field used to pair events
    correlation_extractor: CorrelationExtractor # how to extract key from event
    bindings: list[Binding]                    # variable bindings across chains
    observation_policy: ObservationPolicy
    assumptions: list[Assumption]
    transition_predicates: list[TransitionPredicate]
    property: QuantifiedPredicate
```

### Context

One chain can have multiple contracts. Each context binds a contract to its role and fork configuration:

```
Context:
    chain_id: str                    # "ethereum", "polygon"
    contract: str                    # contract name for ABI/source resolution
    address: str                     # mainnet deployed address (0x...)
    role: str                        # "source", "destination", "relayer", "governance"
    monitors: list[str]              # state variables or events to observe
    fork_block: int                  # pinned mainnet block for this chain
    abi_artifact: str                # path to ABI JSON (from source compilation)
    proxy: Optional[ProxyInfo]       # if the target is behind a proxy

ProxyInfo:
    kind: str                        # "transparent" | "uups" | "beacon"
    implementation_address: str      # implementation address at fork_block
    implementation_code_hash: str    # hash for integrity verification
```

### Correlation Extractor

Defines how to extract the correlation value from a cross-chain event. The harness uses this to pair source and destination events:

```
CorrelationExtractor:
    source: EventSelector            # which event on the source chain
    destination: EventSelector       # which event on the destination chain
    key_field: str                   # field name shared by both events (e.g. "messageHash")

EventSelector:
    contract: str                    # contract name matching a Context
    event_name: str                  # event signature name
```

### Binding

Declares how variables from one chain's state map to variables on another chain. This is what makes quantification over cross-chain state possible:

```
Binding:
    source: str                      # "ethereum.bridgeEth.totalLocked"
    destination: str                 # "polygon.bridgePoly.totalMinted"
    relation: EQUALS | SUM | DIFF    # how the values relate after correlation
```

### Transition Predicate

Declares what state transitions are **valid** on a chain. The decomposer uses these to derive sub-invariants — it does not invent deposit/burn/refund rules:

```
TransitionPredicate:
    chain_id: str
    contract: str
    state_var: str                   # e.g. "locked"
    on_increase: list[str]           # functions that may increase this value
    on_decrease: list[str]           # functions that may decrease this value
    guard: Optional[str]             # additional condition (e.g. "only after verified burn")
```

For a bridge, a developer would declare:

```
TransitionPredicate(
    chain_id="ethereum",
    contract="bridgeEth",
    state_var="locked",
    on_increase=["deposit", "receiveRefund"],
    on_decrease=["burn", "expireMessage"],
)
TransitionPredicate(
    chain_id="polygon",
    contract="bridgePoly",
    state_var="minted",
    on_increase=["mint"],
    on_decrease=["withdraw"],
    guard="only for verified lock events with unique messageHash",
)
```

The decomposer uses these to check that a candidate's state change is valid under the declared transitions, then probes whether an invalid transition is reachable.

### ObservationPolicy

Determines **when** the invariant is checked.

**Scope note:** The harness performs **fork-state invariant testing** — checking invariants against a specific mainnet snapshot at a pinned block. This is not full historical archaeology (which would require multiple representative blocks across upgrade epochs, guardia rotations, and incident windows). A Snapshot Discovery phase (selecting blocks before/after key protocol transitions and clustering by state fingerprint) is deferred to a future milestone. The initial implementation tests one `SnapshotSet` per campaign.

### ObservationPolicy

Determines **when** the invariant is checked:

```
ObservationPolicy:
    kind: PER_TRANSACTION | AFTER_FINALITY | AFTER_ALL_DELIVERED | BLOCK_BOUNDED
    deadline: Optional[int]          # blocks or seconds, for BLOCK_BOUNDED
    deadline_unit: Optional[str]     # "blocks" | "seconds"
    finality_blocks: Optional[int]   # confirmations required, for AFTER_FINALITY
    quiescence: Optional[QuiescenceRule]  # when is "all delivered" satisfied?
```

### QuiescenceRule

Defines when the system is considered quiescent for `AFTER_ALL_DELIVERED`:

```
QuiescenceRule:
    kind: NO_PENDING_MESSAGES | NO_ELIGIBLE_MESSAGES | BOUNDED_BY_BLOCK
    max_pending_age: Optional[int]   # blocks or seconds
    exclude_expired: bool            # ignore expired messages
    exclude_rejected: bool           # ignore rejected messages
```

### Assumption

```
Assumption:
    kind: GUARDIAN_HONESTY | MESSAGE_ORDERING | REORG_DEPTH | LIVENESS | ...
    value: Any                       # e.g. "at_most_N_malicious: 6"
```

### QuantifiedPredicate

The property itself, with explicit quantification over the variables bound across chains:

```
QuantifiedPredicate:
    kind: FORALL | EXISTS | FORALL_EXISTS
    bound_variables: list[str]       # variables from bindings
    predicate: str                   # assertion expression

ObservationSet:
    touched_message_ids: list[str]      # messages involved in the current probe
    relay_dataset_ids: list[str]        # relay data records used
    sampled_historical_ids: list[str]   # historical messages sampled (not full scan)
    probe_generated_ids: list[str]      # messages generated during probing
    max_items: int                      # upper bound for iteration (prevents OOG on mainnet)
```

Mainnet invariants cannot iterate over the full 5-year history. `executedMessageCount()` scanning from index 0 on a long-lived contract is prohibitively expensive or out-of-gas. The `ObservationSet` bounds evaluation to messages that are part of the current campaign or witness — full linear scans of on-chain history are never performed in invariant functions.

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
```

---

## Invariant File Convention

```solidity
// test/invariants/BridgeInvariants.t.sol
pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {IBridgeEth} from "../src/interfaces/IBridgeEth.sol";
import {IBridgePoly} from "../src/interfaces/IBridgePoly.sol";

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

    /// @crosschain src=ethereum dst=polygon
    /// @observation AFTER_ALL_DELIVERED
    /// @transition ethereum:locked increase=deposit,receiveRefund decrease=burn,expireMessage
    /// @transition polygon:minted increase=mint decrease=withdraw
    /// @correlation messageHash
    /// @bind locked=ethereum.bridgeEth.totalLocked minted=polygon.bridgePoly.totalMinted
    /// @quantify FORALL locked,minted: locked == minted
    /// @assume guardian_honesty: at_most_6_malicious
    function invariant_locked_equals_minted_after_delivery() public {
        uint ethLocked = bridgeEth.totalLocked();
        uint polyMinted = bridgePoly.totalMinted();
        assert(ethLocked == polyMinted);
    }
}
```

The NatSpec tags declare the full IR. The Solidity `assert` is a human-readable rendering — the authoritative representation is the IR derived from the NatSpec tags.

---

## Transition Predicates vs Decomposer

The decomposer does **not** invent rules about which functions change which state. It reads `TransitionPredicate` from the IR. For each `on_increase` and `on_decrease` target:

- The sub-invariant for that chain asserts that the state variable only changes through a declared function.
- If a probe finds a path where the state variable changes through an undeclared function, that is a violation of the transition predicate — and therefore of the local sub-invariant.

This is verifiable: the decomposer generates assertions that tools can check. No semantic invention is required.

---

## Cross-Chain Invariant Patterns (with IR Metadata)

### Balance Conservation

```solidity
/// @transition ethereum:locked increase=deposit,receiveRefund decrease=burn,expireMessage
/// @transition polygon:minted increase=mint decrease=withdraw
/// @correlation messageHash
/// @bind locked=ethereum.bridgeEth.totalLocked minted=polygon.bridgePoly.totalMinted
/// @quantify FORALL locked,minted: locked == minted
/// @observation AFTER_ALL_DELIVERED
```

### Quorum Threshold

```solidity
/// @transition ethereum:verifiedSignatures increase=submitMessage decrease=executeMessage
/// @correlation messageHash
/// @bind sigCount=ethereum.bridgeEth.verifiedSignatureCount(msgHash,currentSet)
/// @quantify FORALL msgHash: sigCount >= THRESHOLD
/// @observation PER_TRANSACTION
```

### Message Replay Protection

```solidity
/// @correlation messageHash
/// @bind count=polygon.bridgePoly.consumptionCount(messageHash)
/// @quantify FORALL messageHash: count <= 1
/// @observation PER_TRANSACTION
```

---

## Invariant Metadata (Full NatSpec)

| Tag | Example | Effect |
|---|---|---|
| `@crosschain src=X dst=Y` | `src=ethereum dst=polygon` | Mark as cross-chain, specify chains |
| `@transition` | `chain:var increase=f1,f2 decrease=f3` | Valid state transitions (repeatable) |
| `@observation POLICY` | `AFTER_ALL_DELIVERED` | When the invariant is checked |
| `@correlation KEY` | `messageHash` | How to pair events across chains |
| `@bind` | `locked=eth.totalLocked minted=poly.totalMinted` | Variable bindings across chains |
| `@quantify` | `FORALL locked,minted: locked == minted` | Quantification and predicate |
| `@assume KIND: VALUE` | `guardian_honesty: at_most_6_malicious` | Scope assumption |
| `@tools` | `echidna, halmos` | Restrict tools |
| `@severity` | `CRITICAL` | Impact label |
| `@timeout` | `600` | Per-invariant timeout seconds |

Tags that cannot be expressed in Solidity (`@transition`, `@observation`, `@correlation`, `@bind`, `@quantify`, `@assume`) must be provided via NatSpec. The `.t.sol` parser validates that these are present for cross-chain invariants. Missing metadata is a hard error.


---

## References

- [Halmos](https://github.com/a16z/halmos) — symbolic execution engine used for bounded formal verification
- [Echidna](https://github.com/crytic/echidna) — fuzzer used for concrete sequence exploration
- [Foundry](https://getfoundry.sh/) — Solidity development framework; invariant files follow Foundry test conventions
