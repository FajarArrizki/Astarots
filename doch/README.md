# Astarots — Design Documentation

Cross-chain invariant testing harness for **mainnet-fork state exploration**. It combines Echidna, Halmos, Slither, and a canonical fork executor under one causal search. Invariants are checked against coherent pinned snapshots so the harness can find state-dependent bugs and transaction sequences that code-only analysis or fresh deployments may miss.

**Milestone 1: Cross-chain.** Developer-declared transition predicates are explored through a unified causal frontier: source actions, relay transitions, and destination actions remain in one branch-local `GlobalState`, and the invariant is evaluated directly against that state. Protocol-specific message formats live behind adapters; future milestones may extend the same engine to single-chain domains.

---

## Docs

| Document | Description |
|---|---|
| [Architecture](architecture/README.md) | Overall design, chain decomposition, component roles, and data flow |
| [Algorithm](algorithm/README.md) | Unified causal best-first search, adaptive branching, state ranking, frontier ordering, and deduplication |
| [Usage](usage/README.md) | CLI commands, chain configuration, cross-chain invariant workflow, and development loop |
| [Adapter Protocol](adapter/README.md) | Adapter interface contract, per-tool implementation notes (Echidna, Halmos, Slither), and output normalization |
| [Invariant Specification](invariant/README.md) | Cross-chain invariant patterns in `.t.sol` — quorum, bridge balance, signature verification, message replay, and NatSpec metadata |
| [Output & Evidence](output/README.md) | Verdict/evidence taxonomy, reproducible JSON, causal trace annotations, and multi-chain replay contracts |

---

## Bootstrap

Initial setup documentation — not part of the shipped design docs:

| Document | Description |
|---|---|
| [Project Structure](.initial/Project stukture/README.md) | Modular per-function directory layout for `devil/`, `tests/`, and `doch/` |
| [Dependencies](.initial/Depedensi/README.md) | Runtime, tooling, and application dependency table with versions and links |
