# Astarots

Cross-chain invariant testing harness — **mainnet fork state archaeology**. Combines Echidna, Halmos, and Slither under a unified causal beam search to discover deep edge cases in cross-chain protocols. Invariants are checked against forked mainnet state at pinned blocks, not against freshly deployed contracts.

**Milestone 1 scope: fork-state invariant testing** at pinned snapshot sets. Full historical archaeology (scanning multiple representative blocks across upgrade epochs) is deferred.

## Quick Start

```bash
# Clone
git clone https://github.com/FajarArrizki/Astarots.git
cd Astarots

# Install dependencies
uv sync

# Register chains
astarots chain add ethereum --rpc-env ETH_RPC_URL \
    --chain-id 1 --fork-block 18500000
astarots chain add polygon --rpc-env POLY_RPC_URL \
    --chain-id 137 --fork-block 49800000

# Run cross-chain probe
astarots probe \
    --target ethereum.bridge=0xEthBridge \
    --artifact ethereum.bridge=out/IBridgeEth.sol/IBridgeEth.json \
    --target polygon.bridge=0xPolyBridge \
    --artifact polygon.bridge=out/IBridgePoly.sol/IBridgePoly.json \
    --invariant test/invariants/Invariant.t.sol \
    --tool echidna --tool halmos --tool slither
```

Requires Python `>=3.12`.

## Project Layout

```
astarots/
├── main.py                  # Entry point
├── pyproject.toml           # Project metadata & dependencies
│
├── devil/                   # Application source
│   ├── core/                #   Shared kernel: types, config, relay, snapshot
│   ├── adapter/             #   Tool adapters (Echidna, Halmos, Slither)
│   ├── harness/             #   Orchestrator, search, executor, evaluation
│   ├── invariant/           #   Invariant IR, .t.sol NatSpec parser
│   └── evidence/            #   Verdict, report, replay artifacts
│
├── tests/                   # Test suite (37 tests)
│
├── .github/                 # CI/CD
│   ├── workflows/           #   CI, stale bot, issue labeler
│   └── ISSUE_TEMPLATE/      #   Bug report, feature request
│
└── doch/                    # Design documentation
    ├── architecture/        #   Overall design & data flow
    ├── algorithm/           #   Search algorithm & frontier ordering
    ├── usage/               #   CLI commands & workflow
    ├── adapter/             #   Adapter protocol & per-tool notes
    ├── invariant/           #   Cross-chain invariant IR spec
    └── output/              #   Evidence chain & replay
```

## CLI Commands

| Command | Description |
|---|---|
| `astarots chain add <alias>` | Register a pinned chain |
| `astarots chain rm <alias>` | Remove a chain |
| `astarots chain list` | List configured chains |
| `astarots probe` | Run cross-chain invariant search |
| `astarots replay <path>` | Re-execute a found edge case |
| `astarots validate` | Validate config, IR, and fork identities |
| `astarots forks` | Print verified snapshot fingerprints |
| `astarots list-tools` | Show tool availability |
| `astarots init` | Scaffold invariant templates |

## Documentation

Full design documentation in [`doch/`](doch/README.md):

| Doc | Covers |
|---|---|
| [Architecture](doch/architecture/README.md) | Component roles, message lifecycle, data flow |
| [Algorithm](doch/algorithm/README.md) | Unified frontier, branch-local GlobalState, witness correlation |
| [Usage](doch/usage/README.md) | CLI, chain config, invariant workflow, exit codes |
| [Adapter Protocol](doch/adapter/README.md) | Interface, capabilities, outcomes, per-tool strategies |
| [Invariant IR](doch/invariant/README.md) | Transition predicates, correlation extractors, quantification |
| [Output & Evidence](doch/output/README.md) | Verdict/strength split, metadata schema, twin-state replay |

## Development

```bash
# Run tests
uv run pytest

# Format & lint
uv run ruff check devil/ tests/
uv run ruff format devil/ tests/

# Type check
uv run mypy devil/
```

## CI/CD

- **CI**: Lint (ruff), typecheck (mypy), tests (pytest) on every push/PR
- **Stale bot**: Auto-close inactive issues after 14+7 days
- **Issue labeler**: Auto-label by keywords (bug, feature, docs, ci-cd, adapter, relay, etc.)

## References

- [Halmos](https://github.com/a16z/halmos) — symbolic execution
- [Echidna](https://github.com/crytic/echidna) — fuzzing
- [Slither](https://github.com/crytic/slither) — static analysis
- [Foundry](https://getfoundry.sh/) — Solidity framework
