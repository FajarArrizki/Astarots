# Astarots — Design Documentation

Guided attack-surface explorer for Solidity contracts. Combines Echidna, Halmos, Slither, and other analysis tools under a unified harness to discover deep edge cases that no single tool can find alone.

---

## Docs

| Document | Description |
|---|---|
| [Architecture](architecture/README.md) | Overall design, component roles, data flow, and key decisions |
| [Algorithm](algorithm/README.md) | Core search algorithm — adaptive beam search, state structures, ranking, frontier ordering, deduplication |
| [Usage](usage/README.md) | CLI commands, invariant file workflow, cross-chain setup, configuration, and development loop |
| [Adapter Protocol](adapter/README.md) | Adapter interface contract, per-tool implementation notes (Echidna, Halmos, Slither), and output normalization |
| [Invariant Specification](invariant/README.md) | How to write invariants in `.t.sol`, invariant types, constraints, cross-chain invariants, and NatSpec metadata |
| [Output & Evidence](output/README.md) | Evidence chain, console output format, JSON export, replay contract generation, and confidence model |

---

## Bootstrap

Initial setup documentation — not part of the shipped design docs:

| Document | Description |
|---|---|
| [Project Structure](.initial/Project%20stukture/README.md) | Modular per-function directory layout for `devil/`, `tests/`, and `doch/` |
| [Dependencies](.initial/Depedensi/README.md) | Runtime, tooling, and application dependency table with versions and links |
