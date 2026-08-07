# Usage

Astarots is invoked from the command line. Cross-chain invariant testing requires chain configuration before the first probe. The workflow follows: define chains, write cross-chain invariants in Solidity, run the harness, and review per-chain traces.

---

## Quick Start

Given a Foundry project with bridge contracts on Ethereum and Polygon, and invariants at `test/invariants/BridgeInvariants.t.sol`:

```bash
# Register coherent pinned forks without expanding RPC secrets in argv
astarots chain add ethereum --rpc-env ETH_RPC_URL \
    --chain-id 1 --fork-block 18500000
astarots chain add polygon --rpc-env POLY_RPC_URL \
    --chain-id 137 --fork-block 49800000

# Bind deployed contexts and their compiled artifacts
astarots probe \
    --target ethereum.bridge=0xEthBridge \
    --artifact ethereum.bridge=out/IBridgeEth.sol/IBridgeEth.json \
    --source ethereum.bridge=src/BridgeEth.sol \
    --target polygon.bridge=0xPolyBridge \
    --artifact polygon.bridge=out/IBridgePoly.sol/IBridgePoly.json \
    --source polygon.bridge=src/BridgePoly.sol \
    --invariants test/invariants/ \
    --relay-dataset artifacts/relay/messages.json \
    --relay-mode protocol-valid-synthetic \
    --relay-config astarots.relay.toml \
    --tool echidna --tool halmos --tool slither \
    --max-depth 8
```

This command validates the `SnapshotSet`, loads the relay dataset, runs one unified causal search, replays every tool proposal through the canonical fork executor, and evaluates invariants against branch-local global snapshots.

---

## Commands

### `chain`

Manage chain configurations. Chains must be registered before probing.

```
astarots chain add <alias> --rpc-env <ENV_NAME> --chain-id <ID> --fork-block <BLOCK>
astarots chain rm  <alias>
astarots chain list
```

Example:

```bash
astarots chain add ethereum --rpc-env ETH_RPC_URL \
    --chain-id 1 --fork-block 18500000
astarots chain add polygon --rpc-env POLY_RPC_URL \
    --chain-id 137 --fork-block 49800000
astarots chain add arbitrum --rpc-env ARB_RPC_URL \
    --chain-id 42161 --fork-block 120000000
```

### `probe`

Run guided search against cross-chain invariants — starting from forked mainnet state at the pinned blocks configured per chain. Contracts are NOT deployed fresh; the harness forks mainnet and probes from the accumulated state.

```
astarots probe [OPTIONS]

Options:
  --target CONTEXT=ADDRESS    Deployed target, e.g. ethereum.bridge=0x...
  --artifact CONTEXT=PATH     Compiled ABI/storage-layout artifact for the context
  --source CONTEXT=PATH       Source target for source-level tools such as Slither
  --invariant PATH            Invariant specification file or directory
  --relay-dataset PATH        Content-addressed message/attestation dataset
  --relay-mode MODE           historical-authentic, protocol-valid-synthetic,
                             modeled-relay, or raw-payload
  --relay-config PATH         Protocol adapter and local proof-fixture configuration
  --actor-policy PATH         Allowed actors, privileges, funding, impersonation
  --tool NAME                 Candidate/confirmation tool (repeatable)
  --max-depth N               Maximum global causal action depth (default: 8)
  --max-states N              Global authoritative execution cap (default: 200)
  --timeout N                 Per-invariant timeout seconds (default: 600)
  --output PATH               Output directory for JSON reports
  --json                      Emit JSON to stdout
  --dry-run                   Validate targets, snapshots, IR, and datasets
```

### Target Binding

Each repeatable target uses `chain.context=deployed-address`; its compiled artifact and optional source target are supplied separately. Source is required when a selected adapter needs source-level analysis. This supports multiple contracts on one chain without positional matching:

```bash
astarots probe \
    --target ethereum.core=0xCore \
    --artifact ethereum.core=out/ICore.sol/ICore.json \
    --source ethereum.core=src/Core.sol \
    --target ethereum.token_bridge=0xTokenBridge \
    --artifact ethereum.token_bridge=out/ITokenBridge.sol/ITokenBridge.json \
    --source ethereum.token_bridge=src/TokenBridge.sol \
    --target polygon.token_bridge=0xPolyTokenBridge \
    --artifact polygon.token_bridge=out/ITokenBridge.sol/ITokenBridge.json \
    --source polygon.token_bridge=src/TokenBridge.sol \
    --invariants test/invariants/
```

Every address, runtime code hash, proxy implementation, and artifact hash is validated at the configured block. Context IDs bind targets explicitly; chain order is never inferred positionally.

### `replay`

Re-execute a previously found cross-chain edge case. Uses deterministic twin-state replay (multi-fork or twin database).

```
astarots replay PATH [--config PATH] [--replacement CONTEXT=ADDRESS] [--json]
```

The `path` argument points to a directory of replay artifacts (`.json` + `.t.sol` pairs) or a single metadata JSON file. A replacement target must provide every selector used by the recorded trace and pass artifact, runtime-code, proxy-kind, and implementation validation. Any ABI or proxy mismatch is an invalid replay configuration, not an execution failure.

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

Scaffold a parser-only cross-chain invariant specification bound to existing deployments:

```
astarots init [--template lock-mint|generic] [--targets N]
```

The generated `.t.sol` file contains NatSpec IR plus a human-readable assertion. Astarots generates executable per-chain harnesses; it does not deploy the targets.

### `validate`

Validate config, invariant IR, and fork identities without running a probe:

```
astarots validate [--config PATH] [--invariant PATH] [--json]
```

Checks that all chains are configured, targets reference valid chains, relay/actor policies are present, snapshot fingerprints verify against RPCs, and invariant IR parses correctly.

### `forks`

Print verified snapshot fingerprints for all configured chains:

```
astarots forks [--config PATH] [--json]
```

Outputs chain IDs, block numbers, block hashes, state roots, and fork cache hashes — useful for debugging snapshot coherence issues.

---

## Precedence

Configuration sources resolve in this order, with later sources overriding earlier ones:

1. Built-in defaults
2. `astarots.toml`
3. NatSpec invariant metadata
4. CLI flags

Environment references are interpolated **after** precedence selects the winning value; interpolation is not a separate precedence layer.

---

## Exit Codes

| Code | Meaning |
|---|---|
| 0 | No violations found (verdict: not-observed for all invariants) |
| 1 | Violation found (verdict: violated for at least one invariant) |
| 2 | Error (exception during execution — invalid config, tool error, or runtime failure) |

---

## RPC Secret Protection

RPC configuration stores an environment-variable name rather than an expanded URL:

```toml
[chains.ethereum]
rpc_env = "ETH_RPC_URL"
```

The harness resolves it only when spawning the fork backend, redacts process diagnostics, and never records the URL or secret value. CLI uses `--rpc-env ETH_RPC_URL`, not `--rpc "$ETH_RPC_URL"`.

---

## Cross-Chain Invariant Workflow

### Writing Invariants

Cross-chain functions use Foundry syntax as an authoring surface, but Astarots parses the full required IR and generates executable per-chain harnesses:

```solidity
/// @crosschain contexts=ethereum.bridge,polygon.bridge entry=ethereum.bridge
/// @transition ethereum.bridge:locked increase=["deposit(uint256,address)"] decrease=["burn(uint256,address)","expireMessage(bytes32)"]
/// @transition polygon.bridge:minted increase=["mint(bytes)"] decrease=["withdraw(uint256,address)"]
/// @observation AFTER_ALL_DELIVERED quiescence=NO_ELIGIBLE_MESSAGES max_pending_age=ethereum:7200s exclude=expired,rejected
/// @correlation bridge_message
/// @bind locked=ethereum.bridge.totalLocked() minted=polygon.bridge.totalMinted()
/// @quantify FORALL locked,minted: locked == minted
/// @observe touched,relay max=256
/// @assume message_ordering: ordered_by_sequence
function invariant_locked_equals_minted() public {
    assert(bridgeEth.totalLocked() == bridgePoly.totalMinted());
}
```

### Running a Subset

```bash
astarots probe \
    --target ethereum.bridge=0xEthBridge \
    --artifact ethereum.bridge=out/IBridgeEth.sol/IBridgeEth.json \
    --target polygon.bridge=0xPolyBridge \
    --artifact polygon.bridge=out/IBridgePoly.sol/IBridgePoly.json \
    --tool slither --tool echidna
```

---

## Cross-Chain Configuration

For repeated runs, create `astarots.toml`:

```toml
[default]
invariants = "test/invariants/"
tools = ["echidna", "halmos", "slither"]
max_depth = 8
branching_caps = [4, 4, 3, 3, 2, 2, 1, 1]
max_consecutive_expansions_per_chain = 4
max_states = 200
timeout = 600

[chains.ethereum]
rpc_env = "ETH_RPC_URL"
chain_id = 1
fork_block = 18500000
expected_block_hash = "0x..."
expected_state_root = "0x..."

[chains.polygon]
rpc_env = "POLY_RPC_URL"
chain_id = 137
fork_block = 49800000
expected_block_hash = "0x..."
expected_state_root = "0x..."

[targets."ethereum.bridge"]
address = "0xEthBridge"
artifact = "out/IBridgeEth.sol/IBridgeEth.json"
source = "src/BridgeEth.sol"
role = "source"
expected_code_hash = "0x..."
proxy_kind = "uups"
implementation_address = "0x..."
expected_implementation_code_hash = "0x..."

[targets."polygon.bridge"]
address = "0xPolyBridge"
artifact = "out/IBridgePoly.sol/IBridgePoly.json"
source = "src/BridgePoly.sol"
role = "destination"
expected_code_hash = "0x..."
proxy_kind = "uups"
implementation_address = "0x..."
expected_implementation_code_hash = "0x..."

[correlations.bridge_message]
source_context = "ethereum.bridge"
source_event = "Locked(bytes32,address,uint256)"
source_fields = ["messageHash"]
destination_context = "polygon.bridge"
destination_event = "Minted(bytes32,address,uint256)"
destination_fields = ["messageHash"]
normalize = "bytes32"

[relay]
dataset = "artifacts/relay/messages.json"
mode = "protocol-valid-synthetic"
protocol_adapter = "example-bridge"
delay_model = "bounded"
dataset_hash = "sha256:..."
adapter_config = "astarots.relay.toml"
adapter_config_hash = "sha256:..."
ordering = "fifo_per_emitter"
duplicate_delivery = "reject"
reorg_assumption = "no_reorg_after_finality"
delivery_deadline = { value = 7200, unit = "seconds", chain_id = "ethereum" }

[relay.finality_blocks]
ethereum = 64
polygon = 128

[relay.min_delay_seconds]
ethereum = 0
polygon = 0

[relay.max_delay_seconds]
ethereum = 7200
polygon = 7200

[snapshot]
max_timestamp_delta = 300
require_finalized = true

[actors]
policy = "astarots.actors.toml"
policy_hash = "sha256:..."

[tools.echidna]
timeout = 300
test_limit = 50000

[tools.halmos]
timeout = 600
solver_timeout = 120
```

---

## Output Interpretation

Cross-chain output separates the campaign verdict, full-trace replay strength, and optional per-segment confirmation:

```
Cross-Chain Invariant: invariant_locked_equals_minted
═══════════════════════════════════════════════════
Verdict:           violated
Violation source: introduced_by_trace
Aggregate:         replayed
Global depth:      5 causal actions
Actor class:       permissionless
Relay:             protocol-valid-synthetic (policy sha256:...)
Impact:            CRITICAL — unauthorized mint on destination

Trace:
  [ethereum] setDelegate(attacker)
  [ethereum] lock(100 ETH)
  [relay]    message 0x...
  [polygon]  mint(100 ETH)
  [polygon]  withdraw(100 ETH)

Segment evidence:
  ethereum: observed by Echidna;
            symbolically-confirmed-under-projected-state by Halmos
  polygon:  replayed by canonical executor
```

The trace is one causal branch, not a combination of independent per-chain results. Every relay transition, actor, base fingerprint, bound, and projection manifest is available in JSON.

---

## Development Loop

1. **Define snapshots and targets** — register pinned chains and deployed contexts.
2. **Define relay and actor policies** — pin the dataset, authenticity mode, and allowed privileges.
3. **Define invariants** — author the complete NatSpec IR and human-readable assertion.
4. **Quick static scan** — use Slither for candidate hints.
5. **Causal search** — use Echidna candidates with a global depth appropriate to the protocol.
6. **Confirm and replay** — project selected witnesses into Halmos, replay the full trace, and run the fixed regression against an explicit patched target.
