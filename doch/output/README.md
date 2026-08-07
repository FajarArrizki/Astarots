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
| **observed** | One tool produced a counterexample |
| **replayed** | The full causal trace reproduced in the canonical executor |
| **symbolically-confirmed** | A symbolic tool confirmed against an equivalent full-state backend |
| **symbolically-confirmed-under-projected-state** | Halmos confirmed against a declared code/storage projection |

Strength is recorded per trace segment. `aggregate_strength` is the strongest level satisfied by the **entire** causal trace; one projected segment never upgrades unrelated segments. Every symbolic result reports bounds, assumptions, base fingerprints, and any projection manifest.

---

## Evidence Chain (Cross-Chain)

```
CrossChainEdgeCase
├── finding_id
├── invariant
├── verdict: violated | not-observed | inconclusive
├── violation_source: pre_existing_at_snapshot | introduced_by_trace | amplified_by_trace | inconclusive_due_to_missing_relay_data
├── violated_clauses: local monitor rule IDs and/or global_property
├── aggregate_strength
├── segment_strengths: Map[TraceSegmentId, EvidenceStrength]
├── snapshot_set + base_fingerprints
├── relay: dataset_hash + mode + message_ids
├── actor_classification + actor_policy_hash
├── bounds + assumptions
├── action_trace
├── raw_artifacts
└── metadata: run_id, revision, tool versions, effective config hash
```

---

## Evidence Metadata

Every output artifact includes a metadata block sufficient for independent reproduction:

```json
{
  "schema_version": "1.0.0",
  "mode": "mainnet-fork",
  "run_id": "run_2026-08-06_a3f2c1",
  "finding_id": "bridge-edge-0012",
  "project": {
    "revision": "077d0e8",
    "effective_config_hash": "sha256:abc123",
    "invariant_ir_hash": "sha256:invariant-ir"
  },
  "tools": {
    "echidna": "2.1.0",
    "halmos": "0.2.2",
    "slither": "0.11.0",
    "canonical_executor": "revm-1.0"
  },
  "runtime": {
    "astarots": "0.1.0",
    "solc": "0.8.30",
    "foundry": "1.3.1",
    "platform": "darwin-x86_64"
  },
  "snapshot_set": {
    "id": "snapshot-set-0042",
    "coherence_checks_hash": "sha256:coherence",
    "chains": {
      "ethereum": {
        "chain_id": 1,
        "fork_block": 18500000,
        "block_hash": "0x...",
        "state_root": "0x...",
        "fork_cache_hash": "sha256:...",
        "targets": {
          "bridge": {
            "address": "0x...",
            "code_hash": "0x...",
            "proxy_kind": "uups",
            "implementation": "0x...",
            "implementation_code_hash": "0x...",
            "artifact_hash": "sha256:..."
          }
        }
      },
      "polygon": {
        "chain_id": 137,
        "fork_block": 49800000,
        "block_hash": "0x...",
        "state_root": "0x...",
        "fork_cache_hash": "sha256:...",
        "targets": {
          "bridge": {
            "address": "0x...",
            "code_hash": "0x...",
            "proxy_kind": "uups",
            "implementation": "0x...",
            "implementation_code_hash": "0x...",
            "artifact_hash": "sha256:..."
          }
        }
      }
    }
  },
  "relay": {
    "mode": "protocol-valid-synthetic",
    "dataset_hash": "sha256:relay",
    "policy_hash": "sha256:relay-policy",
    "adapter_config_hash": "sha256:relay-adapter",
    "message_ids": ["0x..."]
  },
  "actor_policy_hash": "sha256:actors",
  "execution": {
    "echidna_seed": 42,
    "action_trace_hash": "sha256:trace",
    "environment_hash": "sha256:environment"
  },
  "search": {
    "global_depth": 5,
    "budget_used": 143,
    "budget_total": 200,
    "incomplete_outcomes": [],
    "unsupported_paths": 3
  }
}
```

Raw tool output is preserved by hash after RPC URLs, tokens, and configured secret values are redacted; redaction metadata is recorded with each artifact.

---

## JSON Output

```bash
astarots probe \
  --target ethereum.bridge=0xEthBridge \
  --artifact ethereum.bridge=out/IBridgeEth.sol/IBridgeEth.json \
  --target polygon.bridge=0xPolyBridge \
  --artifact polygon.bridge=out/IBridgePoly.sol/IBridgePoly.json \
  --output json
```

```json
{
  "schema_version": "1.0.0",
  "mode": "mainnet-fork",
  "run_id": "run_2026-08-06_a3f2c1",
  "metadata": {"snapshot_set_id": "snapshot-set-0042"},
  "findings": [
    {
      "finding_id": "bridge-edge-0012",
      "invariant": "invariant_valid_quorum_required",
      "verdict": "violated",
      "violation_source": "introduced_by_trace",
      "violated_clauses": ["global_property"],
      "aggregate_strength": "replayed",
      "segment_strengths": {
        "ethereum:0-2": "symbolically-confirmed-under-projected-state",
        "polygon:3-4": "replayed"
      },
      "bounds": {
        "global_depth": 5,
        "halmos": {
          "loop_unroll": 5,
          "solver_timeout": 120,
          "address_count": 4
        }
      },
      "impact": "CRITICAL",
      "actor_classification": "permissionless",
      "action_trace": [
        {"chain": "ethereum", "call": "signerSetRotation()"},
        {"chain": "ethereum", "call": "submitMessage(bytes)"},
        {"kind": "relay", "message_id": "0x..."},
        {"chain": "polygon", "call": "receiveMessage(bytes)"},
        {"chain": "polygon", "call": "executeAction()"}
      ],
      "evidence": {
        "ethereum": {
          "primary": {
            "tool": "echidna",
            "outcome": "Counterexample",
            "raw_hash": "sha256:abc"
          },
          "confirmation": {
            "tool": "halmos",
            "outcome": "Counterexample",
            "strength": "symbolically-confirmed-under-projected-state",
            "projection_manifest_hash": "sha256:projection",
            "raw_hash": "sha256:def"
          }
        },
        "polygon": {
          "primary": {
            "tool": "echidna",
            "outcome": "Counterexample",
            "raw_hash": "sha256:ghi"
          },
          "canonical_replay_hash": "sha256:polygon-replay"
        }
      },
      "relay": {
        "mode": "protocol-valid-synthetic",
        "dataset_hash": "sha256:relay",
        "policy_hash": "sha256:relay-policy",
        "adapter_config_hash": "sha256:relay-adapter",
        "message_ids": ["0x..."]
      },
      "artifact": "artifacts/bridge-edge-0012.json"
    }
  ],
  "invariant_results": [
    {
      "name": "invariant_valid_quorum_required",
      "verdict": "violated",
      "finding_ids": ["bridge-edge-0012"]
    },
    {
      "name": "invariant_message_expiry",
      "verdict": "not-observed",
      "finding_ids": []
    }
  ],
  "summary": {
    "invariants_total": 2,
    "findings_total": 1,
    "invariants_violated": 1,
    "invariants_not_observed": 1,
    "invariants_inconclusive": 0
  }
}
```

---

## Multi-Chain Replay Contracts

The harness generates two replay artifacts per edge case: one that reproduces the vulnerability (for demonstration and regression) and one that expects the fix to pass (for CI).

### Backend: Deterministic Twin-State

Replay creates fresh forks from the recorded `BaseForkFingerprint` values and replays the exact `ActionTrace` plus environment transitions. Before execution it validates block hashes, state roots, deployed code hashes, proxy implementations, relay dataset hash, and actor policy hash.

Foundry multi-fork is the generated Solidity backend; a twin-state database may be used internally by the canonical executor. Neither backend forks an in-memory mutated fork or uses `vm.mockCall` as a substitute for another chain.

*Reference: [Foundry fork testing](https://getfoundry.sh/forge/fork-testing)*

### VulnerableReproducer.t.sol

Must demonstrate the specific violation without `vm.expectRevert()`. Generated helpers bind deployed interfaces, validate the recorded base fingerprints, and replay one declared relay transition:

```solidity
// artifacts/replay/VulnerableReproducer_Bridge_0012.t.sol
contract VulnerableReproducer_Bridge_0012 is Test {
    uint256 ethFork;
    uint256 polyFork;
    IBridgeEth bridgeEth = IBridgeEth(ETH_BRIDGE_ADDRESS);
    IBridgePoly bridgePoly = IBridgePoly(POLY_BRIDGE_ADDRESS);

    function setUp() public {
        ethFork = vm.createFork(vm.envString("ETH_RPC_URL"), ETH_BLOCK);
        polyFork = vm.createFork(vm.envString("POLY_RPC_URL"), POLY_BLOCK);
        assertBaseFingerprint(ethFork, ETH_FINGERPRINT);
        assertBaseFingerprint(polyFork, POLY_FINGERPRINT);
    }

    function test_reproduce_violation() public {
        vm.selectFork(ethFork);
        replaySourceSteps(ACTION_TRACE_ID);

        bytes memory relayPayload = loadRelayMessage(
            RELAY_DATASET_HASH,
            MESSAGE_ID,
            RelayMode.HistoricalAuthentic
        );

        vm.selectFork(polyFork);
        uint256 beforeCount = bridgePoly.consumptionCount(MESSAGE_ID);
        bridgePoly.receiveMessage(relayPayload);
        bridgePoly.executeAction(MESSAGE_ID);

        assertEq(bridgePoly.consumptionCount(MESSAGE_ID), beforeCount + 1);
        assertTrue(invariantViolatedFor(MESSAGE_ID));
    }
}
```

### FixedRegression.t.sol

The fixed regression uses the same fingerprints, actor policy, relay record, and action trace, but requires an explicit patched target. The generator either installs a supplied implementation on the local fork or binds a user-provided fixed deployment; it never assumes mainnet changed:

```solidity
contract FixedRegression_Bridge_0012 is VulnerableReproducer_Bridge_0012 {
    function setUp() public override {
        super.setUp();
        installPatchedImplementation(
            polyFork,
            FIXED_IMPLEMENTATION,
            FIXED_IMPLEMENTATION_CODE_HASH
        );
    }

    function test_fix_blocks_exploit() public {
        bytes memory relayPayload = replaySourceAndLoadRelay(ACTION_TRACE_ID);
        vm.selectFork(polyFork);
        bridgePoly.receiveMessage(relayPayload);
        vm.expectRevert();
        bridgePoly.executeAction(MESSAGE_ID);
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
│  Aggregate: replayed (canonical full-trace replay)                      │
│  Segment:   ethereum symbolically-confirmed-under-projected-state       │
│  ...                                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```


## References

- [Foundry fork testing](https://getfoundry.sh/forge/fork-testing) — multi-fork replay backend (`createFork`/`selectFork`)
- [Foundry cheatcodes](https://getfoundry.sh/reference/cheatcodes/mock-call/) — `vm.mockCall` (documented for comparison; not used in generated replay contracts)
- [Echidna](https://github.com/crytic/echidna) — fuzzer, primary probe tool for concrete counterexamples
- [Halmos](https://github.com/a16z/halmos) — symbolic engine for bounded confirmation of counterexamples
