# Output & Evidence

Every cross-chain edge case found by the harness is backed by reproducible evidence with explicit bounds, assumptions, and tool metadata. The output layer separates **verdict** (what was observed) from **evidence strength** (how it was confirmed) and preserves everything needed to reproduce the finding.

---

## Verdict vs Evidence Strength

The harness does not use absolute labels like SAFE, PASSED, or PROVEN. Every finding carries a **verdict** (the factual observation) and an **evidence strength** (how well it is supported):

### Verdict

| Verdict | Meaning |
|---|---|
| **violated** | The invariant was broken — a counterexample exists |
| **not-observed** | No violation was found within the search bounds |
| **inconclusive** | Search was incomplete (timeout, tool error, unsupported path) |

### Evidence Strength

| Strength | Criteria |
|---|---|
| **observed** | A single tool produced a counterexample |
| **replayed** | The counterexample was independently replayed and reproduced |
| **symbolically-confirmed** | A second tool with symbolic capabilities confirmed the path under explicit bounds and assumptions |

Every `symbolically-confirmed` finding must report the bounds under which confirmation was obtained (loop unrolling depth, solver timeout, address count, assumptions).

---

## Evidence Chain (Cross-Chain)

```
CrossChainEdgeCase
├── finding_id:         stable identifier for this finding
├── invariant:          which cross-chain invariant was violated
├── verdict:            violated | not-observed | inconclusive
├── evidence_strength:  observed | replayed | symbolically-confirmed
├── bounds:             depth, loop unrolling, solver timeout, assumptions
├── chains:             [ethereum, polygon]
├── per_chain:          ... (per-chain evidence as below)
├── correlation:        how the two chains' findings connect
├── artifact:           path to saved JSON for replay
└── metadata:           run_id, commit, tool versions, config hash, seed
```

---

## Evidence Metadata

Every output artifact includes a metadata block sufficient for independent reproduction:

```json
{
  "schema_version": "1.0.0",
  "mode": "mainnet-fork",  # invariants checked against forked state, not fresh deploy
  "run_id": "run_2026-08-06_a3f2c1",
  "finding_id": "bridge-edge-0012",
  "project": {
    "name": "astarots",
    "commit": "077d0e8",
    "branch": "main"
  },
  "tools": {
    "echidna": "2.1.0",
    "halmos": "0.2.2",
    "slither": "0.11.0"
  },
  "config": {
    "hash": "sha256:abc123...",
    "beam_widths": [4, 3, 2, 1],
    "max_depth": 4,
    "max_states": 200,
    "timeout": 600
  },
  "chains": {
    "ethereum": {
      "chain_id": 1,
      "fork_block": 18500000,
      "rpc_hash": "sha256:def456..."
    },
    "polygon": {
      "chain_id": 137,
      "fork_block": 49800000,
      "rpc_hash": "sha256:ghi789..."
    }
  },
  "execution": {
    "echidna_seed": 42,
    "command": "astarots probe --target ... --chains eth,poly",
    "env_hash": "sha256:jkl012..."
  },
  "assumptions": {
    "guardian_honesty": "at_most_6_malicious",
    "message_ordering": "ordered_by_sequence"
  },
  "search": {
    "budget_used": 143,
    "budget_total": 200,
    "timeout_occurred": false,
    "tool_errors": [],
    "unsupported_paths": 3,
    "skipped": 0
  }
}
```

Raw tool output is preserved in `Evidence.raw` for each chain, with file hashes for integrity verification.

---

## JSON Output

```bash
astarots probe --target ethereum=src/BridgeEth.sol --target polygon=src/BridgePoly.sol --output json
```

```json
{
  "schema_version": "1.0.0",
  "mode": "mainnet-fork",  # invariants checked against forked state, not fresh deploy
  "run_id": "run_2026-08-06_a3f2c1",
  "metadata": { "...": "..." },
  "invariants": [
    {
      "name": "invariant_valid_quorum_required",
      "verdict": "violated",
      "evidence_strength": "symbolically-confirmed",
      "bounds": {
        "depth": {"ethereum": 3, "polygon": 2},
        "halmos": {"loop_unroll": 5, "solver_timeout": 120, "address_count": 4}
      },
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
          ],
          "evidence": {
            "primary": {
              "tool": "echidna",
              "outcome": "Counterexample",
              "raw_hash": "sha256:abc..."
            },
            "confirmation": {
              "tool": "halmos",
              "outcome": "Counterexample",
              "raw_hash": "sha256:def..."
            }
          }
        },
        "polygon": {
          "sequence": [
            {"call": "receiveMessage(bytes)", "source": "echidna"},
            {"call": "executeAction()", "source": "echidna"}
          ],
          "constraints": [
            {"kind": "ACCESS", "target": "verifySignatures", "value": "no_per_set_check"}
          ],
          "evidence": {
            "primary": {
              "tool": "echidna",
              "outcome": "Counterexample",
              "raw_hash": "sha256:ghi..."
            }
          }
        }
      },
      "correlation": {
        "type": "mixed_guardian_quorum",
        "old_guardian_sigs": 7,
        "new_guardian_sigs": 6,
        "threshold": 13,
        "violation": "per-set quorum not enforced on destination chain"
      },
      "artifact": "artifacts/bridge-edge-0012.json",
      "raw": {
        "ethereum": "artifacts/raw/run_a3f2c1_eth_echidna.json",
        "polygon": "artifacts/raw/run_a3f2c1_poly_echidna.json"
      }
    }
  ],
  "summary": {
    "total": 4,
    "violated": 2,
    "not_observed": 1,
    "inconclusive": 1
  }
}
```

---

## Multi-Chain Replay Contracts

The harness generates two replay artifacts per edge case: one that reproduces the vulnerability (for demonstration and regression) and one that expects the fix to pass (for CI).

### Backend: Deterministic Twin-State

Replay uses **deterministic twin-state execution**, not `vm.mockCall`. Two approaches are supported:

**Approach A: Foundry multi-fork** — `vm.createFork` / `vm.selectFork` with pinned block numbers. Each chain gets its own fork. The harness generates explicit relay transitions that read state from one fork and apply it to the other.

**Approach B: Twin-state database** — Two separate EVM state databases managed by the harness. Messages are serialized as explicit state transitions between databases. This is the default for fully local execution; multi-fork is used when RPC-backed state is needed.

*Reference: [Foundry fork testing](https://getfoundry.sh/forge/fork-testing)*

### VulnerableReproducer.t.sol

Must demonstrate the violation. Does **not** use `vm.expectRevert()` — the violation is expected to succeed. The test is a positive demonstration that the exploit works under the given constraints. If the exploit succeeds, the invariant is violated and the test passes (confirming the vulnerability):

```solidity
// artifacts/replay/VulnerableReproducer_Bridge_0012.t.sol
// Finding: invariant_valid_quorum_required
// Verdict: violated | Evidence: symbolically-confirmed

contract VulnerableReproducer_Bridge_0012 is Test {
    uint256 ethFork;
    uint256 polyFork;

    function setUp() public {
        ethFork = vm.createFork("eth", 18500000);
        polyFork = vm.createFork("poly", 49800000);
    }

    function test_reproduce_violation() public {
        // ETHEREUM: initiate rotation + submit mixed-signature message
        vm.selectFork(ethFork);
        bridgeEth.initiateGuardianRotation(newGuardians);
        bridgeEth.submitMessage(message, mixedSignatures);

        // RELAY TRANSITION: commit ethereum state, deliver to polygon
        bytes memory relayPayload = captureAndRelay();

        // POLYGON: receive and execute — should fail but succeeds
        vm.selectFork(polyFork);
        bridgePoly.receiveMessage(relayPayload);
        // This call should revert (per-set quorum not met), but doesn't.
        // We EXPECT it to succeed to demonstrate the vulnerability.
        bridgePoly.executeAction(message);
        // If we get here, the exploit succeeded — invariant is violated.
        assert(bridgePoly.executedMessageCount() > 0);
    }
}
```

### FixedRegression.t.sol

Must pass after the fix is applied. Uses `vm.expectRevert()` to confirm the fix blocks the exploit:

```solidity
// artifacts/replay/FixedRegression_Bridge_0012.t.sol

contract FixedRegression_Bridge_0012 is Test {
    // ... same setup ...

    function test_fix_blocks_exploit() public {
        // ... same setup steps ...

        // After fix: executeAction should REVERT (quorum not met per-set)
        vm.selectFork(polyFork);
        bridgePoly.receiveMessage(relayPayload);
        vm.expectRevert();  // fix blocks the exploit
        bridgePoly.executeAction(message);
    }
}
```

---

## Console Output

Console output uses verdict + evidence strength labels:

```
┌─ BridgeInvariants ─────────────────────────────────────────────────────┐
│                                                                         │
│  invariant_valid_quorum_required                                        │
│  ═══════════════════════════════                                        │
│  Verdict:   violated                                                    │
│  Evidence:  symbolically-confirmed (echidna + halmos, bounds: L=5,T=120)│
│  Impact:    CRITICAL                                                    │
│  ...                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```


## References

- [Foundry fork testing](https://getfoundry.sh/forge/fork-testing) — multi-fork replay backend (`createFork`/`selectFork`)
- [Foundry cheatcodes](https://getfoundry.sh/reference/cheatcodes/mock-call/) — `vm.mockCall` (documented for comparison; not used in generated replay contracts)
- [Echidna](https://github.com/crytic/echidna) — fuzzer, primary probe tool for concrete counterexamples
- [Halmos](https://github.com/a16z/halmos) — symbolic engine for bounded confirmation of counterexamples
