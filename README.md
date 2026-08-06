# Astarots

Cross-chain invariant testing harness — **mainnet fork state archaeology**. Combines Echidna, Halmos, and Slither under a unified causal beam search to discover deep edge cases in cross-chain protocols. Invariants are checked against forked mainnet state at pinned blocks, not against freshly deployed contracts. The Wormhole protocol has operated for 5+ years — its code is clean, but the accumulated state may harbor edge cases invisible to code-level analysis. **Milestone 1 scope: fork-state invariant testing** at pinned snapshot sets. Full historical archaeology (scanning multiple representative blocks across upgrade epochs) is deferred.

## Quick Start

```bash
# Clone
git clone https://github.com/FajarArrizki/Astarots.git
cd Astarots

# Install dependencies
uv sync

# Run entry point
uv run python main.py
```

Requires Python `>=3.12`.

## Project Layout

```
astarots/
├── main.py                  # Entry point
├── pyproject.toml           # Project metadata & dependencies
├── AGENT.md                 # Agent context (local only, gitignored)
│
├── devil/                   # Application source — one subpackage per function
│   ├── core/                #   Shared kernel: IR, config, base types
│   ├── adapter/             #   Tool adapters (Echidna, Halmos, Slither)
│   ├── harness/             #   Orchestrator & unified beam search
│   ├── invariant/           #   Invariant IR, .t.sol parser
│   └── report/              #   Console & JSON output
│
├── tests/                   # Mirrors devil/ structure
│
└── doch/                    # Design documentation
    ├── architecture/        #   Overall design & data flow
    ├── algorithm/           #   Beam search & search state
    ├── usage/               #   CLI & workflow
    ├── adapter/             #   Adapter protocol & per-tool notes
    ├── invariant/           #   Cross-chain invariant IR spec
    ├── output/              #   Evidence chain & replay
    └── .initial/            #   Bootstrap docs (project structure, deps)
```

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
uv run ruff check .
uv run ruff format .
```

Dependencies are managed with `uv` — [`uv add`](https://docs.astral.sh/uv/concepts/projects/dependencies/) for new deps, [`uv.lock`](https://docs.astral.sh/uv/concepts/projects/layout/#the-lockfile) for reproducible installs.

## References

- [Halmos](https://github.com/a16z/halmos) — symbolic execution
- [Echidna](https://github.com/crytic/echidna) — fuzzing
- [Foundry](https://getfoundry.sh/) — Solidity framework
