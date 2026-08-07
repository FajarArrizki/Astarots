# Changelog

## 0.1.0-dev

### 2026-08-07 — Executable campaign loop

Implemented the first end-to-end executable layer for the mainnet-fork cross-chain model:

- Added Echidna, Halmos, and Slither adapters with typed outcomes, content-addressed artifacts, explicit Halmos projections, and deterministic fixture coverage.
- Added the unified branch-local frontier with chain fairness, adaptive branching caps, deterministic state fingerprints, cross-chain decomposition/recombination, baseline evaluation, bounded observation sets, and campaign scheduling.
- Added replay artifact generation with redaction and trace hashes, JSON/console reporting, and conservative verdict/evidence-strength confidence aggregation.
- Added both `python main.py` and `python -m devil` CLI entrypoints.
- Added Wormhole fork-test discovery and Forge command planning for the external `foundry.toml` checkout.
- Added deterministic campaign, evidence, CLI, Wormhole, and adapter tests.

### Verification

- `uv run ruff check devil tests pyproject.toml` passes.
- `uv run pytest -q` passes with 19 tests.
- `uv run python main.py --help` exposes the `validate` and `forks` commands.
- Wormhole discovery finds 6 Solidity fork tests in `/Volumes/Disk (256GB)/Oxastarots/Wormhole/Mainet/fork-test`.
