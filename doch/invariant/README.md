# Cross-Chain Invariant Specification

Invariants are properties that must hold across all reachable states of a protocol spanning multiple chains. They are the "fence posts" that the harness probes against — when a cross-chain invariant breaks, an edge case has been found that no single-chain tool could detect.

Invariants are defined in `.t.sol` files using standard Foundry test conventions with NatSpec annotations for cross-chain metadata. The harness reads these files, decomposes cross-chain invariants into per-chain sub-invariants, and translates them into tool-specific formats.

---

## Invariant File Convention

An invariant file lives under `test/invariants/` and declares the cross-chain property alongside per-chain contract setup:

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
        // Deploy on both chains via mock relayer
        bridgeEth = new BridgeEth();
        bridgePoly = new BridgePoly();
    }

    /// @crosschain src=ethereum dst=polygon
    function invariant_locked_equals_minted() public {
        uint ethLocked = bridgeEth.totalLocked();
        uint polyMinted = bridgePoly.totalMinted();
        assert(ethLocked == polyMinted);
    }
}
```

The harness identifies cross-chain invariants by the `@crosschain` NatSpec tag. Functions with the `invariant_` prefix but without `@crosschain` are treated as single-chain invariants (supported for completeness, not the primary use case).

---

## Cross-Chain Invariant Patterns

### Balance Conservation

The most fundamental cross-chain invariant: what is locked on the source must equal what is minted (or unlocked) on the destination.

```solidity
/// @crosschain src=ethereum dst=polygon
function invariant_locked_equals_minted() public {
    assert(bridgeEth.totalLocked() == bridgePoly.totalMinted());
}
```

**What can break it:** reentrant lock, double-spend via replay, message ordering manipulation, partial fills that don't reconcile.

**Decomposition:**
- Source: `locked` only increases on valid deposit, only decreases on verified burn.
- Destination: `minted` only increases on verified lock event, only decreases on valid withdrawal.
- Cross-check: every lock event on source has a corresponding mint on destination with equal amount.

### Quorum Threshold

Guardian-based bridges require M-of-N signatures to authorize messages. The threshold logic itself is a rich target for edge cases.

```solidity
/// @crosschain src=ethereum dst=polygon
function invariant_valid_quorum_required() public {
    // No message executes without >= threshold valid signatures
    for (uint i = 0; i < bridgeEth.pendingMessageCount(); i++) {
        bytes32 msgHash = bridgeEth.pendingMessageAt(i);
        uint sigCount = bridgeEth.verifiedSignatureCount(msgHash);
        assert(sigCount >= bridgeEth.GUARDIAN_THRESHOLD());
    }
}
```

**What can break it:** signature from rotated-out guardian counted, duplicate signatures from same guardian, invalid signatures accepted, threshold evaluated against wrong guardian set.

**Decomposition:**
- Per-chain: signature verification logic, guardian set management, threshold comparison.
- Cross-check: guardian set must be identical across chains during the same epoch. A signature from a guardian valid on chain A but not on chain B creates an asymmetry that may be exploitable.

### Guardian Set Consistency

When the guardian set rotates, both chains must agree on the active set. A window where chain A uses set X and chain B uses set Y creates a validation gap.

```solidity
/// @crosschain src=ethereum dst=polygon
function invariant_guardian_set_consistent() public {
    bytes32 ethRoot = bridgeEth.guardianSetRoot();
    bytes32 polyRoot = bridgePoly.guardianSetRoot();
    assert(ethRoot == polyRoot);
}
```

**What can break it:** rotation timing mismatch, one chain accepts rotation while other rejects it, partial rotation (some guardians rotated, some not).

**Decomposition:**
- Per-chain: guardian set update logic, root commitment verification.
- Cross-check: root equality at every block where a cross-chain message was processed.

### Message Replay Protection

A signed message must be executable exactly once, on exactly one destination chain.

```solidity
/// @crosschain src=ethereum dst=polygon
function invariant_no_message_replay() public {
    // Sequence numbers must be monotonic and non-overlapping
    uint srcSeq = bridgeEth.lastProcessedSequence();
    uint dstSeq = bridgePoly.lastReceivedSequence();
    assert(dstSeq <= srcSeq);
}
```

**What can break it:** sequence number reuse across chains, reset during guardian rotation, missing sequence gaps that allow replay of old messages.

**Decomposition:**
- Per-chain: sequence number storage, increment logic, replay guard.
- Cross-check: no (chain_id, sequence) pair processed more than once across any chain.

### Fee Invariance

Cross-chain operations with fees must preserve value through the bridge, accounting for the fee.

```solidity
/// @crosschain src=ethereum dst=polygon
function invariant_bridge_fee_preservation() public {
    uint totalSent = bridgeEth.totalSent();
    uint totalReceived = bridgePoly.totalReceived();
    uint totalFees = bridgeEth.totalFees() + bridgePoly.totalFees();
    assert(totalSent == totalReceived + totalFees);
}
```

**What can break it:** fee parameter changed mid-flight, fee calculated differently on each chain, dust accumulation from rounding.

---

## Invariant Scope

### Primary: Cross-Chain

Properties spanning multiple chains. These are the harness' primary target. Annotated with `@crosschain` and references to chain aliases registered in the chain config.

### Supported for Completeness: Single-Chain

Invariants without `@crosschain` are treated as single-chain (degenerate case: one chain, identity recomposition). These follow the standard Foundry invariant convention and are compatible with all tools.

---

## Invariant Types & Tool Selection

The harness classifies invariants to decide which tools to run and in what order:

| Type | Example | Priority Tools |
|---|---|---|
| **Balance conservation** | `locked_A == minted_B` | Halmos (symbolic equality), Echidna (fuzz extremes) |
| **Quorum threshold** | `valid_sigs >= M` | Halmos (prove bound), Echidna (boundary fuzz) |
| **Guardian consistency** | `guardian_set_A == guardian_set_B` | Slither (access control gaps), Halmos (storage equality) |
| **Replay protection** | `sequence unique per (src,dst)` | Echidna (sequence fuzzing), Slither (missing checks) |
| **State machine** | `status transition valid` | Echidna (sequence exploration) |
| **Access control** | `only guardian can authorize` | Slither (missing modifiers), Echidna (trigger bypass) |

---

## Invariant Metadata

NatSpec tags for harness configuration:

```solidity
/// @crosschain src=ethereum dst=polygon
/// @tools echidna, halmos
/// @severity CRITICAL
/// @timeout 600
function invariant_locked_equals_minted() public { ... }
```

| Tag | Effect |
|---|---|
| `@crosschain` | Mark as cross-chain. `src` and `dst` reference chain aliases |
| `@tools` | Restrict which tools probe this invariant (default: all) |
| `@severity` | Override impact label (CRITICAL, HIGH, MEDIUM, LOW) |
| `@timeout` | Per-invariant timeout in seconds |

---

## Constraints from Invariants

Cross-chain invariants generate constraints that feed the search. For a quorum invariant `valid_sigs >= THRESHOLD`, the harness extracts:

```
Constraint(
    kind=STATE_VAR,
    target="verified_signatures",
    value=Range(0, THRESHOLD),
    chain="ethereum",
    source="invariant:valid_quorum_required"
)
```

The beam search is directed toward paths where `verified_signatures` is near `THRESHOLD` — the boundary where edge cases live.
