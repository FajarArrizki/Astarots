# Usage

Astarots is invoked from the command line. Cross-chain invariant testing requires chain configuration before the first probe. The workflow follows: define chains, write cross-chain invariants in Solidity, run the harness, and review per-chain traces.

---

## Quick Start

Given a Foundry project with bridge contracts on Ethereum and Polygon, and invariants at `test/invariants/BridgeInvariants.t.sol`:

```bash
# Step 1: Register chains
astarots chain add ethereum --rpc $ETH_RPC_URL --chain-id 1
astarots chain add polygon  --rpc $POLY_RPC_URL --chain-id 137

# Step 2: Run probe (explicit chain-to-contract binding)
astarots probe \
    --target ethereum=src/BridgeEth.sol \
    --target polygon=src/BridgePoly.sol \
    --chains ethereum,polygon \
    --invariants test/invariants/ \
    --tools echidna,halmos,slither \
    --max-depth 4
```

This command compiles both contracts, decomposes cross-chain invariants into per-chain sub-invariants, runs adaptive beam search with causal coordination across chains, recombines findings, and prints per-chain traces with cross-chain correlation.

---

## Commands

### `chain`

Manage chain configurations. Chains must be registered before probing.

```
astarots chain add <alias> --rpc <URL> [--chain-id <ID>]
astarots chain rm  <alias>
astarots chain list
```

Example:

```bash
astarots chain add ethereum  --rpc $ETH_RPC_URL  --chain-id 1
astarots chain add polygon   --rpc $POLY_RPC_URL --chain-id 137
astarots chain add arbitrum  --rpc $ARB_RPC_URL  --chain-id 42161
```

### `probe`

Run guided search against cross-chain invariants.

```
astarots probe [OPTIONS]

Options:
  --target CHAIN=CONTRACT    Explicit chain-to-contract binding, repeatable
                              e.g. --target ethereum=src/BridgeEth.sol
  --invariants PATH          Directory containing .t.sol invariant files
  --chains LIST              Comma-separated chain aliases, e.g. "ethereum,polygon"
  --tools LIST               Comma-separated tool names (default: echidna,halmos,slither)
  --max-depth N              Maximum search depth per chain (default: 4)
  --beam LIST                Beam widths per depth, e.g. "4,3,2,1" (default: adaptive)
  --max-states N             Hard cap on total explored states per chain (default: 200)
  --timeout N                Per-invariant timeout in seconds (default: 600)
  --output FORMAT            Output format: console, json (default: console)
  --focus FUNCTION           Only probe a specific invariant function
  --dry-run                  Validate configuration without running tools
```

### Target Binding

Target contracts are bound to chains explicitly using `chain=contract` syntax, not positionally:

```bash
astarots probe \
    --target ethereum=src/BridgeEth.sol \
    --target polygon=src/BridgePoly.sol \
    --chains ethereum,polygon \
    --invariants test/invariants/
```

Positional matching (order of `--target` matching order of `--chains`) is deprecated and emits a warning.

### `replay`

Re-execute a previously found cross-chain edge case. Uses deterministic twin-state replay (multi-fork or twin database).

```
astarots replay [OPTIONS]

Options:
  --edge-case PATH           Path to a saved EdgeCase JSON file
  --target CHAIN=CONTRACT    Contracts to replay against (repeatable)
  --chains LIST              Chain aliases for replay
  --output PATH              Where to write the replay trace
```

### `list-tools`

Show which tools are available, their versions, and capabilities:

```
astarots list-tools
```

Output:

```
  echidna    2.1.0    fuzz,replay    /usr/local/bin/echidna
  halmos     0.2.2    sym,confirm    /usr/local/bin/halmos
  slither    0.11.0   static,hint    /usr/local/bin/slither
```

### `init`

Scaffold a cross-chain invariant test file.

```
astarots init --target ethereum=src/BridgeEth.sol --target polygon=src/BridgePoly.sol
```

Creates `test/invariants/BridgeInvariants.t.sol` with skeleton cross-chain invariant functions. The developer fills in assertion bodies.

---

## Precedence

Configuration is resolved in this order, with later sources overriding earlier ones:

1. Built-in defaults
2. `astarots.toml` project configuration
3. `@` NatSpec tags in `.t.sol` invariant files
4. Environment variables (`$VAR` references in config values)
5. CLI flags (highest precedence)

---

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | No violations found (verdict: not-observed for all invariants) |
| 1 | Violation found (verdict: violated for at least one invariant) |
| 2 | Inconclusive (timeout, tool error, or incomplete search) |
| 3 | Invalid configuration (missing chains, invalid invariant IR, etc.) |
| 4 | Tool execution error (all probes failed) |

---

## RPC Secret Protection

RPC URLs containing secrets (API keys, tokens) are referenced via environment variables:

```toml
[chains.ethereum]
rpc_url = "$ETH_RPC_URL"  # resolved from environment at runtime
```

Direct embedding of secrets in config files or CLI flags is discouraged. The harness never logs RPC URLs.

---

## Cross-Chain Invariant Workflow

### Writing Invariants

Cross-chain invariants are standard Foundry test functions annotated with NatSpec tags:

```solidity
/// @crosschain src=ethereum dst=polygon
/// @observation AFTER_ALL_DELIVERED
/// @correlation messageHash
/// @tools echidna, halmos
/// @severity CRITICAL
function invariant_locked_equals_minted() public {
    assert(bridgeEth.totalLocked() == bridgePoly.totalMinted());
}
```

### Running a Subset of Invariants

```bash
astarots probe \
    --target ethereum=src/BridgeEth.sol \
    --target polygon=src/BridgePoly.sol \
    --chains ethereum,polygon \
    --focus invariant_locked_equals_minted
```

### Running a Subset of Tools

Slither first (fast, static), findings seed Echidna (slower, dynamic):

```bash
astarots probe \
    --target ethereum=src/BridgeEth.sol \
    --target polygon=src/BridgePoly.sol \
    --chains ethereum,polygon \
    --tools slither,echidna
```

---

## Cross-Chain Configuration

For repeated runs, create `astarots.toml`:

```toml
[default]
invariants = "test/invariants/"
chains = ["ethereum", "polygon"]
tools = ["echidna", "halmos", "slither"]
max_depth = 4
beam_widths = [4, 3, 2, 1]
max_states = 200
timeout = 600

[targets]
ethereum = "src/BridgeEth.sol"
polygon = "src/BridgePoly.sol"

[chains.ethereum]
rpc_url = "$ETH_RPC_URL"
chain_id = 1

[chains.polygon]
rpc_url = "$POLY_RPC_URL"
chain_id = 137

[tools.echidna]
timeout = 300
test_limit = 50000

[tools.halmos]
timeout = 600
solver_timeout = 120

[tools.slither]
detectors = ["reentrancy", "unchecked-transfer", "access-control"]
```

---

## Output Interpretation

Cross-chain probe output shows per-chain findings with verdict and evidence strength:

```
Cross-Chain Invariant: invariant_locked_equals_minted
═══════════════════════════════════════════════════
Verdict:   violated
Evidence:  symbolically-confirmed (echidna + halmos, bounds: L=5,T=120)
Depth:     3 (ethereum) + 2 (polygon) → cross-chain confirmed
Impact:    CRITICAL — unauthorized mint on destination

Source Chain (ethereum):
  [setDelegate(attacker), lock(100 ETH), rotateGuardians()]
  └─ Found by: echidna → halmos (symbolically-confirmed)

Destination Chain (polygon):
  [mint(100 ETH), withdraw(100 ETH)]
  └─ Found by: echidna (observed)

Cross-Chain Correlation:
  └─ Guardian rotation on ethereum skipped verification on polygon
  └─ Message signed by 7 old guardians + 6 new guardians = 13 total
  └─ Neither old set (19) nor new set (19) individually reached quorum
  └─ But combined count crossed threshold (13)

Constraints:
  • Guardian rotation must be in-flight (old set not yet expired)
  • Attacker controls delegate address on source chain
  • 7 signatures from rotated-out guardians still accepted
```

Per-chain traces are labeled with `[chain]` annotations. The cross-chain correlation section shows how findings on two independent chains combine into a protocol-level vulnerability.

---

## Development Loop

1. **Define chains** — `astarots chain add` for each chain in the protocol.
2. **Define invariants** — write `.t.sol` files with `@crosschain` annotations and observation policy.
3. **Quick static scan** — `astarots probe --tools slither` for fast cross-chain pattern detection.
4. **Shallow fuzz** — `astarots probe --tools echidna --max-depth 2` for dynamic exploration.
5. **Deep search** — `astarots probe --tools echidna,halmos --max-depth 4` for thorough edge case discovery.
6. **Replay and fix** — `astarots replay` to verify findings; run `FixedRegression` to confirm the patch.
