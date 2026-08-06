# Output & Evidence

Every cross-chain edge case found by the harness is backed by reproducible per-chain evidence. The output layer presents findings with per-chain trace annotations, preserves raw tool output from each chain, and generates multi-chain replay contracts.

---

## Evidence Chain (Cross-Chain)

When the harness reports a cross-chain edge case, it includes the full evidence chain from both sides:

```
CrossChainEdgeCase
├── invariant:          which cross-chain invariant was violated
├── depth:              combined search depth across chains
├── chains:             [ethereum, polygon]
├── cross_violation:    the cross-chain property that broke
├── per_chain:
│   ├── ethereum:
│   │   ├── sequence:   call sequence on source chain
│   │   ├── constraints:preconditions on source chain
│   │   └── evidence:
│   │       ├── primary:       tool that found source-chain violation
│   │       └── confirmation:  independent verification
│   └── polygon:
│       ├── sequence:   call sequence on destination chain
│       ├── constraints:preconditions on destination chain
│       └── evidence:
│           ├── primary:       tool that found dest-chain violation
│           └── confirmation:  independent verification
├── correlation:        how the two chains' findings connect
├── confidence:         proven | reproduced
└── artifact:           path to saved JSON for replay
```

This answers three questions:

- **What happened on each chain?** — per-chain sequences, constraints, and impact.
- **How do they connect?** — the correlation that makes this a true cross-chain edge case.
- **Can I trust it?** — independent confirmation per chain, and cross-chain consistency check.

---

## Console Output

Cross-chain probe output shows per-chain findings and the correlation that ties them together:

```
┌─ BridgeInvariants ─────────────────────────────────────────────────────┐
│                                                                         │
│  invariant_valid_quorum_required                                        │
│  ═══════════════════════════════                                        │
│  Status:   FAIL — PROVEN                                                │
│  Depth:    eth:3 + poly:2 → cross-chain confirmed                       │
│  Impact:   CRITICAL — unauthorized cross-chain message execution        │
│                                                                         │
│  Ethereum:                                                               │
│    [guardianRotation(), submitMessage(data), verifySignatures(13)]       │
│    └─ echidna → halmos  (sig threshold crossed with mixed guardian set) │
│                                                                         │
│  Polygon:                                                                │
│    [receiveMessage(data), executeAction()]                               │
│    └─ echidna  (message accepted with insufficient per-set quorum)      │
│                                                                         │
│  Cross-Chain Correlation:                                                │
│    └─ Guardian rotation in-flight on ethereum                            │
│    └─ 7 old-guardian sigs + 6 new-guardian sigs = 13 (passed threshold) │
│    └─ Polygon verifies against old set → only 7 valid sigs (< 13)       │
│    └─ BUT polygon's verifySignatures doesn't check set membership per-sig│
│    └─ Result: message accepted on polygon with invalid quorum            │
│                                                                         │
│  Constraints:                                                            │
│    • [eth] Guardian rotation must be pending (old set not expired)       │
│    • [eth] 13 signatures: 7 from old guardians, 6 from new              │
│    • [poly] verifySignatures counts sigs globally, not per guardian set  │
│                                                                         │
│  Evidence:                                                               │
│    Primary (eth):     echidna  →  counterexample at seq #4210            │
│    Confirmation (eth): halmos  →  SAT, mixed-quorum path is reachable    │
│    Primary (poly):    echidna  →  counterexample at seq #873             │
│    Correlation:       recombiner → per-set quorum < threshold             │
│    Confidence:        PROVEN                                              │
│    Replay:            astarots replay --edge-case bridge-edge-0012.json   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

Trace annotations use `[chain]` labels and cross-tool enrichment markers. The correlation section is the critical addition — it explains why two per-chain findings that might appear benign in isolation combine into a protocol-level vulnerability.

---

## JSON Output

```bash
astarots probe --target src/BridgeEth.sol,src/BridgePoly.sol --chains eth,poly --output json
```

```json
{
  "targets": {
    "ethereum": "src/BridgeEth.sol",
    "polygon": "src/BridgePoly.sol"
  },
  "invariants": [
    {
      "name": "invariant_valid_quorum_required",
      "status": "proven",
      "depth": {"ethereum": 3, "polygon": 2},
      "impact": "CRITICAL",
      "chains": ["ethereum", "polygon"],
      "per_chain": {
        "ethereum": {
          "sequence": [
            {"call": "guardianRotation()", "source": "slither→echidna"},
            {"call": "submitMessage(bytes)", "source": "echidna"},
            {"call": "verifySignatures(uint256)", "args": [13], "source": "echidna→halmos"}
          ],
          "constraints": [
            {"kind": "STATE_VAR", "target": "guardianSet", "value": "rotation_pending"},
            {"kind": "CROSS_CHAIN", "target": "signature_count", "value": 13}
          ]
        },
        "polygon": {
          "sequence": [
            {"call": "receiveMessage(bytes)", "source": "echidna"},
            {"call": "executeAction()", "source": "echidna"}
          ],
          "constraints": [
            {"kind": "ACCESS", "target": "verifySignatures", "value": "no_per_set_check"}
          ]
        }
      },
      "correlation": {
        "type": "mixed_guardian_quorum",
        "old_guardian_sigs": 7,
        "new_guardian_sigs": 6,
        "threshold": 13,
        "violation": "per-set quorum not enforced on destination chain"
      },
      "evidence": {
        "ethereum": {
          "primary": {"tool": "echidna", "output": "...", "trace": "..."},
          "confirmation": {"tool": "halmos", "result": "confirmed"}
        },
        "polygon": {
          "primary": {"tool": "echidna", "output": "...", "trace": "..."}
        }
      },
      "artifact": "artifacts/bridge-edge-0012.json"
    }
  ],
  "summary": {
    "total": 4,
    "passed": 1,
    "proven": 2,
    "reproduced": 1
  }
}
```

---

## Multi-Chain Replay Contract

Each cross-chain edge case generates a replay contract that reproduces the full multi-chain sequence using Foundry's `vm` cheatcodes to simulate the second chain:

```solidity
// artifacts/replay/Bridge_edge_0012.t.sol
// Auto-generated by Astarots — do not edit manually
// Original finding: invariant_valid_quorum_required
// Chains: ethereum + polygon | Confidence: PROVEN

pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {BridgeEth} from "../../src/BridgeEth.sol";
import {BridgePoly} from "../../src/BridgePoly.sol";

contract Replay_Bridge_edge_0012 is Test {
    BridgeEth bridgeEth;
    BridgePoly bridgePoly;

    address[] oldGuardians;
    address[] newGuardians;

    function setUp() public {
        // Deploy both contracts
        bridgeEth = new BridgeEth();
        bridgePoly = new BridgePoly();

        // Set up guardian sets
        // ... (harness generates exact guardian addresses)
    }

    function test_replay_cross_chain_edge() public {
        // === ETHEREUM SIDE ===

        // Step 1: Initiate guardian rotation (old set not yet expired)
        bridgeEth.initiateGuardianRotation(newGuardians);

        // Step 2: Submit message signed by mixed guardian set
        bytes memory message = abi.encode(/* ... */);
        bytes[] memory signatures = new bytes[](13);
        // 7 sigs from old guardians, 6 from new guardians
        // ... (harness generates exact signatures)
        bridgeEth.submitMessage(message, signatures);

        // Step 3: Verify — crosses threshold with combined sigs
        bridgeEth.verifySignatures(13);

        // === CROSS-CHAIN: Mock relayer ===
        bytes32 messageHash = keccak256(message);
        vm.mockCall(
            address(bridgePoly),
            abi.encodeWithSignature("messageHash()"),
            abi.encode(messageHash)
        );

        // === POLYGON SIDE ===

        // Step 4: Receive message — should require per-set quorum
        //        but doesn't check set membership per signature
        bridgePoly.receiveMessage(message, signatures);

        // Step 5: Execute action — this should fail but succeeds
        vm.expectRevert();  // if fixed, should revert here
        bridgePoly.executeAction(message);
    }
}
```

The replay contract is a self-contained Foundry test that reproduces the cross-chain edge case using `vm.mockCall` to simulate the relayer between chains. Running it verifies that the edge case still reproduces, and that fixes prevent the violation.

---

## Confidence Model

| Confidence | Meaning | Criteria |
|---|---|---|
| **PROVEN** | Formally verified across chains | Per-chain counterexample confirmed by a second tool on at least one chain, AND cross-chain correlation formally checked by the recombiner |
| **REPRODUCED** | Found and replayed successfully | Multi-chain sequence executes and triggers the cross-chain violation, but no independent symbolic verification exists |
