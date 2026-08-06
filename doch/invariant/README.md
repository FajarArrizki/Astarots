# Cross-Chain Invariant Specification

Invariants are properties that must hold across a protocol spanning multiple chains. They are the "fence posts" that the harness probes against. A cross-chain invariant differs from a single-chain invariant in one critical way: the protocol has in-flight state — messages emitted but not yet delivered — where naive equality assertions are invalid.

The harness accepts invariants defined in `.t.sol` using standard Foundry test conventions. A `.t.sol` file declares *what* must hold; the harness' invariant IR (defined below) carries the *when, how, and under what assumptions* the property is checked. The IR is the authoritative representation — the `.t.sol` parser populates it, but a developer may also write the IR directly for properties that cannot be expressed in Solidity assertions alone.

---

## Invariant IR (Internal Representation)

Every invariant, whether parsed from `.t.sol` or authored directly, is normalized into this structure before the scheduler sees it:

```
CrossChainInvariant:
    id: str                          # stable identifier
    contexts: Map[ChainId, Context]  # one context per participating chain
    correlation_key: str             # how to pair events across chains
    observation_policy: ObservationPolicy
    assumptions: list[Assumption]
    property: SafetyPredicate | EventuallyPredicate
```

### Context

```
Context:
    chain_id: str                    # "ethereum", "polygon"
    contract: str                    # contract name or address
    monitors: list[str]              # state variables or events to observe
```

### ObservationPolicy

Determines **when** the invariant is checked. A property that is only true after all in-flight messages are delivered must not be checked mid-flight.

```
ObservationPolicy:
    kind: PER_TRANSACTION | AFTER_FINALITY | AFTER_ALL_DELIVERED | BLOCK_BOUNDED
    deadline: Optional[int]          # blocks or seconds, for BLOCK_BOUNDED
    finality_blocks: Optional[int]   # confirmations required, for AFTER_FINALITY
```

### Assumption

Conditions the harness assumes to hold. These are NOT checked — they define the scope. If an assumption is violated in the real world, the invariant's guarantees do not apply.

```
Assumption:
    kind: GUARDIAN_HONESTY | MESSAGE_ORDERING | REORG_DEPTH | LIVENESS | ...
    value: Any                       # e.g. "at_most_N_malicious: 6"
```

### Property

Two kinds of cross-chain properties:

```
SafetyPredicate:                     # must always hold
    predicate: str                   # assertion expression

EventuallyPredicate:                 # must hold eventually
    predicate: str
    deadline: int                    # blocks or seconds
```

`safety` is for properties like "message replay count ≤ 1". `eventually` is for liveness properties like "a valid lock event is minted on the destination within N blocks".

---

## Invariant File Convention

Invariant files live under `test/invariants/`. The `@crosschain` tag signals a cross-chain invariant. Additional NatSpec tags declare observation policy and assumptions:

```solidity
// test/invariants/BridgeInvariants.t.sol
pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {BridgeEth} from "../src/BridgeEth.sol";
import {BridgePoly} from "../src/BridgePoly.sol";

contract BridgeInvariants is Test {
    BridgeEth bridgeEth;
    BridgePoly bridgePoly;

    function setUp() public {
        bridgeEth = new BridgeEth();
        bridgePoly = new BridgePoly();
    }

    /// @crosschain src=ethereum dst=polygon
    /// @observation AFTER_ALL_DELIVERED
    /// @assume guardian_honesty: at_most_6_malicious
    /// @assume message_ordering: ordered_by_sequence
    /// @correlation messageHash
    function invariant_locked_equals_minted() public {
        uint ethLocked = bridgeEth.totalLocked();
        uint polyMinted = bridgePoly.totalMinted();
        assert(ethLocked == polyMinted);
    }
}
```

The `@observation AFTER_ALL_DELIVERED` tag tells the harness: do not check this invariant while messages are in flight. The invariant is only expected to hold after all emitted messages have been delivered and processed on the destination.

---

## Cross-Chain Invariant Patterns

### Balance Conservation (Eventual)

The most fundamental cross-chain invariant: what is locked on the source must equal what is minted on the destination — **after all in-flight messages are delivered**.

```solidity
/// @crosschain src=ethereum dst=polygon
/// @observation AFTER_ALL_DELIVERED
/// @correlation messageHash
function invariant_locked_equals_minted_after_delivery() public {
    assert(bridgeEth.totalLocked() == bridgePoly.totalMinted());
}
```

Without `AFTER_ALL_DELIVERED`, this assertion fails on every normal `lock()` call — the source has incremented `locked` but the destination hasn't yet received the message.

**What can break it:** reentrant lock, double-spend via replay, message ordering manipulation, partial fills that don't reconcile, message expiration without refund.

**Decomposition:**
- Source (safety): `locked` only increases on valid deposit; only decreases on verified burn or expired-message refund.
- Destination (safety): `minted` only increases on verified lock event with unique `messageHash`.
- Cross-check (eventual): for every finalized lock event on source, there exists a corresponding mint on destination with equal amount, checked after all eligible messages are delivered.

### Quorum Threshold (Safety)

Guardian-based bridges require M-of-N signatures. This is a pure safety property — it must hold at every state, regardless of in-flight messages.

```solidity
/// @crosschain src=ethereum dst=polygon
/// @observation PER_TRANSACTION
/// @assume guardian_honesty: at_most_6_malicious
function invariant_valid_quorum_required() public {
    // No message is executed without >= threshold valid signatures
    // from the CURRENT guardian set.
    bytes32 currentSet = bridgeEth.currentGuardianSetRoot();
    for (uint i = 0; i < bridgeEth.executedMessageCount(); i++) {
        bytes32 msgHash = bridgeEth.executedMessageAt(i);
        uint sigCount = bridgeEth.verifiedSignatureCount(msgHash, currentSet);
        assert(sigCount >= bridgeEth.GUARDIAN_THRESHOLD());
    }
}
```

**What can break it:** signature from rotated-out guardian counted toward quorum, duplicate signatures from same guardian, invalid signatures accepted, threshold evaluated against wrong guardian set, mixed-set quorum crossing.

**Decomposition:**
- Per-chain (safety): signature verification logic checks per-signature set membership against the active guardian set at execution time.
- Cross-check (safety): guardian set root must be identical across chains during the same epoch.

### Message Replay Protection (Safety)

A signed message must be consumable at most once per destination chain.

```solidity
/// @crosschain src=ethereum dst=polygon
/// @observation PER_TRANSACTION
/// @correlation messageHash
function invariant_no_message_replay() public {
    // Every executed message has consumption count exactly 1
    for (uint i = 0; i < bridgePoly.executedMessageCount(); i++) {
        bytes32 msgHash = bridgePoly.executedMessageAt(i);
        assert(bridgePoly.consumptionCount(msgHash) == 1);
    }
}
```

The invariant is per message identity — `consumption_count[messageHash] <= 1`. Sequence number relationships between source and destination are implementation details that may not hold during normal operation (gaps, reorgs).

**What can break it:** message hash collision, consumption flag not set before external call, guardian rotation resets consumption map, same message valid on multiple chains.

**Decomposition:**
- Per-chain (safety): consumption flag set irreversibly before any external call in the execution path.
- Cross-check (safety): no `messageHash` consumed on more than one destination chain.

### Guardian Set Consistency (Safety)

Both chains must agree on the active guardian set during message processing.

```solidity
/// @crosschain src=ethereum dst=polygon
/// @observation PER_TRANSACTION
function invariant_guardian_set_consistent() public {
    // Guardian set root must match on both chains at the time
    // a message is processed. In-flight rotation messages are
    // excluded from this check until delivered.
    bytes32 ethRoot = bridgeEth.currentGuardianSetRoot();
    bytes32 polyRoot = bridgePoly.currentGuardianSetRoot();
    assert(ethRoot == polyRoot);
}
```

**What can break it:** rotation timing mismatch, one chain accepts rotation while other rejects it, partial rotation.

### Fee Preservation (Eventual)

```solidity
/// @crosschain src=ethereum dst=polygon
/// @observation AFTER_ALL_DELIVERED
/// @correlation messageHash
function invariant_bridge_fee_preservation() public {
    uint totalSent = bridgeEth.totalSent();
    uint totalReceived = bridgePoly.totalReceived();
    uint totalFees = bridgeEth.totalFees() + bridgePoly.totalFees();
    assert(totalSent == totalReceived + totalFees);
}
```

---

## Observation Policy Matrix

| Policy | When Checked | Example Use |
|---|---|---|
| `PER_TRANSACTION` | After every state change | Quorum, replay protection, access control |
| `AFTER_FINALITY` | After N block confirmations | Balance equality (avoids reorg false positives) |
| `AFTER_ALL_DELIVERED` | After all emitted messages delivered | Locked==minted, fee preservation |
| `BLOCK_BOUNDED(N)` | Within N blocks of trigger event | Liveness: "message delivered within 100 blocks" |

---

## Invariant Types & Tool Fit

| Type | Observation | Priority Tools |
|---|---|---|
| **Balance conservation** | Eventual | Echidna (fuzz extremes), Halmos (symbolic equality under bounds) |
| **Quorum threshold** | Safety | Halmos (bounded proof), Echidna (boundary fuzz) |
| **Guardian consistency** | Safety | Slither (access gaps), Halmos (storage equality under bounds) |
| **Replay protection** | Safety | Echidna (sequence fuzz), Slither (missing consume-before-call) |
| **Liveness** | Eventually | Echidna (explore delivery paths), manual analysis for deadline bounds |

---

## Invariant Metadata (NatSpec)

```solidity
/// @crosschain src=ethereum dst=polygon
/// @observation AFTER_ALL_DELIVERED
/// @correlation messageHash
/// @assume guardian_honesty: at_most_6_malicious
/// @tools echidna, halmos
/// @severity CRITICAL
/// @timeout 600
function invariant_locked_equals_minted_after_delivery() public { ... }
```

| Tag | Effect |
|---|---|
| `@crosschain src=X dst=Y` | Mark as cross-chain, specify participating chains |
| `@observation POLICY` | When the invariant is checked |
| `@correlation KEY` | How to pair events across chains (e.g., `messageHash`) |
| `@assume KIND: VALUE` | Scope assumption (honesty threshold, ordering model) |
| `@tools` | Restrict which tools probe this invariant |
| `@severity` | Impact label (CRITICAL, HIGH, MEDIUM, LOW) |
| `@timeout` | Per-invariant timeout in seconds |

Tags that cannot be expressed in Solidity (observation, correlation, assumptions) must be provided via NatSpec or the invariant IR. The `.t.sol` parser validates that these are present for cross-chain invariants and warns if defaults are used silently.
