# Astarots — Design Documentation

Cross-chain invariant testing harness. Combines Echidna, Halmos, Slither, and other analysis tools under a unified beam search to discover deep edge cases in cross-chain protocols — bridges, message relays, and multi-chain applications — that no single tool can find alone.

**Milestone 1: Cross-chain.** The harness decomposes multi-chain invariants into per-chain sub-probes with developer-declared transition predicates, runs causal beam search across a unified frontier — source-chain steps precede destination-chain steps through an explicit message lifecycle, no Cartesian recombination — and correlates witnesses by correlation value to verify the cross-chain property. Future milestones will extend this to single-chain protocol domains (DEX, lending, staking).

---

## Docs

| Document | Description |
|---|---|
| [Architecture](architecture/README.md) | Overall design, chain decomposition, component roles, and data flow |
| [Algorithm](algorithm/README.md) | Core search algorithm — adaptive beam search, cross-chain state decomposition, ranking, frontier ordering, deduplication |
| [Usage](usage/README.md) | CLI commands, chain configuration, cross-chain invariant workflow, and development loop |
| [Adapter Protocol](adapter/README.md) | Adapter interface contract, per-tool implementation notes (Echidna, Halmos, Slither), and output normalization |
| [Invariant Specification](invariant/README.md) | Cross-chain invariant patterns in `.t.sol` — quorum, bridge balance, signature verification, message replay, and NatSpec metadata |
| [Output & Evidence](output/README.md) | Per-chain evidence chain, console output, JSON export, multi-chain replay contracts, and confidence model |

---

## Bootstrap

Initial setup documentation — not part of the shipped design docs:

| Document | Description |
|---|---|
| [Project Structure](.initial/Project stukture/README.md) | Modular per-function directory layout for `devil/`, `tests/`, and `doch/` |
| [Dependencies](.initial/Depedensi/README.md) | Runtime, tooling, and application dependency table with versions and links |
