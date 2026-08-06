# Usage

Astarots is invoked from the command line. Cross-chain invariant testing requires chain configuration before the first probe. The workflow follows: define chains, write cross-chain invariants in Solidity, run the harness, and review per-chain traces.

---

## Quick Start

Given a Foundry project with bridge contracts on Ethereum and Polygon, and invariants at `test/invariants/BridgeInvariants.t.sol`:

```bash
# Step 1: Register chains
astarots chain add ethereum --rpc $ETH_RPC_URL
astarots chain add polygon  --rpc $POLY_RPC_URL

# Step 2: Run probe
astarots probe \
    --target src/BridgeEth.sol,src/BridgePoly.sol \
    --invariants test/invariants/ \
    --chains ethereum,polygon \
    --tools echidna,halmos,slither \
    --max-depth 4
```

This command compiles both contracts, decomposes cross-chain invariants into per-chain sub-invariants, runs adaptive beam search on each chain independently, recombines findings, and prints per-chain traces with cross-chain correlation.

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

Run guided search against cross-chain invariants. Requires at least two chains via `--chains`.

```
astarots probe [OPTIONS]

Options:
  --target PATH,...        Comma-separated Solidity files per chain (order matches --chains)
  --invariants PATH        Directory containing .t.sol invariant files
  --chains LIST            Comma-separated chain aliases, e.g. "ethereum,polygon"
  --tools LIST             Comma-separated tool names (default: echidna,halmos,slither)
  --max-depth N            Maximum search depth per chain (default: 4)
  --beam LIST              Beam widths per depth, e.g. "4,3,2,1" (default: adaptive)
  --max-states N           Hard cap on total explored states per chain (default: 200)
  --timeout N              Per-invariant timeout in seconds (default: 600)
  --output FORMAT          Output format: console, json, html (default: console)
  --focus FUNCTION         Only probe a specific invariant function
  --dry-run                Validate configuration without running tools
```

Single-chain probing (for development or single-contract invariants) is supported by omitting `--chains` and providing a single `--target`:

```bash
astarots probe --target src/Vault.sol --invariants test/invariants/
```

### `replay`

Re-execute a previously found cross-chain edge case. Replays the full multi-chain sequence through the mock relayer.

```
astarots replay [OPTIONS]

Options:
  --edge-case PATH          Path to a saved EdgeCase JSON file
  --target PATH,...         Contracts to replay against (may differ from original)
  --chains LIST             Chain aliases for replay
  --output PATH             Where to write the replay trace
```

### `list-tools`

Show which tools are available and their versions.

```
astarots list-tools
```

Output:

```
  echidna    2.1.0    /usr/local/bin/echidna
  halmos     0.2.2    /usr/local/bin/halmos
  slither    0.11.0   /usr/local/bin/slither
```

### `init`

Scaffold a cross-chain invariant test file. Requires chain configuration to determine which contracts to reference.

```
astarots init --target src/BridgeEth.sol,src/BridgePoly.sol --chains ethereum,polygon
```

Creates `test/invariants/BridgeInvariants.t.sol` with skeleton cross-chain invariant functions derived from the contracts' public APIs. The developer fills in assertion bodies.

---

## Cross-Chain Invariant Workflow

### Writing Invariants

Cross-chain invariants are standard Foundry test functions annotated with `@crosschain`:

```solidity
/// @crosschain src=ethereum dst=polygon
/// @tools echidna, halmos
/// @severity CRITICAL
function invariant_locked_equals_minted() public {
    assert(bridgeEth.totalLocked() == bridgePoly.totalMinted());
}
```

The harness decomposes this into per-chain sub-invariants:
- **ethereum sub-invariant:** `locked` state is valid (only changes on verified lock/burn events).
- **polygon sub-invariant:** `minted` state is valid (only changes on verified mint/withdraw events).
- **Cross-check (Python):** `locked(eth_event) == minted(poly_event)` for every correlated message pair.

### Running a Subset of Invariants

To focus on one invariant during development:

```bash
astarots probe \
    --target src/BridgeEth.sol,src/BridgePoly.sol \
    --chains ethereum,polygon \
    --focus invariant_locked_equals_minted
```

### Running a Subset of Tools

Slither first (fast, static), findings seed Echidna (slower, dynamic):

```bash
astarots probe \
    --target src/BridgeEth.sol,src/BridgePoly.sol \
    --chains ethereum,polygon \
    --tools slither,echidna
```

---

## Cross-Chain Configuration

For repeated runs, create `astarots.toml`:

```toml
[default]
target = ["src/BridgeEth.sol", "src/BridgePoly.sol"]
invariants = "test/invariants/"
chains = ["ethereum", "polygon"]
tools = ["echidna", "halmos", "slither"]
max_depth = 4
beam_widths = [4, 3, 2, 1]
max_states = 200
timeout = 600

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

Cross-chain probe output shows per-chain findings with correlation:

```
Cross-Chain Invariant: invariant_locked_equals_minted
═══════════════════════════════════════════════════
Status:   FAIL — PROVEN
Depth:    3 (ethereum) + 2 (polygon) → cross-chain confirmed
Impact:   CRITICAL — unauthorized mint on destination

Source Chain (ethereum):
  [setDelegate(attacker), lock(100 ETH), rotateGuardians()]
  └─ Found by: echidna → halmos (proven)

Destination Chain (polygon):
  [mint(100 ETH), withdraw(100 ETH)]
  └─ Found by: echidna

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
2. **Define invariants** — write `.t.sol` files with `@crosschain` annotations.
3. **Quick scan** — `astarots probe --tools slither` for fast static cross-chain pattern detection.
4. **Shallow fuzz** — `astarots probe --tools echidna --max-depth 2` per chain.
5. **Deep search** — `astarots probe --tools echidna,halmos --max-depth 4` for thorough edge case discovery.
6. **Replay and fix** — `astarots replay` across both chains to verify fixes.
