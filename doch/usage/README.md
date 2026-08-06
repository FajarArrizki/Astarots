# Usage

Astarots is invoked from the command line. The workflow follows a three-phase pattern: define invariants in Solidity, run the harness to probe for edge cases, and review the output to understand what was found.

---

## Quick Start

Given a Foundry project with a target contract at `src/Vault.sol` and invariants at `test/invariants/VaultInvariants.t.sol`:

```bash
astarots probe \
    --target src/Vault.sol \
    --invariants test/invariants/ \
    --tools echidna,halmos,slither \
    --max-depth 4
```

This command will compile the project, load the invariants, and run the adaptive beam search across all three tools up to depth 4. Output appears in the terminal as the search progresses, with a final summary at the end.

---

## Commands

### `probe`

The primary command. Runs the guided search against a target contract.

```
astarots probe [OPTIONS]

Options:
  --target PATH             Solidity file or contract name to analyze
  --invariants PATH         Directory containing .t.sol invariant files
  --tools LIST              Comma-separated tool names (default: echidna,halmos,slither)
  --max-depth N             Maximum search depth (default: 4)
  --beam LIST               Beam widths per depth, e.g. "4,3,2,1" (default: adaptive)
  --max-states N            Hard cap on total explored states (default: 200)
  --timeout N               Per-invariant timeout in seconds (default: 600)
  --output FORMAT           Output format: console, json, html (default: console)
  --chains LIST             Chain aliases for cross-chain probes, e.g. "eth,poly"
  --focus FUNCTION          Only probe a specific invariant function
  --dry-run                 Validate configuration without running tools
```

### `replay`

Re-execute a previously found edge case sequence. Useful for verifying that a fix resolves the issue, or for demonstrating the attack during an audit.

```
astarots replay [OPTIONS]

Options:
  --edge-case PATH          Path to a saved EdgeCase JSON file
  --target PATH             Contract to replay against (may differ from original)
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

Scaffold an invariant test file for a target contract.

```
astarots init --target src/Vault.sol
```

Creates `test/invariants/VaultInvariants.t.sol` with skeleton invariant functions derived from the contract's public API and storage layout. The developer then fills in the assertion bodies.

---

## Invariant File Workflow

### Writing Invariants

Invariants live in `test/invariants/` as standard Foundry test files. The harness recognizes functions prefixed with `invariant_`:

```solidity
contract VaultInvariants is Test {
    Vault vault;

    function setUp() public {
        vault = new Vault();
    }

    function invariant_no_overdraft() public {
        // Checks that no user can withdraw more than deposited
        // Called after every transaction sequence by Echidna
        // Checked symbolically by Halmos
        // Scanned for vulnerability patterns by Slither
    }
}
```

The same invariant function is consumed by all tools. The adapters translate it into tool-specific formats — Echidna as a property check, Halmos as a target assertion, Slither as a taint source.

### Running a Subset of Invariants

To focus on one invariant during development:

```bash
astarots probe --target src/Vault.sol --focus invariant_no_overdraft
```

### Running a Subset of Tools

To skip slow tools during rapid iteration:

```bash
astarots probe --target src/Vault.sol --tools slither,echidna
```

Slither runs first (fast, static), findings seed Echidna (slower, dynamic).

---

## Cross-Chain Workflow

For bridges and cross-chain protocols, invariants span multiple chain states.

### Step 1: Define chain configurations

```bash
astarots chain add ethereum --rpc $ETH_RPC_URL
astarots chain add polygon  --rpc $POLY_RPC_URL
```

### Step 2: Define cross-chain invariant

```solidity
/// @crosschain src=ethereum dst=polygon
function invariant_bridge_locked_equals_minted() public {
    uint ethLocked = bridgeEth.totalLocked();
    uint polyMinted = bridgePoly.totalMinted();
    assert(ethLocked == polyMinted);
}
```

### Step 3: Run

```bash
astarots probe \
    --target src/BridgeEth.sol,src/BridgePoly.sol \
    --invariants test/invariants/ \
    --chains ethereum,polygon
```

The harness deploys contracts on both mock chains, mocks the relayer between them, and probes per-chain with the cross-chain invariant checked at the orchestration layer.

---

## Output Interpretation

At the end of a probe run, the console shows a summary table:

```
Invariant                               Tool(s)       Depth  Confidence   Impact
─────────────────────────────────────────────────────────────────────────────
invariant_no_overdraft                  PASS           —      —            —
invariant_deposit_equals_shares         echidna+halmos  3     PROVEN       HIGH
  └─ {delegate=attacker, fee=max, oracle=stale}
  └─ [setDelegate(attacker), deposit(100), rebalance(), withdraw(50)]
invariant_supply_under_cap              slither         1     WARNING      LOW
  └─ unchecked arithmetic in mint() at L42
```

A `PASS` result means no tool found a violation at any depth. A `PROVEN` result means two tools independently confirmed the edge case. A `WARNING` result means a static tool flagged a pattern but no dynamic tool produced a concrete violation.

---

## Configuration File

For repeated runs, create `astarots.toml` in the project root:

```toml
[default]
target = "src/Vault.sol"
invariants = "test/invariants/"
tools = ["echidna", "halmos", "slither"]
max_depth = 4
beam_widths = [4, 3, 2, 1]
max_states = 200
timeout = 600

[chains.ethereum]
rpc_url = "$ETH_RPC_URL"

[chains.polygon]
rpc_url = "$POLY_RPC_URL"

[tools.echidna]
timeout = 300
test_limit = 50000

[tools.halmos]
timeout = 600
solver_timeout = 120

[tools.slither]
detectors = ["reentrancy", "unchecked-transfer", "access-control"]
```

Command-line flags override config file values.

---

## Development Loop

The recommended workflow while building or auditing:

1. **Define invariants** — write `.t.sol` files capturing security properties.
2. **Quick scan** — `astarots probe --tools slither` for fast static analysis. Fix obvious issues.
3. **Shallow fuzz** — `astarots probe --tools echidna --max-depth 2` for dynamic exploration. Fix found issues.
4. **Deep search** — `astarots probe --tools echidna,halmos --max-depth 4` for thorough edge case discovery.
5. **Replay and fix** — use `astarots replay` to verify fixes against found edge cases.

Each step builds on the previous one, progressively tightening the security guarantees without wasting deep analysis on shallow issues.
