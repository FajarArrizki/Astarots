# Output & Evidence

Every edge case found by the harness is backed by reproducible evidence. The output layer is responsible for presenting findings clearly, preserving the raw data that produced them, and enabling replay.

---

## Evidence Chain

When the harness reports an edge case, it includes the full chain of evidence that led to it:

```
EdgeCase
├── invariant:          which invariant was violated
├── depth:              how deep in the search tree
├── sequence:           exact call sequence that triggers the violation
├── constraints:        preconditions required to reproduce
├── impact:             what breaks and the estimated severity
├── evidence:
│   ├── primary:        the tool that first found the violation
│   │   ├── tool:       echidna | halmos | slither
│   │   ├── output:     raw tool output
│   │   └── trace:      normalized call trace
│   └── confirmation:   the tool that independently verified
│       ├── tool:       echidna | halmos | slither
│       ├── output:     raw tool output
│       └── result:     confirmed | refuted | inconclusive
├── confidence:         proven | reproduced | suspected
└── artifact:           path to saved JSON for replay
```

This structure answers three questions an auditor or developer will ask:

- **What happened?** — the invariant, sequence, and impact.
- **How was it found?** — the primary tool and its raw output.
- **Can I trust it?** — the independent confirmation and confidence level.

---

## Console Output

The default output format is a table rendered to the terminal, designed for rapid scanning during development:

```
┌─ VaultInvariants ─────────────────────────────────────────────────────────┐
│                                                                            │
│  invariant_no_overdraft                                                    │
│  ════════════════════════                                                  │
│  Status:   PASS                                                            │
│  Tools:    echidna (50000 seqs), halmos (UNSAT), slither (clean)           │
│                                                                            │
│  invariant_deposit_equals_shares                                           │
│  ═════════════════════════════                                             │
│  Status:   FAIL — PROVEN                                                   │
│  Depth:    3                                                               │
│  Impact:   HIGH — users can withdraw more than deposited                   │
│                                                                            │
│  Sequence:                                                                 │
│    1. setDelegate(attacker)                          [echidna]              │
│    2. deposit(100 ether)                             [echidna]              │
│    3. rebalance()  ← oracle reverts at token[2]      [slither → echidna]   │
│    4. withdraw(50 ether)  ← uses stale price         [echidna → halmos]    │
│                                                                            │
│  Constraints:                                                              │
│    • delegate must be attacker-controlled                                  │
│    • oracle must be stale (>60s since last update)                         │
│    • fee must be set to maximum (10000 bps)                                │
│    • rebalance must partially fail (token[2] reverts)                      │
│                                                                            │
│  Evidence:                                                                 │
│    Primary:      echidna  →  counterexample found at seq #48210            │
│    Confirmation: halmos   →  SAT, path is symbolically reachable           │
│    Confidence:   PROVEN                                                     │
│    Replay:       astarots replay --edge-case vault-edge-0042.json          │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘
```

The trace annotations (`[echidna]`, `[slither → echidna]`, `[echidna → halmos]`) show which tool discovered each step in the sequence, making the cross-tool enrichment visible.

---

## JSON Output

For CI integration and programmatic consumption:

```bash
astarots probe --target src/Vault.sol --output json
```

Produces:

```json
{
  "target": "src/Vault.sol",
  "invariants": [
    {
      "name": "invariant_deposit_equals_shares",
      "status": "proven",
      "depth": 3,
      "impact": "HIGH",
      "sequence": [
        {"call": "setDelegate(address)", "args": ["0xattacker"], "source": "echidna"},
        {"call": "deposit(uint256)", "args": ["100000000000000000000"], "source": "echidna"},
        {"call": "rebalance()", "args": [], "source": "slither→echidna"},
        {"call": "withdraw(uint256)", "args": ["50000000000000000000"], "source": "echidna→halmos"}
      ],
      "constraints": [
        {"kind": "STATE_VAR", "target": "delegate", "value": "attacker"},
        {"kind": "TIMING", "target": "oracle", "value": "staleness > 60s"},
        {"kind": "STATE_VAR", "target": "fee", "value": 10000}
      ],
      "evidence": {
        "primary": {"tool": "echidna", "output": "...", "trace": "..."},
        "confirmation": {"tool": "halmos", "result": "confirmed"}
      },
      "artifact": "artifacts/vault-edge-0042.json"
    }
  ],
  "summary": {
    "total": 3,
    "passed": 1,
    "proven": 1,
    "reproduced": 1
  }
}
```

---

## Replay Contract

Each edge case artifact can be replayed as a standalone Foundry test. The harness generates a replay contract:

```solidity
// artifacts/replay/Vault_edge_0042.t.sol
// Auto-generated by Astarots — do not edit manually
// Original finding: invariant_deposit_equals_shares
// Depth: 3 | Confidence: PROVEN

pragma solidity ^0.8.0;

import {Test} from "forge-std/Test.sol";
import {Vault} from "../../src/Vault.sol";

contract Replay_Vault_edge_0042 is Test {
    Vault vault;
    address attacker = address(0xbad);

    function setUp() public {
        // Reproduce constraints
        vm.warp(block.timestamp + 61);  // oracle staleness > 60s
        vault = new Vault();
        vault.setFee(10000);             // fee = max
        vault.setDelegate(attacker);     // delegate = attacker
    }

    function test_replay_edge_case() public {
        // Step 1: deposit
        vm.prank(attacker);
        vault.deposit{value: 100 ether}();

        // Step 2: rebalance (partial failure)
        // Mock oracle revert for token[2]
        vm.mockCallRevert(
            address(vault.oracle()),
            abi.encodeWithSignature("getPrice(uint256)", 2),
            "Oracle: stale price"
        );
        vault.rebalance();

        // Step 3: withdraw — this should trigger the violation
        vm.prank(attacker);
        vm.expectRevert();  // if fixed, this should revert
        vault.withdraw(50 ether);
    }
}
```

The replay contract is a self-contained Foundry test. Running it verifies that:

- The edge case still reproduces against the current contract (regression check).
- A fix successfully prevents the violation (change `expectRevert` to check the invariant).

---

## Confidence Model

| Confidence | Meaning | Criteria |
|---|---|---|
| **PROVEN** | Formally verified by a second tool | Primary tool found counterexample, secondary tool confirmed it symbolically or through a different method |
| **REPRODUCED** | Found by one tool, replayed successfully | The sequence executes and triggers the violation, but no independent symbolic verification exists |
| **SUSPECTED** | Flagged by static analysis only | A pattern was identified but no concrete violation was produced |

PROVEN findings are suitable for audit reports. REPRODUCED findings warrant investigation and possible fixes. SUSPECTED findings are informational — they guide further manual review.
